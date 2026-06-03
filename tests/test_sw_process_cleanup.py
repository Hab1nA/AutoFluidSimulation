from __future__ import annotations

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
