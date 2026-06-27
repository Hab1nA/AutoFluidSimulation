from __future__ import annotations

import argparse
import importlib.util
import sys
import types
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).parents[1]
    / "executor"
    / "remote_scripts"
    / "batch_postprocess_gen4.py"
)


def _load_batch_postprocess_module(monkeypatch, launch_fluent):
    ansys_module = types.ModuleType("ansys")
    fluent_module = types.ModuleType("ansys.fluent")
    core_module = types.ModuleType("ansys.fluent.core")
    core_module.launch_fluent = launch_fluent
    core_module.FluentMode = types.SimpleNamespace(SOLVER="solver")
    core_module.Precision = types.SimpleNamespace(DOUBLE="double")
    core_module.FluentVersion = types.SimpleNamespace(v241="24.1")
    ansys_module.fluent = fluent_module
    fluent_module.core = core_module

    monkeypatch.setitem(sys.modules, "ansys", ansys_module)
    monkeypatch.setitem(sys.modules, "ansys.fluent", fluent_module)
    monkeypatch.setitem(sys.modules, "ansys.fluent.core", core_module)

    spec = importlib.util.spec_from_file_location("batch_postprocess_gen4_under_test", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_args(tmp_path: Path, config_id: int = 7) -> argparse.Namespace:
    case_dir = tmp_path / "case"
    scripts_dir = tmp_path / "scripts"
    output_dir = tmp_path / "post"
    working_dir = tmp_path / "working"
    working_dir_t = working_dir / "animation-t"
    working_dir_v = working_dir / "animation-v"
    anim_dir = tmp_path / "animation"
    case_dir.mkdir()
    scripts_dir.mkdir()
    post_journal_path = scripts_dir / "solver_post_gen4.jou"
    extra_post_journal_path = scripts_dir / "postprocess_extra_gen4.jou"
    post_journal_path.write_text("/file/set-tui-version \"24.1\"", encoding="utf-8")
    extra_post_journal_path.write_text("/file/set-tui-version \"24.1\"", encoding="utf-8")
    (case_dir / f"model_gen4_{config_id}.cas.h5").write_text("case", encoding="utf-8")
    (case_dir / f"model_gen4_{config_id}.dat.h5").write_text("data", encoding="utf-8")
    return argparse.Namespace(
        config_id=config_id,
        case_dir=str(case_dir),
        post_journal_path=str(post_journal_path),
        extra_post_journal_path=str(extra_post_journal_path),
        postprocess_output_dir=str(output_dir),
        flag_file=str(tmp_path / "flags" / f"postprocess_done_{config_id}.txt"),
        anim_dir=str(anim_dir),
        working_dir=str(working_dir),
        working_dir_t=str(working_dir_t),
        working_dir_v=str(working_dir_v),
        fluent_path=str(tmp_path / "fluent.exe"),
        metrics_script=None,
        compute_metrics_script=None,
        metrics_output_dir=str(tmp_path / "metrics"),
        metrics_processor_count=1,
        metrics_ambient_pressure=0.0,
        metrics_pressure_reference=101325.0,
        metrics_tcomb=1000.0,
        metrics_thrust_axis="x",
        metrics_exit_to_throat_area_ratio=7.42,
        metrics_cstar_reference=1830.4,
    )


class _SuccessfulPostprocessSession:
    def __init__(self) -> None:
        self.read_case_data_calls: list[str] = []
        self.read_journal_calls: list[str] = []
        self.exit_calls = 0
        self.force_exit_calls = 0
        self.tui = types.SimpleNamespace(
            file=types.SimpleNamespace(
                read_case_data=self._read_case_data,
                read_case=self._read_case_data,
                read_journal=self._read_journal,
            )
        )

    def is_server_healthy(self) -> bool:
        return True

    def _read_case_data(self, path: str) -> None:
        self.read_case_data_calls.append(path)

    def _read_journal(self, path: str) -> None:
        self.read_journal_calls.append(path)

    def exit(self) -> None:
        self.exit_calls += 1

    def force_exit(self) -> None:
        self.force_exit_calls += 1


def test_postprocess_runs_required_and_extra_journals_then_writes_flag(tmp_path, monkeypatch):
    session = _SuccessfulPostprocessSession()
    module = _load_batch_postprocess_module(monkeypatch, lambda **kwargs: session)
    args = _make_args(tmp_path)
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    module.main()

    assert session.read_case_data_calls == [
        str(Path(args.case_dir, f"model_gen4_{args.config_id}.cas.h5"))
    ]
    assert session.read_journal_calls == [
        args.post_journal_path,
        args.extra_post_journal_path,
    ]
    assert Path(args.flag_file).read_text(encoding="utf-8").strip() == "OK"
    assert session.exit_calls == 1


def test_postprocess_skips_missing_extra_journal(tmp_path, monkeypatch):
    session = _SuccessfulPostprocessSession()
    module = _load_batch_postprocess_module(monkeypatch, lambda **kwargs: session)
    args = _make_args(tmp_path)
    Path(args.extra_post_journal_path).unlink()
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    module.main()

    assert session.read_journal_calls == [args.post_journal_path]
    assert Path(args.flag_file).exists()


def test_postprocess_does_not_write_flag_when_journal_fails(tmp_path, monkeypatch):
    class _FailingSession(_SuccessfulPostprocessSession):
        def _read_journal(self, path: str) -> None:
            raise RuntimeError("journal failed")

    session = _FailingSession()
    module = _load_batch_postprocess_module(monkeypatch, lambda **kwargs: session)
    args = _make_args(tmp_path)
    monkeypatch.setattr(module, "parse_args", lambda: args)

    with pytest.raises(RuntimeError, match="journal failed"):
        module.main()

    assert not Path(args.flag_file).exists()


def test_postprocess_skips_video_journals_when_final_animations_exist(tmp_path, monkeypatch):
    def fail_launch(**kwargs: object):
        raise AssertionError("Fluent should not be launched when final videos already exist")

    module = _load_batch_postprocess_module(monkeypatch, fail_launch)
    args = _make_args(tmp_path)
    Path(args.anim_dir).mkdir(parents=True)
    Path(args.anim_dir, f"v_gen4_{args.config_id}.mp4").write_bytes(b"v")
    Path(args.anim_dir, f"t_gen4_{args.config_id}.mp4").write_bytes(b"t")
    metrics_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(
        module,
        "_run_metrics_postprocess",
        lambda parsed_args, case_path, config_id: metrics_calls.append((case_path, config_id)),
    )

    module.main()

    assert metrics_calls == [
        (str(Path(args.case_dir, f"model_gen4_{args.config_id}.cas.h5")), args.config_id)
    ]
    assert Path(args.flag_file).read_text(encoding="utf-8").strip() == "OK"


def test_postprocess_continues_video_options_failure_when_final_animations_exist(
    tmp_path,
    monkeypatch,
):
    class _VideoOptionsFailingSession(_SuccessfulPostprocessSession):
        def _read_journal(self, path: str) -> None:
            self.read_journal_calls.append(path)
            Path(self.anim_dir, f"v_gen4_{self.config_id}.mp4").write_bytes(b"v")
            Path(self.anim_dir, f"t_gen4_{self.config_id}.mp4").write_bytes(b"t")
            raise RuntimeError(
                'cx-name-to-id: cannot find widget: "Video Options*Table1*IntegerEntry2(FPS)"'
            )

    session = _VideoOptionsFailingSession()
    module = _load_batch_postprocess_module(monkeypatch, lambda **kwargs: session)
    args = _make_args(tmp_path)
    session.anim_dir = Path(args.anim_dir)
    session.config_id = args.config_id
    metrics_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(
        module,
        "_run_metrics_postprocess",
        lambda parsed_args, case_path, config_id: metrics_calls.append((case_path, config_id)),
    )

    module.main()

    assert session.read_journal_calls == [args.post_journal_path]
    assert metrics_calls
    assert Path(args.flag_file).exists()


def test_postprocess_launch_uses_configured_fluent_path(tmp_path, monkeypatch):
    launch_kwargs: dict[str, object] = {}
    session = _SuccessfulPostprocessSession()

    def launch_fluent(**kwargs: object):
        launch_kwargs.update(kwargs)
        return session

    module = _load_batch_postprocess_module(monkeypatch, launch_fluent)
    args = _make_args(tmp_path)
    args.fluent_path = r"D:\ANSYS Inc\v241\fluent\ntbin\win64\fluent.exe"
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    module.main()

    assert launch_kwargs["fluent_path"] == args.fluent_path


def test_postprocess_metrics_receives_configured_fluent_path(tmp_path, monkeypatch):
    module = _load_batch_postprocess_module(monkeypatch, lambda **kwargs: None)
    args = _make_args(tmp_path)
    args.metrics_script = str(tmp_path / "postprocess_metrics_gen4.py")
    args.compute_metrics_script = str(tmp_path / "compute_metrics_gen4.py")
    args.metrics_output_dir = str(tmp_path / "metrics")
    args.metrics_processor_count = 2
    args.metrics_ambient_pressure = 0.0
    args.metrics_pressure_reference = 101325.0
    args.metrics_tcomb = 1000.0
    args.metrics_thrust_axis = "x"
    args.metrics_exit_to_throat_area_ratio = 7.42
    args.metrics_cstar_reference = 1830.4
    args.fluent_path = r"D:\ANSYS Inc\v241\fluent\ntbin\win64\fluent.exe"
    Path(args.metrics_script).write_text("# metrics", encoding="utf-8")
    Path(args.compute_metrics_script).write_text("# compute", encoding="utf-8")
    case_path = str(tmp_path / "case.cas.h5")
    Path(case_path).write_text("case", encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(command: list[str], check: bool) -> None:
        commands.append(command)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module._run_metrics_postprocess(args, case_path, args.config_id)

    assert commands
    assert "--fluent-path" in commands[0]
    assert commands[0][commands[0].index("--fluent-path") + 1] == args.fluent_path


def test_metrics_reference_values_are_required_by_cli(monkeypatch):
    module = _load_batch_postprocess_module(monkeypatch, lambda **kwargs: None)
    argv = [
        "batch_postprocess_gen4.py",
        "--config-id", "1",
        "--case-dir", r"D:\case",
        "--post-journal-path", "post.jou",
        "--postprocess-output-dir", r"D:\post",
        "--flag-file", r"D:\flags\post.txt",
        "--anim-dir", r"D:\animation",
        "--working-dir", r"D:\work",
        "--working-dir-t", r"D:\work\animation-t",
        "--working-dir-v", r"D:\work\animation-v",
        "--metrics-output-dir", r"D:\metrics",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exc_info:
        module.parse_args()
    assert exc_info.value.code == 2
