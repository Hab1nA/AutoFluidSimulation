from __future__ import annotations

import argparse
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest


SCRIPT_PATH = (
    Path(__file__).parents[1]
    / "executor"
    / "remote_scripts"
    / "batch_meshing_gen4.py"
)


def _load_batch_meshing_module(monkeypatch, launch_fluent):
    ansys_module = types.ModuleType("ansys")
    fluent_module = types.ModuleType("ansys.fluent")
    core_module = types.ModuleType("ansys.fluent.core")
    core_module.launch_fluent = launch_fluent
    core_module.FluentMode = types.SimpleNamespace(MESHING="meshing")
    core_module.Precision = types.SimpleNamespace(DOUBLE="double")
    core_module.FluentVersion = types.SimpleNamespace(v241="24.1")
    ansys_module.fluent = fluent_module
    fluent_module.core = core_module

    monkeypatch.setitem(sys.modules, "ansys", ansys_module)
    monkeypatch.setitem(sys.modules, "ansys.fluent", fluent_module)
    monkeypatch.setitem(sys.modules, "ansys.fluent.core", core_module)

    spec = importlib.util.spec_from_file_location("batch_meshing_gen4_under_test", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_args(tmp_path: Path, config_id: int = 7) -> argparse.Namespace:
    scripts_dir = tmp_path / "scripts"
    scdoc_dir = tmp_path / "scdoc"
    output_dir = tmp_path / "msh"
    working_dir = tmp_path / "working"
    mpi_bin_dir = tmp_path / "mpi" / "bin"
    scripts_dir.mkdir()
    scdoc_dir.mkdir()
    output_dir.mkdir()
    working_dir.mkdir()
    mpi_bin_dir.mkdir(parents=True)
    workflow_path = scripts_dir / "meshing_gen4.wft"
    journal_path = scripts_dir / "meshing_gen4.jou"
    workflow_path.write_text('{"config": "{config}"}', encoding="utf-8")
    journal_path.write_text("meshing_gen4.wft", encoding="utf-8")
    (scdoc_dir / f"model_gen4_{config_id}.scdoc").write_text("scdoc", encoding="utf-8")
    return argparse.Namespace(
        config_id=config_id,
        mpi_bin_dir=str(mpi_bin_dir),
        workflow_path=str(workflow_path),
        journal_path=str(journal_path),
        scdoc_dir=str(scdoc_dir),
        output_dir=str(output_dir),
        working_dir=str(working_dir),
        processor_count=1,
    )


class _SuccessfulMeshingSession:
    def __init__(self) -> None:
        self.exit_calls = 0
        self.read_journal_calls: list[str] = []
        self.write_mesh_calls: list[str] = []
        self.tui = types.SimpleNamespace(
            file=types.SimpleNamespace(
                read_journal=self._read_journal,
                write_mesh=self._write_mesh,
            )
        )

    def is_server_healthy(self) -> bool:
        return True

    def _read_journal(self, path: str) -> None:
        self.read_journal_calls.append(path)

    def _write_mesh(self, path: str) -> None:
        self.write_mesh_calls.append(path)

    def exit(self) -> None:
        self.exit_calls += 1


def test_missing_scdoc_is_rejected_before_fluent_launch(tmp_path, monkeypatch):
    def fail_if_launched(**kwargs: Any):
        raise AssertionError("Fluent must not launch when the SCDOC input is missing")

    module = _load_batch_meshing_module(monkeypatch, fail_if_launched)
    args = _make_args(tmp_path)
    Path(args.scdoc_dir, f"model_gen4_{args.config_id}.scdoc").unlink()
    monkeypatch.setattr(module, "parse_args", lambda: args)

    with pytest.raises(FileNotFoundError, match="SCDOC 输入文件不存在"):
        module.main()


def test_unhealthy_server_is_rejected_before_journal_execution(tmp_path, monkeypatch):
    journal_calls: list[str] = []

    class _UnhealthyMeshingSession:
        def __init__(self) -> None:
            self.exit_calls = 0
            self.tui = types.SimpleNamespace(
                file=types.SimpleNamespace(read_journal=journal_calls.append)
            )

        def is_server_healthy(self) -> bool:
            return False

        def exit(self) -> None:
            self.exit_calls += 1

    session = _UnhealthyMeshingSession()
    module = _load_batch_meshing_module(monkeypatch, lambda **kwargs: session)
    args = _make_args(tmp_path)
    monkeypatch.setattr(module, "parse_args", lambda: args)

    with pytest.raises(RuntimeError, match="Fluent server 健康检查失败"):
        module.main()

    assert journal_calls == []
    assert session.exit_calls == 1


def test_journal_failure_does_not_print_success_message(tmp_path, monkeypatch, capsys):
    class _JournalFailureSession(_SuccessfulMeshingSession):
        def _read_journal(self, path: str) -> None:
            raise RuntimeError("grpc unavailable")

    session = _JournalFailureSession()
    module = _load_batch_meshing_module(monkeypatch, lambda **kwargs: session)
    args = _make_args(tmp_path)
    monkeypatch.setattr(module, "parse_args", lambda: args)

    with pytest.raises(RuntimeError, match="grpc unavailable"):
        module.main()

    assert "网格生成完成" not in capsys.readouterr().out
    assert session.exit_calls == 1


def test_launch_does_not_force_localized_fluent_gui(tmp_path, monkeypatch):
    launch_kwargs: dict[str, Any] = {}
    session = _SuccessfulMeshingSession()

    def launch_fluent(**kwargs: Any):
        launch_kwargs.update(kwargs)
        return session

    module = _load_batch_meshing_module(monkeypatch, launch_fluent)
    args = _make_args(tmp_path)
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    module.main()

    assert "env" not in launch_kwargs
    assert launch_kwargs["ui_mode"] == "gui"
    assert launch_kwargs["start_watchdog"] is False


def test_launch_uses_configured_working_dir(tmp_path, monkeypatch):
    launch_kwargs: dict[str, Any] = {}
    session = _SuccessfulMeshingSession()

    def launch_fluent(**kwargs: Any):
        launch_kwargs.update(kwargs)
        return session

    module = _load_batch_meshing_module(monkeypatch, launch_fluent)
    args = _make_args(tmp_path)
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    module.main()

    assert launch_kwargs["cwd"] == args.working_dir


def test_launch_uses_configured_processor_count(tmp_path, monkeypatch):
    launch_kwargs: dict[str, Any] = {}
    session = _SuccessfulMeshingSession()

    def launch_fluent(**kwargs: Any):
        launch_kwargs.update(kwargs)
        return session

    module = _load_batch_meshing_module(monkeypatch, launch_fluent)
    args = _make_args(tmp_path)
    args.processor_count = 4
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    module.main()

    assert launch_kwargs["processor_count"] == 4


def test_invalid_processor_count_is_rejected(tmp_path, monkeypatch):
    def fail_if_launched(**kwargs: Any):
        raise AssertionError("Fluent must not launch with invalid processor count")

    module = _load_batch_meshing_module(monkeypatch, fail_if_launched)
    args = _make_args(tmp_path)
    args.processor_count = 0
    monkeypatch.setattr(module, "parse_args", lambda: args)

    with pytest.raises(ValueError, match="--processor-count"):
        module.main()
