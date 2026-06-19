"""Post-process a Fluent Gen4 case/data pair into research metrics.

The script is intended to run on the Fluent workstation inside the pinned
PyFluent environment. Fluent performs surface and volume integrations in-process;
Python only combines the resulting scalar reports into the five research
metrics.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import re
from pathlib import Path
from typing import Any

import ansys.fluent.core as pyfluent

SIDEWALL_ZONES = ("wall_chamber", "wall_nozzle", "wall_throat")
FLUID_ZONE = "s------6.5076"
OUTLET_ZONE = "outlet"
INLET_OXIDIZER = "inlet_oxidizer"
INLET_FUEL = "inlet_fuel"
DEFAULT_EXIT_TO_THROAT_AREA_RATIO = 7.427276607
DEFAULT_CSTAR_REFERENCE = 1830.4
REPORT_VALUE_RE = re.compile(r"^\s*(?:Net|\S+)\s+([-+0-9.Ee]+)\s*$")
EXPRESSION_ROW_RE = re.compile(r"^\s*(\S+)\s+([-+0-9.Ee]+)\s+(?:\[.*\])?\s*$")
REPORT_UNIT_RE = re.compile(r"\[([^\]]+)\]")
LENGTH_TO_M = {
    "m": 1.0,
    "cm": 1.0e-2,
    "mm": 1.0e-3,
    "um": 1.0e-6,
    "in": 0.0254,
    "ft": 0.3048,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute Gen4 Fluent post-processing metrics.")
    parser.add_argument("--case-data", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--compute-script", type=Path, default=Path(__file__).with_name("compute_metrics_gen4.py"))
    parser.add_argument("--fluent-path", type=Path, default=Path(r"C:\Program Files\ANSYS Inc\v241\fluent\ntbin\win64\fluent.exe"))
    parser.add_argument("--processor-count", type=int, default=2)
    parser.add_argument("--ambient-pressure", type=float, default=0.0)
    parser.add_argument("--pressure-reference", type=float, default=101325.0)
    parser.add_argument("--tcomb", type=float, default=1000.0)
    parser.add_argument("--chamber-x-max", type=float, default=None)
    parser.add_argument("--thrust-axis", choices=("x", "y", "z"), default="x")
    parser.add_argument("--exit-to-throat-area-ratio", type=float, default=DEFAULT_EXIT_TO_THROAT_AREA_RATIO)
    parser.add_argument("--cstar-reference", type=float, default=DEFAULT_CSTAR_REFERENCE)
    parser.add_argument("--config-name", type=str, default=None)
    parser.add_argument("--config-id", type=int, default=None)
    return parser.parse_args()


def _load_compute_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("compute_metrics_gen4_runtime", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load compute script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_report_value(path: Path, name: str, value: float, *, append: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and append
    with path.open("a" if append else "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "value"])
        if not exists:
            writer.writeheader()
        writer.writerow({"name": name, "value": value})


def _parse_report_file(path: Path) -> float:
    text = path.read_text(encoding="utf-8", errors="replace")
    matches = [
        match.group(1)
        for line in text.splitlines()
        if (match := REPORT_VALUE_RE.match(line))
    ]
    if not matches:
        raise ValueError(f"could not parse Net value from {path}")
    return float(matches[-1])


def _parse_report_unit(path: Path) -> str | None:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = REPORT_UNIT_RE.search(line)
        if match:
            return match.group(1).strip()
    return None


def _run_report(command: Any, path: Path, **kwargs: Any) -> float:
    command(write_to_file=True, file_name=str(path), **kwargs)
    return _parse_report_file(path)


def _run_length_report_m(command: Any, path: Path, **kwargs: Any) -> float:
    command(write_to_file=True, file_name=str(path), **kwargs)
    value = _parse_report_file(path)
    unit = _parse_report_unit(path)
    if unit is None:
        raise ValueError(f"could not parse length unit from {path}")
    try:
        return value * LENGTH_TO_M[unit]
    except KeyError as exc:
        raise ValueError(f"unsupported length unit {unit!r} in {path}") from exc


def _define_expression(named_expressions: Any, name: str, definition: str) -> None:
    if name in named_expressions.get_object_names():
        named_expressions.delete(name)
    named_expressions.create(name)
    named_expressions[name].set_state({"definition": definition})


def _define_custom_field_function(solver: Any, name: str, definition: str) -> None:
    solver.tui.define.custom_field_functions.define(f'"{name}"', f'"{definition}"')


def _parse_expression_values(transcript: Path, names: set[str]) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        match = EXPRESSION_ROW_RE.match(line)
        if match and match.group(1) in names:
            values[match.group(1)] = float(match.group(2))
    missing = names - set(values)
    if missing:
        raise ValueError(f"missing expression values in transcript: {sorted(missing)}")
    return values


def _find_latest_transcript(run_dir: Path) -> Path:
    transcripts = sorted(run_dir.glob("fluent-*.trn"), key=lambda p: p.stat().st_mtime)
    if not transcripts:
        raise FileNotFoundError(f"no Fluent transcript found in {run_dir}")
    return transcripts[-1]


def _default_config_name(case_data: Path) -> str:
    name = case_data.name
    for suffix in (".cas.h5", ".dat.h5", ".cas", ".dat"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return case_data.stem


def _infer_chamber_x_max(solver: Any, reports_dir: Path) -> float:
    reports_dir.mkdir(parents=True, exist_ok=True)
    return _run_length_report_m(
        solver.settings.results.report.surface_integrals.facet_min,
        reports_dir / "chamber_x_max.txt",
        surface_names=["wall_throat"],
        report_of="x-coordinate",
    )


def _collect_reports(
    solver: Any,
    output_dir: Path,
    chamber_x_max: float,
    tcomb: float,
    ambient_pressure: float,
    pressure_reference: float,
    thrust_axis: str,
    exit_to_throat_area_ratio: float,
    cstar_reference: float,
) -> dict[str, float]:
    report_path = output_dir / "metrics_reports.csv"
    reports_dir = output_dir / "fluent_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    if report_path.exists():
        report_path.unlink()

    si = solver.settings.results.report.surface_integrals
    vi = solver.settings.results.report.volume_integrals

    _define_custom_field_function(
        solver,
        f"cff_thrust_pressure_{thrust_axis}",
        f"pressure + {pressure_reference:.17g} - {ambient_pressure:.17g}",
    )
    mdot_outlet_report = _run_report(
        si.mass_flow_rate,
        reports_dir / "mdot_outlet.txt",
        surface_names=[OUTLET_ZONE],
    )
    outlet_axis_velocity_mass_avg = _run_report(
        si.mass_weighted_avg,
        reports_dir / f"outlet_{thrust_axis}_velocity_mass_avg.txt",
        surface_names=[OUTLET_ZONE],
        report_of=f"{thrust_axis}-velocity",
    )
    f_momentum = abs(mdot_outlet_report) * abs(outlet_axis_velocity_mass_avg)
    outlet_area = _run_report(
        si.area,
        reports_dir / "outlet_area.txt",
        surface_names=[OUTLET_ZONE],
    )

    report_values = {
        "mdot_oxidizer": _run_report(
            si.mass_flow_rate,
            reports_dir / "mdot_oxidizer.txt",
            surface_names=[INLET_OXIDIZER],
        ),
        "mdot_fuel": _run_report(
            si.mass_flow_rate,
            reports_dir / "mdot_fuel.txt",
            surface_names=[INLET_FUEL],
        ),
        "mdot_outlet": mdot_outlet_report,
        "qdot_actual": _run_report(
            vi.volume_integral,
            reports_dir / "qdot_actual.txt",
            cell_zones=[FLUID_ZONE],
            cell_function="heat-release-rate",
        ),
        "outlet_axis_velocity_mass_avg": outlet_axis_velocity_mass_avg,
        "outlet_area": outlet_area,
        "throat_area": outlet_area / exit_to_throat_area_ratio,
        "exit_to_throat_area_ratio": exit_to_throat_area_ratio,
        "cstar_reference": cstar_reference,
        "F_momentum": f_momentum,
        "F_pressure": _run_report(
            si.integral,
            reports_dir / "F_pressure.txt",
            surface_names=[OUTLET_ZONE],
            report_of=f"cff_thrust_pressure_{thrust_axis}",
        ),
        "chamber_pressure_gauge": _run_report(
            si.area_weighted_avg,
            reports_dir / "chamber_wall_avg_pressure.txt",
            surface_names=["wall_chamber"],
            report_of="pressure",
        ),
        "wall_area": _run_report(
            si.area,
            reports_dir / "wall_side_area.txt",
            surface_names=list(SIDEWALL_ZONES),
        ),
        "Twall_total": _run_report(
            si.area_weighted_avg,
            reports_dir / "wall_side_avg_temp.txt",
            surface_names=list(SIDEWALL_ZONES),
            report_of="temperature",
        ),
        "Tmax_sidewall": _run_report(
            si.facet_max,
            reports_dir / "wall_side_max_temp.txt",
            surface_names=list(SIDEWALL_ZONES),
            report_of="temperature",
        ),
        "chamber_x_max": chamber_x_max,
        "pressure_reference": pressure_reference,
    }
    report_values["chamber_wall_pressure_abs"] = report_values["chamber_pressure_gauge"] + pressure_reference

    zone = FLUID_ZONE
    hot_condition = f"StaticTemperature > {tcomb:g} [K]"
    chamber_condition = f"Position.x <= {chamber_x_max:.17g} [m]"
    phi = "((1-MeanMixtureFraction)/(MeanMixtureFraction+1e-12))/4"
    named_expressions = solver.settings.setup.named_expressions
    phi2 = f"({phi})*({phi})"
    expressions = {
        "phi_hot_volume": f"Sum(IF({hot_condition}, IF({chamber_condition}, 1, 0), 0), ['{zone}'], Weight=\"Volume\")",
        "phi_sum": f"Sum(IF({hot_condition}, IF({chamber_condition}, {phi}, 0), 0), ['{zone}'], Weight=\"Volume\")",
        "phi2_sum": f"Sum(IF({hot_condition}, IF({chamber_condition}, {phi2}, 0), 0), ['{zone}'], Weight=\"Volume\")",
        "chamber_volume": f"Sum(IF({chamber_condition}, 1, 0), ['{zone}'], Weight=\"Volume\")",
        "chamber_abs_pressure_sum": f"Sum(IF({chamber_condition}, AbsolutePressure, 0 [Pa]), ['{zone}'], Weight=\"Volume\")",
    }
    for name, definition in expressions.items():
        for old_name in list(named_expressions.get_object_names()):
            named_expressions.delete(old_name)
        _define_expression(named_expressions, name, definition)
        named_expressions.compute()
    expression_values = _parse_expression_values(_find_latest_transcript(output_dir / "run"), set(expressions))
    report_values.update(expression_values)
    chamber_volume = report_values["chamber_volume"]
    report_values["chamber_pressure_abs"] = (
        report_values["chamber_abs_pressure_sum"] / chamber_volume
        if chamber_volume > 0
        else float("nan")
    )

    for index, (name, value) in enumerate(report_values.items()):
        _write_report_value(report_path, name, value, append=index > 0)
    return report_values


def main() -> None:
    args = parse_args()
    if args.processor_count <= 0:
        raise ValueError("--processor-count must be positive")
    output_dir = args.output_dir
    run_dir = output_dir / "run"
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    solver = pyfluent.launch_fluent(
        product_version=241,
        dimension=3,
        precision="double",
        processor_count=args.processor_count,
        ui_mode="no_gui",
        cwd=str(run_dir),
        fluent_path=str(args.fluent_path),
        start_transcript=True,
        start_timeout=180,
    )
    try:
        solver.settings.file.read_case_data(file_name=str(args.case_data))
        chamber_x_max = (
            args.chamber_x_max
            if args.chamber_x_max is not None
            else _infer_chamber_x_max(solver, output_dir / "fluent_reports")
        )
        _collect_reports(
            solver,
            output_dir,
            chamber_x_max,
            args.tcomb,
            args.ambient_pressure,
            args.pressure_reference,
            args.thrust_axis,
            args.exit_to_throat_area_ratio,
            args.cstar_reference,
        )
    finally:
        solver.exit()

    compute_module = _load_compute_module(args.compute_script)
    config_name = args.config_name or _default_config_name(args.case_data)
    metrics = compute_module.compute_metrics(
        reports_path=output_dir / "metrics_reports.csv",
        exit_surface_path=None,
        chamber_cells_path=None,
        wall_faces_path=None,
        ambient_pressure=args.ambient_pressure,
        tcomb=args.tcomb,
        chamber_x_max=None,
        config_id=args.config_id,
    )
    compute_module._write_metrics(output_dir / f"{config_name}.csv", metrics)
    compute_module._write_metrics(output_dir / "metrics_summary.csv", metrics)


if __name__ == "__main__":
    main()
