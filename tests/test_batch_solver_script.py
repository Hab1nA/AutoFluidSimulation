from __future__ import annotations

import argparse
import json
import importlib.util
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest


SCRIPT_PATH = (
    Path(__file__).parents[1]
    / "executor"
    / "remote_scripts"
    / "batch_solver_gen4.py"
)


def _load_batch_solver_module(monkeypatch, launch_fluent):
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

    spec = importlib.util.spec_from_file_location("batch_solver_gen4_under_test", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_args(tmp_path: Path, config_id: int = 7) -> argparse.Namespace:
    scripts_dir = tmp_path / "scripts"
    msh_dir = tmp_path / "msh"
    output_dir = tmp_path / "result"
    anim_dir = tmp_path / "animation"
    working_dir = tmp_path / "working"
    working_dir_t = tmp_path / "working" / "animation-t"
    working_dir_v = tmp_path / "working" / "animation-v"
    mpi_bin_dir = tmp_path / "mpi" / "bin"
    scripts_dir.mkdir()
    msh_dir.mkdir()
    mpi_bin_dir.mkdir(parents=True)
    journal_path = scripts_dir / "solver_gen4.jou"
    post_journal_path = scripts_dir / "solver_post_gen4.jou"
    journal_path.write_text("/file/set-tui-version \"24.1\"", encoding="utf-8")
    post_journal_path.write_text("/file/set-tui-version \"24.1\"", encoding="utf-8")
    (msh_dir / f"model_gen4_{config_id}.msh.h5").write_text("mesh", encoding="utf-8")
    return argparse.Namespace(
        config_id=config_id,
        mpi_bin_dir=str(mpi_bin_dir),
        journal_path=str(journal_path),
        post_journal_path=str(post_journal_path),
        msh_dir=str(msh_dir),
        output_dir=str(output_dir),
        anim_dir=str(anim_dir),
        working_dir=str(working_dir),
        working_dir_t=str(working_dir_t),
        working_dir_v=str(working_dir_v),
        processor_count=64,
        iterate_count=10,
        progress_file=None,
    )


class _SuccessfulSolverSession:
    def __init__(self) -> None:
        self.exit_calls = 0
        self.read_mesh_calls: list[str] = []
        self.read_case_calls: list[str] = []
        self.read_journal_calls: list[str] = []
        self.iterate_calls: list[int] = []
        self.write_case_data_calls: list[str] = []
        self.force_exit_calls = 0
        self.tui = types.SimpleNamespace(
            file=types.SimpleNamespace(
                read_mesh=self._read_mesh,
                read_case=self._read_case,
                read_journal=self._read_journal,
                write_case_data=self._write_case_data,
            ),
            solve=types.SimpleNamespace(iterate=self._iterate),
        )

    def is_server_healthy(self) -> bool:
        return True

    def _read_mesh(self, path: str) -> None:
        self.read_mesh_calls.append(path)

    def _read_case(self, path: str) -> None:
        self.read_case_calls.append(path)

    def _read_journal(self, path: str) -> None:
        self.read_journal_calls.append(path)

    def _iterate(self, count: int) -> None:
        self.iterate_calls.append(count)

    def _write_case_data(self, path: str) -> None:
        self.write_case_data_calls.append(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("case", encoding="utf-8")

    def exit(self) -> None:
        self.exit_calls += 1

    def force_exit(self) -> None:
        self.force_exit_calls += 1


class _ExitFailureSolverSession(_SuccessfulSolverSession):
    def exit(self) -> None:
        self.exit_calls += 1
        raise RuntimeError("exit failed")


def test_missing_mesh_is_rejected_before_fluent_launch(tmp_path, monkeypatch):
    def fail_if_launched(**kwargs: Any):
        raise AssertionError("Fluent must not launch when the mesh input is missing")

    module = _load_batch_solver_module(monkeypatch, fail_if_launched)
    args = _make_args(tmp_path)
    Path(args.msh_dir, f"model_gen4_{args.config_id}.msh.h5").unlink()
    monkeypatch.setattr(module, "parse_args", lambda: args)

    with pytest.raises(FileNotFoundError, match="网格文件不存在"):
        module.main()


def test_invalid_processor_count_is_rejected_before_fluent_launch(tmp_path, monkeypatch):
    def fail_if_launched(**kwargs: Any):
        raise AssertionError("Fluent must not launch with invalid processor count")

    module = _load_batch_solver_module(monkeypatch, fail_if_launched)
    args = _make_args(tmp_path)
    args.processor_count = 0
    monkeypatch.setattr(module, "parse_args", lambda: args)

    with pytest.raises(ValueError, match="--processor-count"):
        module.main()


def test_invalid_iterate_count_is_rejected_before_fluent_launch(tmp_path, monkeypatch):
    def fail_if_launched(**kwargs: Any):
        raise AssertionError("Fluent must not launch with invalid iterate count")

    module = _load_batch_solver_module(monkeypatch, fail_if_launched)
    args = _make_args(tmp_path)
    args.iterate_count = 0
    monkeypatch.setattr(module, "parse_args", lambda: args)

    with pytest.raises(ValueError, match="--iterate-count"):
        module.main()


def test_mpi_pin_list_matches_configured_processor_count(tmp_path, monkeypatch):
    module = _load_batch_solver_module(monkeypatch, lambda **kwargs: None)
    mpi_bin_dir = tmp_path / "mpi" / "bin"
    mpi_bin_dir.mkdir(parents=True)
    monkeypatch.setattr(module.os, "cpu_count", lambda: 128)

    module.setup_mpi_environment(str(mpi_bin_dir), 64)
    assert os.environ["I_MPI_PIN_PROCESSOR_LIST"] == "64-127"

    module.setup_mpi_environment(str(mpi_bin_dir), 128)
    assert os.environ["I_MPI_PIN_PROCESSOR_LIST"] == "0-127"


def test_mpi_rejects_processor_count_larger_than_machine(tmp_path, monkeypatch):
    module = _load_batch_solver_module(monkeypatch, lambda **kwargs: None)
    mpi_bin_dir = tmp_path / "mpi" / "bin"
    mpi_bin_dir.mkdir(parents=True)
    monkeypatch.setattr(module.os, "cpu_count", lambda: 64)

    with pytest.raises(ValueError, match="不能超过本机逻辑处理器数量"):
        module.setup_mpi_environment(str(mpi_bin_dir), 128)


def test_parse_remaining_time_line_extracts_seconds_and_iteration(monkeypatch):
    module = _load_batch_solver_module(monkeypatch, lambda **kwargs: None)

    progress = module._parse_remaining_time_line(
        "iter 350 residuals ... estimated time remaining: 1:23:45",
        total_iter=1000,
        config_id=5,
    )

    assert progress == {
        "config_name": 5,
        "current_iter": 350,
        "total_iter": 1000,
        "remaining_sec": 5025.0,
        "raw_line": "iter 350 residuals ... estimated time remaining: 1:23:45",
    }


def test_parse_remaining_time_line_accepts_mm_ss(monkeypatch):
    module = _load_batch_solver_module(monkeypatch, lambda **kwargs: None)

    progress = module._parse_remaining_time_line(
        "solver update: remaining time: 02:05",
        total_iter=10,
        config_id=7,
    )

    assert progress is not None
    assert progress["current_iter"] is None
    assert progress["remaining_sec"] == 125.0


def test_write_progress_file_uses_atomic_replace(tmp_path, monkeypatch):
    module = _load_batch_solver_module(monkeypatch, lambda **kwargs: None)
    progress_file = tmp_path / "solver_progress_5.json"
    payload = {
        "config_name": 5,
        "current_iter": 3,
        "total_iter": 10,
        "remaining_sec": 15.0,
        "raw_line": "time remaining: 00:15",
    }

    module._write_progress_file(str(progress_file), payload)

    stored = json.loads(progress_file.read_text(encoding="utf-8"))
    assert stored["config_name"] == 5
    assert stored["remaining_sec"] == 15.0
    assert isinstance(stored["updated_at"], float)
    assert not progress_file.with_suffix(".json.tmp").exists()


def test_launch_uses_configured_processor_count_and_reads_mesh(tmp_path, monkeypatch):
    launch_kwargs: dict[str, Any] = {}
    session = _SuccessfulSolverSession()

    def launch_fluent(**kwargs: Any):
        launch_kwargs.update(kwargs)
        return session

    module = _load_batch_solver_module(monkeypatch, launch_fluent)
    args = _make_args(tmp_path)
    args.processor_count = 96
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.os, "cpu_count", lambda: 128)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    module.main()

    assert launch_kwargs["processor_count"] == 96
    assert launch_kwargs["ui_mode"] == "gui"
    assert launch_kwargs["start_watchdog"] is False
    assert launch_kwargs["cwd"] == args.working_dir
    assert os.environ["I_MPI_PIN_PROCESSOR_LIST"] == "32-127"
    assert session.read_mesh_calls == [
        str(Path(args.msh_dir, f"model_gen4_{args.config_id}.msh.h5"))
    ]
    assert session.read_case_calls == []
    assert session.iterate_calls == [10]
    assert session.exit_calls == 1


def test_exit_failure_tries_force_exit(tmp_path, monkeypatch):
    session = _ExitFailureSolverSession()
    module = _load_batch_solver_module(monkeypatch, lambda **kwargs: session)
    args = _make_args(tmp_path)
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.os, "cpu_count", lambda: 128)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    module.main()

    assert session.exit_calls == 1
    assert session.force_exit_calls == 1


def test_mesh_read_falls_back_to_read_case_when_needed(tmp_path, monkeypatch):
    session = _SuccessfulSolverSession()
    delattr(session.tui.file, "read_mesh")
    module = _load_batch_solver_module(monkeypatch, lambda **kwargs: session)
    args = _make_args(tmp_path)
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.os, "cpu_count", lambda: 128)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    module.main()

    assert session.read_case_calls == [
        str(Path(args.msh_dir, f"model_gen4_{args.config_id}.msh.h5"))
    ]


def test_journal_failure_does_not_print_success_message(tmp_path, monkeypatch, capsys):
    class _JournalFailureSession(_SuccessfulSolverSession):
        def _read_journal(self, path: str) -> None:
            raise RuntimeError("journal failed")

    session = _JournalFailureSession()
    module = _load_batch_solver_module(monkeypatch, lambda **kwargs: session)
    args = _make_args(tmp_path)
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.os, "cpu_count", lambda: 128)

    with pytest.raises(RuntimeError, match="journal failed"):
        module.main()

    assert "仿真计算完成" not in capsys.readouterr().out
    assert session.exit_calls == 1
