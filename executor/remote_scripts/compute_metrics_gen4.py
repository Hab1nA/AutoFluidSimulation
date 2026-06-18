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

SIDEWALL_ZONES = frozenset({"wall_chamber", "wall_nozzle", "wall_throat"})


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
        y_velocity = _number(row, "y_velocity", "y-velocity") if "y_velocity" in row or "y-velocity" in row else 0.0
        z_velocity = _number(row, "z_velocity", "z-velocity") if "z_velocity" in row or "z-velocity" in row else 0.0
        pressure = _number(row, "pressure")
        x_face_area = _number(row, "x_face_area", "x-face-area")
        y_face_area = _number(row, "y_face_area", "y-face-area") if "y_face_area" in row or "y-face-area" in row else 0.0
        z_face_area = _number(row, "z_face_area", "z-face-area") if "z_face_area" in row or "z-face-area" in row else 0.0
        velocity_flux = x_velocity * x_face_area + y_velocity * y_face_area + z_velocity * z_face_area
        momentum_terms.append(density * x_velocity * velocity_flux)
        pressure_terms.append((pressure - ambient_pressure) * x_face_area)
    f_momentum = _sum(momentum_terms)
    f_pressure = _sum(pressure_terms)
    return f_momentum, f_pressure, f_momentum + f_pressure


def _compute_phi(
    rows: list[dict[str, str]],
    tcomb: float,
    chamber_x_max: float | None,
) -> tuple[float, float, float]:
    hot: list[tuple[float, float]] = []
    for row in rows:
        temperature = _number(row, "temperature")
        if temperature <= tcomb:
            continue
        if chamber_x_max is not None:
            x_coordinate = _number(row, "x_coordinate", "x-coordinate")
            if x_coordinate > chamber_x_max:
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


def _compute_phi_from_integrals(reports: dict[str, float]) -> tuple[float, float, float]:
    hot_volume = reports["phi_hot_volume"]
    if hot_volume <= 0:
        return math.nan, math.nan, 0.0
    phi_mean = reports["phi_sum"] / hot_volume
    phi2_mean = reports["phi2_sum"] / hot_volume
    variance = phi2_mean - phi_mean * phi_mean
    return phi_mean, math.sqrt(max(variance, 0.0)), hot_volume


def _compute_wall_metrics(rows: list[dict[str, str]]) -> dict[str, float]:
    weighted_terms: list[float] = []
    area_terms: list[float] = []
    all_temperatures: list[float] = []
    throat_temperatures: list[float] = []

    for row in rows:
        zone = row.get("zone", "")
        if zone not in SIDEWALL_ZONES:
            continue
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
        "Tmax_sidewall": max(all_temperatures) if all_temperatures else math.nan,
        "T_p995_wall": _percentile(all_temperatures, 0.995),
    }


def compute_metrics(
    *,
    reports_path: Path,
    exit_surface_path: Path | None,
    chamber_cells_path: Path | None,
    wall_faces_path: Path | None,
    ambient_pressure: float,
    tcomb: float,
    chamber_x_max: float | None = None,
) -> dict[str, float]:
    reports = _read_report_values(reports_path)

    mdot_oxidizer = abs(reports["mdot_oxidizer"])
    mdot_fuel = abs(reports["mdot_fuel"])
    mdot_total = mdot_oxidizer + mdot_fuel
    mdot_outlet = abs(reports["mdot_outlet"])
    if "F_momentum" in reports and "F_pressure" in reports:
        f_momentum = reports["F_momentum"]
        f_pressure = reports["F_pressure"]
        f_total = f_momentum + f_pressure
    else:
        if exit_surface_path is None:
            raise ValueError("exit_surface_path is required when force reports are absent")
        exit_rows = _read_rows(exit_surface_path)
        f_momentum, f_pressure, f_total = _compute_exit_forces(exit_rows, ambient_pressure)
    if chamber_cells_path is None:
        phi_mean, phi_std, hot_volume = _compute_phi_from_integrals(reports)
    else:
        chamber_rows = _read_rows(chamber_cells_path)
        phi_mean, phi_std, hot_volume = _compute_phi(chamber_rows, tcomb, chamber_x_max)
    if {"wall_area", "Twall_total", "Tmax_throat", "Tmax_sidewall"} <= reports.keys():
        wall_metrics = {
            "wall_area": reports["wall_area"],
            "Twall_total": reports["Twall_total"],
            "Tmax_throat": reports["Tmax_throat"],
            "Tmax_sidewall": reports["Tmax_sidewall"],
            "T_p995_wall": reports.get("T_p995_wall", math.nan),
        }
    else:
        if wall_faces_path is None:
            raise ValueError("wall_faces_path is required when wall reports are absent")
        wall_rows = _read_rows(wall_faces_path)
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
    parser.add_argument("--exit-surface", type=Path, default=None)
    parser.add_argument(
        "--chamber-cells",
        type=Path,
        default=None,
        help="Optional cell table. Omit when reports contain phi_hot_volume, phi_sum, and phi2_sum.",
    )
    parser.add_argument("--wall-faces", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ambient-pressure", type=float, default=0.0)
    parser.add_argument("--tcomb", type=float, default=DEFAULT_TCOMB)
    parser.add_argument(
        "--chamber-x-max",
        type=float,
        default=None,
        help="Maximum x-coordinate included in combustion-chamber phi statistics.",
    )
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
        chamber_x_max=args.chamber_x_max,
    )
    _write_metrics(args.output, metrics)


if __name__ == "__main__":
    main()
