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
POSTPROCESS_SCRIPT_PATH = (
    Path(__file__).parents[1]
    / "executor"
    / "remote_scripts"
    / "postprocess_metrics_gen4.py"
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


def _load_postprocess_module(monkeypatch):
    ansys_module = type(importlib.util)("ansys")
    fluent_module = type(importlib.util)("ansys.fluent")
    core_module = type(importlib.util)("ansys.fluent.core")
    ansys_module.fluent = fluent_module
    fluent_module.core = core_module
    monkeypatch.setitem(__import__("sys").modules, "ansys", ansys_module)
    monkeypatch.setitem(__import__("sys").modules, "ansys.fluent", fluent_module)
    monkeypatch.setitem(__import__("sys").modules, "ansys.fluent.core", core_module)

    spec = importlib.util.spec_from_file_location("postprocess_metrics_gen4_under_test", POSTPROCESS_SCRIPT_PATH)
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
            {"name": "chamber_abs_pressure_sum", "value": 7_200_000.0},
            {"name": "chamber_volume", "value": 4.0},
            {"name": "throat_area", "value": 0.003},
            {"name": "cstar_reference", "value": 1800.0},
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
            {"temperature": 1200.0, "fmean": 0.2, "cell_volume": 1.0, "x_coordinate": -0.1},
            {"temperature": 900.0, "fmean": 0.5, "cell_volume": 10.0, "x_coordinate": -0.1},
            {"temperature": 1600.0, "fmean": 0.25, "cell_volume": 3.0, "x_coordinate": 0.2},
        ],
    )
    wall_faces = tmp_path / "wall.csv"
    _write_csv(
        wall_faces,
        [
            {"zone": "wall_chamber", "temperature": 1000.0, "face_area_magnitude": 1.0},
            {"zone": "wall_nozzle", "temperature": 1200.0, "face_area_magnitude": 2.0},
            {"zone": "wall_throat", "temperature": 1500.0, "face_area_magnitude": 1.0},
            {"zone": "wall_gap", "temperature": 3000.0, "face_area_magnitude": 100.0},
        ],
    )

    metrics = module.compute_metrics(
        reports_path=reports,
        exit_surface_path=exit_surface,
        chamber_cells_path=chamber_cells,
        wall_faces_path=wall_faces,
        ambient_pressure=100_000.0,
        tcomb=1000.0,
        chamber_x_max=0.0,
        config_id=7,
    )

    assert metrics["config_id"] == 7
    assert metrics["mdot_total"] == 3.0
    assert metrics["mass_imbalance"] == 0.0
    assert metrics["F_momentum"] == 1000.0
    assert metrics["F_pressure"] == 900.0
    assert metrics["F_total"] == 1900.0
    assert metrics["Isp"] == 1900.0 / (3.0 * 9.80665)
    assert "eta_c" not in metrics
    assert metrics["chamber_pressure_abs"] == 1_800_000.0
    assert metrics["cstar_actual"] == 1800.0
    assert metrics["cstar_efficiency"] == 1.0
    assert metrics["phi_mean"] == 1.0
    assert metrics["phi_std"] == 0.0
    assert metrics["Twall_total"] == 1225.0
    assert metrics["Tmax_sidewall"] == 1500.0
    assert "Tmax_all" not in metrics


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
            {"name": "chamber_pressure_abs", "value": 1_800_000.0},
            {"name": "throat_area", "value": 0.003},
            {"name": "cstar_reference", "value": 1800.0},
        ],
    )
    _write_csv(
        exit_surface,
        [{"density": 1.0, "x_velocity": 100.0, "pressure": 100_000.0, "x_face_area": 0.1}],
    )
    _write_csv(chamber_cells, [{"temperature": 1200.0, "fmean": 0.2, "cell_volume": 1.0, "x_coordinate": -0.1}])
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
            "--chamber-x-max",
            "0.0",
            "--config-id",
            "3",
        ],
    )

    module.main()

    rows = _read_rows(output)
    assert len(rows) == 1
    assert rows[0]["config_id"] == "3"
    assert "config_name" not in rows[0]
    assert "eta_c" not in rows[0]
    assert float(rows[0]["cstar_efficiency"]) == 1.0


def test_compute_metrics_accepts_fluent_phi_integrals(tmp_path: Path) -> None:
    module = _load_module()
    reports = tmp_path / "reports.csv"
    _write_csv(
        reports,
        [
            {"name": "mdot_oxidizer", "value": 2.0},
            {"name": "mdot_fuel", "value": 1.0},
            {"name": "mdot_outlet", "value": -3.0},
            {"name": "qdot_actual", "value": 25_000_000.0},
            {"name": "chamber_pressure_abs", "value": 1_800_000.0},
            {"name": "throat_area", "value": 0.003},
            {"name": "cstar_reference", "value": 1800.0},
            {"name": "phi_hot_volume", "value": 4.0},
            {"name": "phi_sum", "value": 6.0},
            {"name": "phi2_sum", "value": 10.0},
        ],
    )
    exit_surface = tmp_path / "exit.csv"
    _write_csv(
        exit_surface,
        [{"density": 1.0, "x_velocity": 100.0, "pressure": 100_000.0, "x_face_area": 0.1}],
    )
    wall_faces = tmp_path / "wall.csv"
    _write_csv(
        wall_faces,
        [{"zone": "wall_throat", "temperature": 1500.0, "face_area_magnitude": 1.0}],
    )

    metrics = module.compute_metrics(
        reports_path=reports,
        exit_surface_path=exit_surface,
        chamber_cells_path=None,
        wall_faces_path=wall_faces,
        ambient_pressure=100_000.0,
        tcomb=1000.0,
    )

    assert metrics["phi_mean"] == 1.5
    assert metrics["phi_std"] == 0.5
    assert metrics["hot_volume"] == 4.0


def test_compute_metrics_from_fluent_integral_reports_only(tmp_path: Path) -> None:
    module = _load_module()
    reports = tmp_path / "reports.csv"
    _write_csv(
        reports,
        [
            {"name": "mdot_oxidizer", "value": 2.0},
            {"name": "mdot_fuel", "value": 1.0},
            {"name": "mdot_outlet", "value": -3.0},
            {"name": "qdot_actual", "value": 25_000_000.0},
            {"name": "F_momentum", "value": 1000.0},
            {"name": "F_pressure", "value": 900.0},
            {"name": "chamber_pressure_abs", "value": 1_800_000.0},
            {"name": "throat_area", "value": 0.003},
            {"name": "cstar_reference", "value": 1800.0},
            {"name": "phi_hot_volume", "value": 4.0},
            {"name": "phi_sum", "value": 6.0},
            {"name": "phi2_sum", "value": 10.0},
            {"name": "wall_area", "value": 4.0},
            {"name": "Twall_total", "value": 1225.0},
            {"name": "Tmax_sidewall", "value": 1600.0},
        ],
    )

    metrics = module.compute_metrics(
        reports_path=reports,
        exit_surface_path=None,
        chamber_cells_path=None,
        wall_faces_path=None,
        ambient_pressure=100_000.0,
        tcomb=1000.0,
    )

    assert metrics["F_total"] == 1900.0
    assert metrics["Isp"] == 1900.0 / (3.0 * 9.80665)
    assert "eta_c" not in metrics
    assert metrics["cstar_efficiency"] == 1.0
    assert metrics["phi_mean"] == 1.5
    assert metrics["phi_std"] == 0.5
    assert metrics["Twall_total"] == 1225.0
    assert metrics["Tmax_sidewall"] == 1600.0


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
        "x-coordinate",
    ]:
        assert expected in text
    assert (
        "/report/surface-integrals/area-weighted-avg "
        "wall_chamber wall_nozzle wall_throat () temperature"
    ) in text
    assert "/report/volume-integrals/volume-integral s------6.5076 () heat-release-rate" in text


def test_parse_report_file_accepts_single_zone_and_net_rows(tmp_path: Path, monkeypatch) -> None:
    module = _load_postprocess_module(monkeypatch)
    single = tmp_path / "single.txt"
    single.write_text(
        """                         "Surface Integral Report"

                  Mass Flow Rate               [kg/s]
-------------------------------- --------------------
                  inlet_oxidizer             2.598407
""",
        encoding="utf-8",
    )
    net = tmp_path / "net.txt"
    net.write_text(
        """                          "Volume Integral Report"

           Total Volume Integral
                  heat-release-rate
-------------------------------- --------------------
                   s------6.5076           1.2345E+05
                ---------------- --------------------
                             Net           1.2345E+05
""",
        encoding="utf-8",
    )

    assert module._parse_report_file(single) == 2.598407
    assert module._parse_report_file(net) == 123450.0


def test_length_report_parser_converts_mm_to_m(tmp_path: Path, monkeypatch) -> None:
    module = _load_postprocess_module(monkeypatch)
    report = tmp_path / "chamber_x_max.txt"
    report.write_text(
        """                         "Surface Integral Report"

         Minimum of Facet Values
                    X-Coordinate                 [mm]
-------------------------------- --------------------
                     wall_throat           -17.418265
""",
        encoding="utf-8",
    )

    assert module._parse_report_file(report) == -17.418265
    assert module._parse_report_unit(report) == "mm"
    assert math.isclose(
        module._parse_report_file(report) * module.LENGTH_TO_M[module._parse_report_unit(report)],
        -0.017418265,
    )


def test_default_config_name_strips_fluent_case_suffix(monkeypatch) -> None:
    module = _load_postprocess_module(monkeypatch)

    assert module._default_config_name(Path("model_gen4_12.cas.h5")) == "model_gen4_12"
    assert module._default_config_name(Path("model_gen4_12.dat.h5")) == "model_gen4_12"
    assert module._default_config_name(Path("custom.case")) == "custom"


def test_config_named_metrics_csv_is_single_row(tmp_path: Path) -> None:
    module = _load_module()
    output = tmp_path / "model_gen4_8.csv"
    metrics = {
        "config_id": 8,
        "mdot_oxidizer": 2.6,
        "mdot_fuel": 1.2,
        "mdot_total": 3.8,
        "mdot_outlet": 3.7,
        "mass_imbalance": 0.02,
        "chamber_pressure_abs": 1_900_000.0,
        "throat_area": 0.0034,
        "cstar_actual": 1730.0,
        "cstar_reference": 1830.4,
        "cstar_efficiency": 0.94,
        "F_momentum": 10_000.0,
        "F_pressure": 900.0,
        "F_total": 10_900.0,
        "Isp": 290.0,
        "Qdot_actual": 22_000_000.0,
        "Qdot_theoretical": 61_000_000.0,
        "phi_mean": 0.7,
        "phi_std": 0.85,
        "hot_volume": 0.0027,
        "wall_area": 0.16,
        "Twall_total": 2871.0,
        "Tmax_sidewall": 4369.0,
    }

    module._write_metrics(output, metrics)

    rows = _read_rows(output)
    assert len(rows) == 1
    assert output.name == "model_gen4_8.csv"
    assert list(rows[0]) == list(metrics)
    assert rows[0]["config_id"] == "8"


def test_parse_expression_values_collects_multiple_tables(tmp_path: Path, monkeypatch) -> None:
    module = _load_postprocess_module(monkeypatch)
    transcript = tmp_path / "fluent.trn"
    transcript.write_text(
        """--------------------------------------
  Expression               Value Unit
--------------------------------------
phi_hot_volume  1.5 [m^3]
--------------------------------------

--------------------------------------
  Expression               Value Unit
--------------------------------------
phi_sum  3.0 [m^3]
--------------------------------------

--------------------------------------
  Expression               Value Unit
--------------------------------------
phi2_sum  7.5 [m^3]
--------------------------------------
""",
        encoding="utf-8",
    )

    assert module._parse_expression_values(transcript, {"phi_hot_volume", "phi_sum", "phi2_sum"}) == {
        "phi_hot_volume": 1.5,
        "phi_sum": 3.0,
        "phi2_sum": 7.5,
    }
