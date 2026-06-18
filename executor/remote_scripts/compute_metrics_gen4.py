"""Compute research metrics from Fluent post-processing exports.

The companion Fluent journal exports the minimum report values and raw tables.
This script owns the physics formulas so they can be tested without launching
Fluent.
"""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Iterable

G0 = 9.80665
LHV_CH4 = 50_000_000.0
DEFAULT_TCOMB = 1000.0
EPS = 1.0e-12

WALL_ZONES = ("wall_chamber", "wall_nozzle", "wall_throat", "wall_top", "wall_gap", "s------6")


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _number(row: dict[str, str], *names: str) -> float:
    for name in names:
        if name in row and row[name] not in {"", None}:
            return float(row[name])
    raise KeyError(f"missing numeric column: {'/'.join(names)}")


def _read_report_values(path: Path) -> dict[str, float]:
    values: dict[str, float] = {}
    for row in _read_rows(path):
        name = row.get("name")
        if not name:
            raise ValueError(f"report row missing name in {path}")
        values[name] = _number(row, "value")
    return values


def _sum(values: Iterable[float]) -> float:
    return math.fsum(values)


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return math.nan
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = (len(sorted_values) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return sorted_values[lower]
    weight = index - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _compute_exit_forces(
    rows: list[dict[str, str]],
    ambient_pressure: float,
) -> tuple[float, float, float]:
    momentum_terms: list[float] = []
    pressure_terms: list[float] = []
    for row in rows:
        density = _number(row, "density")
        x_velocity = _number(row, "x_velocity", "x-velocity")
        pressure = _number(row, "pressure")
        x_face_area = _number(row, "x_face_area", "x-face-area")
        momentum_terms.append(density * x_velocity * x_velocity * x_face_area)
        pressure_terms.append((pressure - ambient_pressure) * x_face_area)
    f_momentum = _sum(momentum_terms)
    f_pressure = _sum(pressure_terms)
    return f_momentum, f_pressure, f_momentum + f_pressure


def _compute_phi(rows: list[dict[str, str]], tcomb: float) -> tuple[float, float, float]:
    hot: list[tuple[float, float]] = []
    for row in rows:
        temperature = _number(row, "temperature")
        if temperature <= tcomb:
            continue
        fmean = _number(row, "fmean")
        volume = _number(row, "cell_volume", "cell-volume")
        phi = ((1.0 - fmean) / max(fmean, EPS)) / 4.0
        hot.append((phi, volume))

    hot_volume = _sum(volume for _, volume in hot)
    if hot_volume <= 0:
        return math.nan, math.nan, 0.0
    phi_mean = _sum(phi * volume for phi, volume in hot) / hot_volume
    variance = _sum(((phi - phi_mean) ** 2) * volume for phi, volume in hot) / hot_volume
    return phi_mean, math.sqrt(max(variance, 0.0)), hot_volume


def _compute_wall_metrics(rows: list[dict[str, str]]) -> dict[str, float]:
    weighted_terms: list[float] = []
    area_terms: list[float] = []
    all_temperatures: list[float] = []
    throat_temperatures: list[float] = []

    for row in rows:
        zone = row.get("zone", "")
        temperature = _number(row, "temperature", "wall_temperature", "wall-temperature")
        area = _number(row, "face_area_magnitude", "face-area-magnitude")
        weighted_terms.append(temperature * area)
        area_terms.append(area)
        all_temperatures.append(temperature)
        if zone == "wall_throat":
            throat_temperatures.append(temperature)

    total_area = _sum(area_terms)
    all_temperatures.sort()
    return {
        "wall_area": total_area,
        "Twall_total": _sum(weighted_terms) / total_area if total_area > 0 else math.nan,
        "Tmax_throat": max(throat_temperatures) if throat_temperatures else math.nan,
        "Tmax_all": max(all_temperatures) if all_temperatures else math.nan,
        "T_p995_wall": _percentile(all_temperatures, 0.995),
    }


def compute_metrics(
    *,
    reports_path: Path,
    exit_surface_path: Path,
    chamber_cells_path: Path,
    wall_faces_path: Path,
    ambient_pressure: float,
    tcomb: float,
) -> dict[str, float]:
    reports = _read_report_values(reports_path)
    exit_rows = _read_rows(exit_surface_path)
    chamber_rows = _read_rows(chamber_cells_path)
    wall_rows = _read_rows(wall_faces_path)

    mdot_oxidizer = abs(reports["mdot_oxidizer"])
    mdot_fuel = abs(reports["mdot_fuel"])
    mdot_total = mdot_oxidizer + mdot_fuel
    mdot_outlet = abs(reports["mdot_outlet"])
    f_momentum, f_pressure, f_total = _compute_exit_forces(exit_rows, ambient_pressure)
    phi_mean, phi_std, hot_volume = _compute_phi(chamber_rows, tcomb)
    wall_metrics = _compute_wall_metrics(wall_rows)
    qdot_actual = reports["qdot_actual"]
    qdot_theoretical = mdot_fuel * LHV_CH4

    metrics = {
        "mdot_oxidizer": mdot_oxidizer,
        "mdot_fuel": mdot_fuel,
        "mdot_total": mdot_total,
        "mdot_outlet": mdot_outlet,
        "mass_imbalance": abs(mdot_total - mdot_outlet) / mdot_total if mdot_total > 0 else math.nan,
        "F_momentum": f_momentum,
        "F_pressure": f_pressure,
        "F_total": f_total,
        "Isp": f_total / (mdot_total * G0) if mdot_total > 0 else math.nan,
        "Qdot_actual": qdot_actual,
        "Qdot_theoretical": qdot_theoretical,
        "eta_c": qdot_actual / qdot_theoretical if qdot_theoretical > 0 else math.nan,
        "phi_mean": phi_mean,
        "phi_std": phi_std,
        "hot_volume": hot_volume,
    }
    metrics.update(wall_metrics)
    return metrics


def _write_metrics(path: Path, metrics: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(metrics))
        writer.writeheader()
        writer.writerow(metrics)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute Fluent research metrics from exported CSV files.")
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--exit-surface", type=Path, required=True)
    parser.add_argument("--chamber-cells", type=Path, required=True)
    parser.add_argument("--wall-faces", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ambient-pressure", type=float, default=0.0)
    parser.add_argument("--tcomb", type=float, default=DEFAULT_TCOMB)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    metrics = compute_metrics(
        reports_path=args.reports,
        exit_surface_path=args.exit_surface,
        chamber_cells_path=args.chamber_cells,
        wall_faces_path=args.wall_faces,
        ambient_pressure=args.ambient_pressure,
        tcomb=args.tcomb,
    )
    _write_metrics(args.output, metrics)


if __name__ == "__main__":
    main()
