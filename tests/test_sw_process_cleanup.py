from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from executor.sw_executor import SWExecutor


class _State:
    pass


class TestSWProcessCleanup:
    """验证 SolidWorks 全量清理生命周期。"""

    def test_initial_cleanup_state(self) -> None:
        executor = SWExecutor(_State())

        assert executor._first_cleanup_done is False
        assert executor._final_cleanup_done is False

    def test_do_first_cleanup_idempotent(self, monkeypatch) -> None:
        executor = SWExecutor(_State())
        calls: list[str] = []

        monkeypatch.setattr(
            executor,
            "_shutdown_all_internal",
            lambda: calls.append("shutdown"),
        )

        executor.do_first_cleanup()
        executor.do_first_cleanup()

        assert executor._first_cleanup_done is True
        assert calls == ["shutdown"]

    def test_do_final_cleanup_idempotent(self, monkeypatch) -> None:
        executor = SWExecutor(_State())
        calls: list[str] = []

        monkeypatch.setattr(
            executor,
            "_shutdown_all_internal",
            lambda: calls.append("shutdown"),
        )

        executor.do_final_cleanup()
        executor.do_final_cleanup()

        assert executor._final_cleanup_done is True
        assert calls == ["shutdown"]

    def test_shutdown_all_resets_cleanup_flags(self, monkeypatch) -> None:
        executor = SWExecutor(_State())
        executor._first_cleanup_done = True
        executor._final_cleanup_done = True
        calls: list[str] = []

        monkeypatch.setattr(
            executor,
            "_shutdown_all_internal",
            lambda: calls.append("shutdown"),
        )

        executor.shutdown_all()

        assert executor._first_cleanup_done is False
        assert executor._final_cleanup_done is False
        assert calls == ["shutdown"]

    def test_reset_cleanup_state_clears_cache_and_flags(self, monkeypatch) -> None:
        executor = SWExecutor(_State())
        executor._first_cleanup_done = True
        executor._final_cleanup_done = True
        calls: list[str] = []

        monkeypatch.setattr(
            executor,
            "disconnect_sw_cached",
            lambda: calls.append("disconnect"),
        )

        executor.reset_cleanup_state()

        assert executor._first_cleanup_done is False
        assert executor._final_cleanup_done is False
        assert calls == ["disconnect"]

    def test_shutdown_all_internal_disconnects_and_terminates(self, monkeypatch) -> None:
        executor = SWExecutor(_State())
        calls: list[str] = []

        monkeypatch.setattr(
            executor,
            "disconnect_sw_cached",
            lambda: calls.append("disconnect"),
        )
        monkeypatch.setattr(
            executor,
            "_terminate_sw_processes",
            lambda: calls.append("terminate"),
        )

        with executor._cleanup_lock:
            executor._shutdown_all_internal()

        assert calls == ["disconnect", "terminate"]

    def test_is_sw_process_running_handles_missing_stdout(self, monkeypatch) -> None:
        executor = SWExecutor(_State())

        monkeypatch.setattr("executor.sw_executor.os.name", "nt")
        monkeypatch.setattr(
            "executor.sw_executor.subprocess.run",
            lambda *_args, **_kwargs: SimpleNamespace(stdout=None),
        )

        assert executor._is_sw_process_running() is False

    def test_disconnect_sw_defers_exit_to_full_cleanup(self, monkeypatch) -> None:
        executor = SWExecutor(_State())
        sw_app = MagicMock()
        doc = MagicMock()
        doc.GetTitle.return_value = "model_gen4.SLDPRT"
        com_calls: list[str] = []

        monkeypatch.setitem(
            sys.modules,
            "pythoncom",
            SimpleNamespace(
                CoFreeUnusedLibraries=lambda: com_calls.append("free"),
                CoUninitialize=lambda: com_calls.append("uninitialize"),
            ),
        )
        monkeypatch.setattr("executor.sw_executor.time.sleep", lambda _seconds: None)
        monkeypatch.setattr(
            executor,
            "_terminate_sw_processes",
            lambda: com_calls.append("terminate"),
        )

        executor._disconnect_sw(
            sw_app,
            doc,
            r"C:\models\model_gen4.SLDPRT",
        )

        sw_app.CloseDoc.assert_called_once_with("model_gen4.SLDPRT")
        sw_app.ExitApp.assert_not_called()
        assert "terminate" not in com_calls
        assert com_calls.count("free") == 2
        assert com_calls[-1] == "uninitialize"
