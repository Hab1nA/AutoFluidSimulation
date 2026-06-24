"""Track LocalWorker registration and heartbeat state."""
from __future__ import annotations

import time
from collections.abc import Callable
import threading
from typing import Any


class LocalWorkerRegistry:
    """In-memory registry for LocalWorker liveness."""

    def __init__(
        self,
        timeout_seconds: float = 90.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._clock = clock or time.time
        self._workers: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._pending_task_ids: list[str] = []
        self._task_counter = 0
        self._lock = threading.RLock()
        self._task_condition = threading.Condition(self._lock)

    def register(
        self,
        worker_id: str,
        capabilities: dict[str, Any] | None = None,
        network: dict[str, Any] | None = None,
        remote_addr: str | None = None,
    ) -> dict[str, Any]:
        """Register or refresh one LocalWorker."""
        now = self._clock()
        network_snapshot = self._network_snapshot(network, remote_addr)
        worker = {
            "worker_id": worker_id,
            "capabilities": dict(capabilities or {}),
            "network": network_snapshot,
            "registered_at": now,
            "last_seen_at": now,
        }
        with self._lock:
            self._workers[worker_id] = worker
            return self._with_online(worker)

    def heartbeat(self, worker_id: str) -> dict[str, Any]:
        """Refresh one worker heartbeat, creating a minimal entry if needed."""
        now = self._clock()
        with self._lock:
            worker = self._workers.setdefault(
                worker_id,
                {
                    "worker_id": worker_id,
                    "capabilities": {},
                    "network": {},
                    "registered_at": now,
                    "last_seen_at": now,
                },
            )
            worker["last_seen_at"] = now
            return self._with_online(worker)

    def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        """Return one worker snapshot."""
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                return None
            return self._with_online(worker)

    def has_online_worker(self) -> bool:
        """Return True if any worker is currently online."""
        with self._lock:
            return any(self._is_online(worker) for worker in self._workers.values())

    def online_workers(self) -> list[dict[str, Any]]:
        """Return snapshots for workers whose heartbeat is still fresh."""
        with self._lock:
            return [
                self._with_online(worker)
                for worker in self._workers.values()
                if self._is_online(worker)
            ]

    def has_active_step_task(
        self,
        step: str,
        config_name: int | None = None,
    ) -> bool:
        """Return True when a matching LocalWorker task is pending or running."""
        with self._lock:
            for task in self._tasks.values():
                if task.get("step") != step:
                    continue
                if task.get("status") not in {"pending", "running"}:
                    continue
                params = task.get("params", {})
                if config_name is None or "config_name" not in params:
                    return True
                try:
                    if int(params["config_name"]) == int(config_name):
                        return True
                except (TypeError, ValueError):
                    continue
            return False

    def clear_online_workers(self) -> None:
        """Clear all registered workers so they must re-register."""
        with self._lock:
            self._workers.clear()
            # _task_condition 的 notify 唤醒可能在等待 worker 的线程
            self._task_condition.notify_all()

    def prune_offline_workers(self) -> int:
        """Remove workers whose heartbeat has expired and return removed count."""
        with self._lock:
            stale_worker_ids = [
                worker_id
                for worker_id, worker in self._workers.items()
                if not self._is_online(worker)
            ]
            for worker_id in stale_worker_ids:
                self._workers.pop(worker_id, None)
            if stale_worker_ids:
                self._task_condition.notify_all()
            return len(stale_worker_ids)
    def clear_pending_tasks(self) -> int:
        """Cancel all pending tasks and return the count of cancelled tasks."""
        with self._task_condition:
            cancelled = 0
            for task_id in list(self._pending_task_ids):
                task = self._tasks.get(task_id)
                if task is not None and task.get("status") == "pending":
                    task["status"] = "error"
                    task["error"] = "Worker 停止时取消"
                    task["finished_at"] = self._clock()
                    cancelled += 1
            self._pending_task_ids.clear()
            self._task_condition.notify_all()
            return cancelled

    def enqueue_task(
        self,
        step: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Queue one LocalWorker task."""
        with self._task_condition:
            self._task_counter += 1
            task_id = f"local-{self._task_counter}"
            task = {
                "task_id": task_id,
                "step": step,
                "params": dict(params or {}),
                "status": "pending",
                "created_at": self._clock(),
                "worker_id": None,
            }
            if timeout_seconds is not None:
                task["timeout_seconds"] = float(timeout_seconds)
            self._tasks[task_id] = task
            self._pending_task_ids.append(task_id)
            self._task_condition.notify_all()
            return self._task_snapshot(task)

    def poll_task(self, worker_id: str) -> dict[str, Any] | None:
        """Assign and return the next pending task for a LocalWorker."""
        with self._task_condition:
            while self._pending_task_ids:
                task_id = self._pending_task_ids.pop(0)
                task = self._tasks[task_id]
                if task.get("status") != "pending":
                    continue
                task["status"] = "running"
                task["worker_id"] = worker_id
                task["started_at"] = self._clock()
                self.heartbeat(worker_id)
                return self._task_snapshot(task)
            else:
                self.heartbeat(worker_id)
                return None

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Return one LocalWorker task snapshot without changing its state."""
        with self._task_condition:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return self._task_snapshot(task)

    def complete_task(
        self,
        task_id: str,
        worker_id: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mark one LocalWorker task as completed."""
        return self._finish_task(task_id, worker_id, "completed", result or {}, "")

    def fail_task(
        self,
        task_id: str,
        worker_id: str,
        error: str,
    ) -> dict[str, Any]:
        """Mark one LocalWorker task as failed."""
        return self._finish_task(task_id, worker_id, "error", {}, error)

    def wait_for_task(
        self,
        task_id: str,
        timeout_seconds: float,
        poll_interval: float,
    ) -> dict[str, Any]:
        """Wait for one queued task to reach a terminal state."""
        deadline = time.monotonic() + timeout_seconds
        with self._task_condition:
            while True:
                task = self._tasks[task_id]
                if task["status"] in {"completed", "error"}:
                    return self._task_snapshot(task)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    task["status"] = "error"
                    task["error"] = "LocalWorker 任务等待超时"
                    task["finished_at"] = self._clock()
                    self._pending_task_ids = [
                        pending_id
                        for pending_id in self._pending_task_ids
                        if pending_id != task_id
                    ]
                    self._task_condition.notify_all()
                    return self._task_snapshot(task)
                self._task_condition.wait(timeout=min(poll_interval, remaining))

    def _is_online(self, worker: dict[str, Any]) -> bool:
        last_seen_at = float(worker["last_seen_at"])
        return self._clock() - last_seen_at <= self._timeout_seconds

    def _with_online(self, worker: dict[str, Any]) -> dict[str, Any]:
        snapshot = dict(worker)
        snapshot["capabilities"] = dict(worker.get("capabilities", {}))
        snapshot["network"] = dict(worker.get("network", {}))
        snapshot["online"] = self._is_online(worker)
        return snapshot

    def _finish_task(
        self,
        task_id: str,
        worker_id: str,
        status: str,
        result: dict[str, Any],
        error: str,
    ) -> dict[str, Any]:
        with self._task_condition:
            task = self._tasks[task_id]
            if task.get("status") in {"completed", "error"}:
                return self._task_snapshot(task)
            if task.get("worker_id") not in {None, worker_id}:
                raise ValueError(f"任务 {task_id} 已由其他 LocalWorker 领取")
            task["worker_id"] = worker_id
            task["status"] = status
            task["result"] = dict(result)
            if error:
                task["error"] = error
            task["finished_at"] = self._clock()
            self.heartbeat(worker_id)
            self._task_condition.notify_all()
            return self._task_snapshot(task)

    @staticmethod
    def _task_snapshot(task: dict[str, Any]) -> dict[str, Any]:
        snapshot = dict(task)
        snapshot["params"] = dict(task.get("params", {}))
        if "result" in task:
            snapshot["result"] = dict(task.get("result", {}))
        return snapshot

    @staticmethod
    def _network_snapshot(
        network: dict[str, Any] | None,
        remote_addr: str | None,
    ) -> dict[str, Any]:
        snapshot = dict(network or {})
        candidate_hosts = snapshot.get("candidate_hosts")
        if isinstance(candidate_hosts, (list, tuple)):
            snapshot["candidate_hosts"] = [str(host) for host in candidate_hosts]
        elif candidate_hosts is not None:
            snapshot["candidate_hosts"] = [str(candidate_hosts)]
        if remote_addr:
            snapshot["last_seen_remote_addr"] = remote_addr
        return snapshot
