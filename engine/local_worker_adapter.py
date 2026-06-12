"""Daemon-side adapter for delegating local Windows tasks to LocalWorker."""
from __future__ import annotations

from typing import Any

from engine.local_worker_registry import LocalWorkerRegistry
from utils.logger import setup_logger

logger = setup_logger(__name__)


class LocalWorkerAdapter:
    """Queue SW/SC work for LocalWorker and wait for completion."""

    def __init__(
        self,
        registry: LocalWorkerRegistry,
        result_poll_interval: float = 0.5,
    ) -> None:
        self._registry = registry
        self._result_poll_interval = result_poll_interval

    def execute_sw_step(self, timeout_seconds: float = 3600.0) -> bool:
        """Delegate the batch SW step to a LocalWorker."""
        return self._execute("sw", {}, timeout_seconds)

    def execute_sw_per_config(
        self,
        config_name: int,
        timeout_seconds: float = 3600.0,
    ) -> bool:
        """Delegate one SW config export to a LocalWorker."""
        return self._execute("sw", {"config_name": config_name}, timeout_seconds)

    def execute_sc_step(
        self,
        config_name: int,
        timeout_seconds: float = 3600.0,
    ) -> bool:
        """Delegate one SC conversion to a LocalWorker."""
        return self._execute("sc", {"config_name": config_name}, timeout_seconds)

    def check_local_environment(self, timeout_seconds: float = 120.0) -> dict[str, Any] | None:
        """Delegate a LocalWorker-side environment check."""
        result = self._execute_for_result("check_local_environment", {}, timeout_seconds)
        if result is None:
            return None
        return dict(result)

    def clean_local_files(
        self,
        step_name: str,
        config_name: int | str | None = None,
        timeout_seconds: float = 300.0,
    ) -> bool:
        """Delegate LocalWorker-side file cleanup."""
        params: dict[str, Any] = {"step_name": step_name}
        if config_name is not None and config_name != "all":
            params["config_name"] = config_name
        return self._execute("clean_local_files", params, timeout_seconds)

    def has_active_task(
        self,
        step: str,
        config_name: int | None = None,
    ) -> bool:
        """Return True when a matching delegated task has not reached terminal state."""
        return self._registry.has_active_step_task(step, config_name)

    def _execute(
        self,
        step: str,
        params: dict[str, Any],
        timeout_seconds: float,
    ) -> bool:
        result = self._execute_for_result(step, params, timeout_seconds)
        return result is not None and bool(result.get("ok", True))

    def _execute_for_result(
        self,
        step: str,
        params: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        if not self._registry.has_online_worker():
            logger.error("[LocalWorker] 没有在线 LocalWorker，无法执行 %s", step)
            return None
        task = self._registry.enqueue_task(
            step,
            params,
            timeout_seconds=timeout_seconds,
        )
        finished = self._registry.wait_for_task(
            str(task["task_id"]),
            timeout_seconds=timeout_seconds,
            poll_interval=self._result_poll_interval,
        )
        if finished["status"] != "completed":
            logger.error(
                "[LocalWorker] 任务失败: step=%s task_id=%s error=%s",
                step,
                finished["task_id"],
                finished.get("error", ""),
            )
            return None
        result = finished.get("result", {})
        if not isinstance(result, dict):
            return {"ok": False}
        return dict(result)
