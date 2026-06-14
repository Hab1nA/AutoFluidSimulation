from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from executor.sw_executor import SWExecutor
from engine.config import LOCAL_PATHS


class _FakePythoncom(types.ModuleType):
    VT_BYREF = 0x4000
    VT_DISPATCH = 9
    VT_I4 = 3

    def __init__(self) -> None:
        super().__init__("pythoncom")
        self.initialized = 0
        self.uninitialized = 0

    def CoInitialize(self) -> None:
        self.initialized += 1

    def CoUninitialize(self) -> None:
        self.uninitialized += 1

    def CoFreeUnusedLibraries(self) -> None:
        pass


class _FakeVariant:
    def __init__(self, _variant_type: int, value):
        self.value = value


@pytest.fixture
def fake_com_modules(monkeypatch):
    pythoncom = _FakePythoncom()
    win32com = types.ModuleType("win32com")
    win32com_client = types.ModuleType("win32com.client")
    win32com_client.VARIANT = _FakeVariant
    win32com.client = win32com_client

    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", win32com_client)

    return pythoncom


@pytest.fixture
def sw_paths(monkeypatch, tmp_path: Path):
    original = LOCAL_PATHS.copy()
    monkeypatch.setitem(LOCAL_PATHS, "sw_model", str(tmp_path / "model.SLDPRT"))
    monkeypatch.setitem(LOCAL_PATHS, "excel", str(tmp_path / "table.xlsx"))
    monkeypatch.setitem(LOCAL_PATHS, "step_dir", str(tmp_path / "steps"))
    yield
    LOCAL_PATHS.clear()
    LOCAL_PATHS.update(original)


def test_export_per_config_cleans_up_when_model_open_fails(
    fake_com_modules,
    sw_paths,
    monkeypatch,
):
    executor = SWExecutor(SimpleNamespace())
    app = SimpleNamespace()
    cleanup_calls = []

    monkeypatch.setattr(executor, "_connect_sw", lambda: app)
    monkeypatch.setattr(executor, "_open_sw_model", lambda *_args: None)
    monkeypatch.setattr(
        executor,
        "_disconnect_sw",
        lambda sw_app, doc, sw_model: cleanup_calls.append((sw_app, doc, sw_model)),
    )

    assert executor.export_sw_per_config(1) is False
    assert cleanup_calls == [(app, None, LOCAL_PATHS["sw_model"])]
    assert executor._cached_sw_app is None
    assert executor._cached_doc is None
    assert executor._com_initialized is False


def test_export_per_config_cleans_up_when_design_table_import_fails(
    fake_com_modules,
    sw_paths,
    monkeypatch,
):
    executor = SWExecutor(SimpleNamespace())
    app = SimpleNamespace()
    doc = SimpleNamespace()
    cleanup_calls = []

    monkeypatch.setattr(executor, "_connect_sw", lambda: app)
    monkeypatch.setattr(executor, "_open_sw_model", lambda *_args: doc)
    monkeypatch.setattr(
        executor,
        "_import_design_table_with_retry",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        executor,
        "_disconnect_sw",
        lambda sw_app, sw_doc, sw_model: cleanup_calls.append(
            (sw_app, sw_doc, sw_model)
        ),
    )

    assert executor.export_sw_per_config(1) is False
    assert cleanup_calls == [(app, doc, LOCAL_PATHS["sw_model"])]
    assert executor._cached_sw_app is None
    assert executor._cached_doc is None
    assert executor._com_initialized is False


def test_export_per_config_saveas_exception_disconnects_cached_com(
    fake_com_modules,
    sw_paths,
    monkeypatch,
):
    executor = SWExecutor(SimpleNamespace())
    app = SimpleNamespace()
    extension = SimpleNamespace()
    doc = SimpleNamespace(Extension=extension)
    cleanup_calls = []

    doc.ShowConfiguration2 = lambda _name: True
    doc.Rebuild = lambda _arg: True
    extension.Rebuild = lambda _arg: True

    def raise_saveas(*_args):
        raise RuntimeError("save failed")

    extension.SaveAs = raise_saveas
    executor._cached_sw_app = app
    executor._cached_doc = doc
    executor._com_initialized = True

    monkeypatch.setattr(
        executor,
        "_disconnect_sw",
        lambda sw_app, sw_doc, sw_model: cleanup_calls.append(
            (sw_app, sw_doc, sw_model)
        ),
    )

    assert executor.export_sw_per_config(1) is False
    assert cleanup_calls == [(app, doc, LOCAL_PATHS["sw_model"])]
    assert executor._cached_sw_app is None
    assert executor._cached_doc is None
    assert executor._com_initialized is False


def test_opendoc6_rpc_failure_terminates_sw_and_retries_once(
    fake_com_modules,
    sw_paths,
    monkeypatch,
):
    executor = SWExecutor(SimpleNamespace())
    app = SimpleNamespace()
    terminate_calls = []
    open_calls = []

    class _Extension:
        def Rebuild(self, _arg):
            return True

        def SaveAs(self, filepath, *_args):
            Path(filepath).write_text("step", encoding="utf-8")
            return True

    class _Doc:
        Extension = _Extension()

        def ShowConfiguration2(self, _name):
            return True

        def Rebuild(self, _arg):
            return True

    def open_model(*_args):
        open_calls.append("open")
        if len(open_calls) == 1:
            executor._last_open_error = RuntimeError("-2147023170 远程过程调用失败。")
            return None
        return _Doc()

    monkeypatch.setattr(executor, "_connect_sw", lambda: app)
    monkeypatch.setattr(executor, "_open_sw_model", open_model)
    monkeypatch.setattr(executor, "_import_design_table_with_retry", lambda *_args: True)
    monkeypatch.setattr(
        executor,
        "_terminate_sw_processes",
        lambda: terminate_calls.append("terminate"),
    )
    monkeypatch.setattr(executor, "_disconnect_sw", lambda *_args: None)

    assert executor.export_sw_per_config(1) is True
    assert open_calls == ["open", "open"]
    assert terminate_calls == ["terminate"]


def test_opendoc6_rpc_recovery_fails_gracefully_when_reopen_fails(
    fake_com_modules,
    sw_paths,
    monkeypatch,
):
    executor = SWExecutor(SimpleNamespace())
    app = SimpleNamespace()
    terminate_calls = []

    def open_model(*_args):
        executor._last_open_error = RuntimeError("0x800706BE RPC_S_CALL_FAILED")
        return None

    monkeypatch.setattr(executor, "_connect_sw", lambda: app)
    monkeypatch.setattr(executor, "_open_sw_model", open_model)
    monkeypatch.setattr(
        executor,
        "_terminate_sw_processes",
        lambda: terminate_calls.append("terminate"),
    )
    monkeypatch.setattr(executor, "_disconnect_sw", lambda *_args: None)

    assert executor.export_sw_per_config(1) is False
    assert terminate_calls == ["terminate"]
    assert "无法打开模型" in executor.last_error


def test_opendoc6_non_rpc_failure_does_not_terminate_sw(
    fake_com_modules,
    sw_paths,
    monkeypatch,
):
    executor = SWExecutor(SimpleNamespace())
    app = SimpleNamespace()
    terminate_calls = []

    def open_model(*_args):
        executor._last_open_error = RuntimeError("file is corrupt")
        return None

    monkeypatch.setattr(executor, "_connect_sw", lambda: app)
    monkeypatch.setattr(executor, "_open_sw_model", open_model)
    monkeypatch.setattr(
        executor,
        "_terminate_sw_processes",
        lambda: terminate_calls.append("terminate"),
    )
    monkeypatch.setattr(executor, "_disconnect_sw", lambda *_args: None)

    assert executor.export_sw_per_config(1) is False
    assert terminate_calls == []
    assert "无法打开模型" in executor.last_error
