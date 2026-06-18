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
