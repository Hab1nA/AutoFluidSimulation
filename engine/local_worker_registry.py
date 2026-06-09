"""Track LocalWorker registration and heartbeat state."""
from __future__ import annotations

import time
from collections.abc import Callable
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
        self._workers[worker_id] = worker
        return self._with_online(worker)

    def heartbeat(self, worker_id: str) -> dict[str, Any]:
        """Refresh one worker heartbeat, creating a minimal entry if needed."""
        now = self._clock()
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
        worker = self._workers.get(worker_id)
        if worker is None:
            return None
        return self._with_online(worker)

    def has_online_worker(self) -> bool:
        """Return True if any worker is currently online."""
        return any(self._is_online(worker) for worker in self._workers.values())

    def _is_online(self, worker: dict[str, Any]) -> bool:
        last_seen_at = float(worker["last_seen_at"])
        return self._clock() - last_seen_at <= self._timeout_seconds

    def _with_online(self, worker: dict[str, Any]) -> dict[str, Any]:
        snapshot = dict(worker)
        snapshot["capabilities"] = dict(worker.get("capabilities", {}))
        snapshot["network"] = dict(worker.get("network", {}))
        snapshot["online"] = self._is_online(worker)
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
