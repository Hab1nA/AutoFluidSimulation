from __future__ import annotations

import csv
import importlib.util
import math
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).parents[1]
    / "executor"
    / "remote_scripts"
    / "compute_metrics_gen4.py"
)
JOURNAL_PATH = (
    Path(__file__).parents[1]
    / "executor"
    / "remote_scripts"
    / "metrics_export_gen4.jou"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("compute_metrics_gen4_under_test", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_compute_metrics_from_export_tables(tmp_path: Path) -> None:
    module = _load_module()
    reports = tmp_path / "reports.csv"
    _write_csv(
        reports,
        [
            {"name": "mdot_oxidizer", "value": 2.0},
            {"name": "mdot_fuel", "value": 1.0},
            {"name": "mdot_outlet", "value": -3.0},
            {"name": "qdot_actual", "value": 25_000_000.0},
        ],
    )
    exit_surface = tmp_path / "exit.csv"
    _write_csv(
        exit_surface,
        [
            {"density": 2.0, "x_velocity": 100.0, "pressure": 150_000.0, "x_face_area": 0.01},
            {"density": 1.0, "x_velocity": 200.0, "pressure": 120_000.0, "x_face_area": 0.02},
        ],
    )
    chamber_cells = tmp_path / "chamber.csv"
    _write_csv(
        chamber_cells,
        [
            {"temperature": 1200.0, "fmean": 0.2, "cell_volume": 1.0},
            {"temperature": 900.0, "fmean": 0.5, "cell_volume": 10.0},
            {"temperature": 1600.0, "fmean": 0.25, "cell_volume": 3.0},
        ],
    )
    wall_faces = tmp_path / "wall.csv"
    _write_csv(
        wall_faces,
        [
            {"zone": "wall_chamber", "temperature": 1000.0, "face_area_magnitude": 1.0},
            {"zone": "wall_nozzle", "temperature": 1200.0, "face_area_magnitude": 2.0},
            {"zone": "wall_throat", "temperature": 1500.0, "face_area_magnitude": 1.0},
        ],
    )

    metrics = module.compute_metrics(
        reports_path=reports,
        exit_surface_path=exit_surface,
        chamber_cells_path=chamber_cells,
        wall_faces_path=wall_faces,
        ambient_pressure=100_000.0,
        tcomb=1000.0,
    )

    assert metrics["mdot_total"] == 3.0
    assert metrics["mass_imbalance"] == 0.0
    assert metrics["F_momentum"] == 1000.0
    assert metrics["F_pressure"] == 900.0
    assert metrics["F_total"] == 1900.0
    assert metrics["Isp"] == 1900.0 / (3.0 * 9.80665)
    assert metrics["eta_c"] == 0.5
    assert metrics["phi_mean"] == 0.8125
    assert math.isclose(metrics["phi_std"], 0.10825317547305482)
    assert metrics["Twall_total"] == 1225.0
    assert metrics["Tmax_throat"] == 1500.0
    assert metrics["Tmax_all"] == 1500.0


def test_cli_writes_metrics_summary(tmp_path: Path, monkeypatch) -> None:
    module = _load_module()
    reports = tmp_path / "reports.csv"
    exit_surface = tmp_path / "exit.csv"
    chamber_cells = tmp_path / "chamber.csv"
    wall_faces = tmp_path / "wall.csv"
    output = tmp_path / "metrics_summary.csv"
    _write_csv(
        reports,
        [
            {"name": "mdot_oxidizer", "value": 2.0},
            {"name": "mdot_fuel", "value": 1.0},
            {"name": "mdot_outlet", "value": -3.0},
            {"name": "qdot_actual", "value": 25_000_000.0},
        ],
    )
    _write_csv(
        exit_surface,
        [{"density": 1.0, "x_velocity": 100.0, "pressure": 100_000.0, "x_face_area": 0.1}],
    )
    _write_csv(chamber_cells, [{"temperature": 1200.0, "fmean": 0.2, "cell_volume": 1.0}])
    _write_csv(
        wall_faces,
        [{"zone": "wall_throat", "temperature": 1500.0, "face_area_magnitude": 1.0}],
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "compute_metrics_gen4.py",
            "--reports",
            str(reports),
            "--exit-surface",
            str(exit_surface),
            "--chamber-cells",
            str(chamber_cells),
            "--wall-faces",
            str(wall_faces),
            "--output",
            str(output),
            "--ambient-pressure",
            "100000",
        ],
    )

    module.main()

    rows = _read_rows(output)
    assert len(rows) == 1
    assert float(rows[0]["eta_c"]) == 0.5


def test_journal_template_uses_confirmed_zone_and_field_names() -> None:
    text = JOURNAL_PATH.read_text(encoding="utf-8")

    for expected in [
        '/file/set-tui-version "24.1"',
        "inlet_oxidizer",
        "inlet_fuel",
        "outlet",
        "s------6.5076",
        "wall_chamber",
        "wall_nozzle",
        "wall_throat",
        "heat-release-rate",
        "fmean",
        "cell-volume",
    ]:
        assert expected in text
    assert (
        "/report/surface-integrals/area-weighted-avg "
        "wall_chamber wall_nozzle wall_throat wall_top wall_gap s------6 () temperature"
    ) in text
    assert "/report/volume-integrals/volume-integral s------6.5076 () heat-release-rate" in text
