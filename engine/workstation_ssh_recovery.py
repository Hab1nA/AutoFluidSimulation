from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_float(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError:
        logger.warning("[SSH-Recovery] 环境变量 %s=%r 无效，使用默认值 %s", name, raw_value, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        logger.warning("[SSH-Recovery] 环境变量 %s=%r 无效，使用默认值 %s", name, raw_value, default)
        return default


class WorkstationTunnelRepairer(Protocol):
    def repair(
        self,
        workstation_id: str,
        target: Mapping[str, Any],
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class WorkstationSshRecoveryPolicy:
    enabled: bool = True
    failure_threshold: int = 2
    min_repair_interval_seconds: float = 300.0
    repair_timeout_seconds: float = 180.0
    repair_sources: frozenset[str] = frozenset({"active_probe"})

    @classmethod
    def from_env(cls) -> "WorkstationSshRecoveryPolicy":
        return cls(
            enabled=_env_bool("AUTOFLUID_WORKSTATION_SSH_AUTO_RECOVERY", True),
            failure_threshold=max(
                1,
                _env_int("AUTOFLUID_WORKSTATION_SSH_RECOVERY_THRESHOLD", 2),
            ),
            min_repair_interval_seconds=max(
                1.0,
                _env_float("AUTOFLUID_WORKSTATION_SSH_RECOVERY_MIN_INTERVAL", 300.0),
            ),
            repair_timeout_seconds=max(
                10.0,
                _env_float("AUTOFLUID_WORKSTATION_SSH_RECOVERY_TIMEOUT", 180.0),
            ),
        )


@dataclass
class WorkstationSshRecoveryState:
    failure_count: int = 0
    last_status: str = "unknown"
    last_source: str = ""
    last_target: dict[str, Any] = field(default_factory=dict)
    last_checked_at: float | None = None
    last_repair_started_at: float | None = None
    last_repair_finished_at: float | None = None
    last_repair_status: str = "never"
    last_repair_detail: str = ""
    repair_inflight: bool = False

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "failure_count": self.failure_count,
            "last_status": self.last_status,
            "last_source": self.last_source,
            "last_target": dict(self.last_target),
            "last_repair_status": self.last_repair_status,
            "repair_inflight": self.repair_inflight,
        }
        if self.last_checked_at is not None:
            result["last_checked_at"] = self.last_checked_at
        if self.last_repair_started_at is not None:
            result["last_repair_started_at"] = self.last_repair_started_at
        if self.last_repair_finished_at is not None:
            result["last_repair_finished_at"] = self.last_repair_finished_at
        if self.last_repair_detail:
            result["last_repair_detail"] = self.last_repair_detail
        return result


class SubprocessWorkstationTunnelRepairer:
    """Run the existing workstation tunnel ensure flow as a bounded subprocess."""

    def __init__(
        self,
        *,
        project_dir: str,
        python_exe: str | None = None,
        jobs: int = 4,
    ) -> None:
        self.project_dir = project_dir
        self.python_exe = python_exe or sys.executable
        self.jobs = jobs

    def repair(
        self,
        workstation_id: str,
        target: Mapping[str, Any],
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        command = [
            self.python_exe,
            "-m",
            "tools.workstation_tunnel",
            "ensure",
            "--workstation",
            workstation_id,
            "--jobs",
            str(self.jobs),
            "--progress-jsonl",
            "--project-dir",
            self.project_dir,
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                command,
                cwd=self.project_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                creationflags=creationflags,
            )
        except OSError as exc:
            return {
                "ok": False,
                "status": "spawn_failed",
                "detail": str(exc),
                "target": dict(target),
            }

        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                remaining = deadline - time.monotonic()
                if cancel_event is not None and cancel_event.is_set():
                    stdout, stderr = self._terminate_process(process)
                    return {
                        "ok": False,
                        "status": "cancelled",
                        "detail": "repair cancelled",
                        "target": dict(target),
                        "stdout": stdout,
                        "stderr": stderr,
                    }
                if remaining <= 0:
                    stdout, stderr = self._terminate_process(process)
                    return {
                        "ok": False,
                        "status": "timeout",
                        "detail": f"repair timed out after {timeout_seconds:.1f}s",
                        "target": dict(target),
                        "stdout": stdout,
                        "stderr": stderr,
                    }
                try:
                    stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except Exception:
            self._terminate_process(process)
            raise

        stdout = (stdout or "").strip()
        stderr = (stderr or "").strip()
        payload: dict[str, Any] = {}
        if stdout:
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError:
                payload = {"raw_stdout": stdout}
        ok = process.returncode == 0 and bool(payload.get("ok", process.returncode == 0))
        return {
            "ok": ok,
            "status": "repair_succeeded" if ok else "repair_failed",
            "returncode": process.returncode,
            "detail": stderr or stdout,
            "target": dict(target),
            "payload": payload,
        }

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> tuple[str, str]:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                    shell=False,
                )
            else:
                process.kill()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        return stdout or "", stderr or ""


class LocalWorkerWorkstationTunnelRepairer:
    """Ask an online LocalWorker to repair workstation-owned reverse tunnels."""

    def __init__(self, local_worker_adapter: Any) -> None:
        self.local_worker_adapter = local_worker_adapter

    def repair(
        self,
        workstation_id: str,
        target: Mapping[str, Any],
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        if cancel_event is not None and cancel_event.is_set():
            return {
                "ok": False,
                "status": "cancelled",
                "detail": "repair cancelled before LocalWorker dispatch",
                "target": dict(target),
            }
        ensure = getattr(self.local_worker_adapter, "ensure_workstation_tunnel", None)
        if not callable(ensure):
            return {
                "ok": False,
                "status": "local_worker_unavailable",
                "detail": "LocalWorker adapter cannot repair workstation tunnels",
                "target": dict(target),
            }
        result = ensure(workstation_id, timeout_seconds=timeout_seconds)
        ok = bool(result.get("ok")) if isinstance(result, Mapping) else False
        return {
            "ok": ok,
            "status": "repair_succeeded" if ok else "local_worker_repair_failed",
            "detail": "" if ok else str(result.get("error", result) if isinstance(result, Mapping) else result),
            "target": dict(target),
            "payload": dict(result) if isinstance(result, Mapping) else {"raw_result": result},
        }


class CompositeWorkstationTunnelRepairer:
    """Try multiple repair paths in order and return the first success."""

    def __init__(self, repairers: list[WorkstationTunnelRepairer]) -> None:
        self.repairers = list(repairers)

    def repair(
        self,
        workstation_id: str,
        target: Mapping[str, Any],
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        for repairer in self.repairers:
            if cancel_event is not None and cancel_event.is_set():
                return {
                    "ok": False,
                    "status": "cancelled",
                    "detail": "repair cancelled",
                    "target": dict(target),
                    "attempts": attempts,
                }
            result = repairer.repair(
                workstation_id,
                target,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
            )
            attempts.append(dict(result))
            if result.get("ok"):
                return dict(result, attempts=attempts)
        return {
            "ok": False,
            "status": "all_repairers_failed",
            "detail": attempts[-1].get("detail", "") if attempts else "no repairers configured",
            "target": dict(target),
            "attempts": attempts,
        }


class WorkstationSshRecoveryManager:
    """Classify workstation SSH failures and run bounded tunnel repairs."""

    def __init__(
        self,
        *,
        policy: WorkstationSshRecoveryPolicy | None = None,
        repairer: WorkstationTunnelRepairer | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.policy = policy or WorkstationSshRecoveryPolicy.from_env()
        self.repairer = repairer
        self.clock = clock
        self._lock = threading.RLock()
        self._idle_condition = threading.Condition(self._lock)
        self._shutdown_event = threading.Event()
        self._states: dict[str, WorkstationSshRecoveryState] = {}

    def _state_for_locked(self, workstation_id: str) -> WorkstationSshRecoveryState:
        state = self._states.get(workstation_id)
        if state is None:
            state = WorkstationSshRecoveryState()
            self._states[workstation_id] = state
        return state

    def record_check(
        self,
        workstation_id: str,
        status: str,
        target: Mapping[str, Any],
        *,
        source: str,
    ) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            state = self._state_for_locked(workstation_id)
            state.last_status = status
            state.last_source = source
            state.last_target = dict(target)
            state.last_checked_at = now
            if status == "ok":
                state.failure_count = 0
                state.last_repair_status = "healthy"
                state.last_repair_detail = ""
                return {"status": status, "repair": {"status": "healthy"}}
            if self._shutdown_event.is_set():
                return {"status": status, "repair": {"status": "shutdown"}}

            state.failure_count += 1
            if source == "post_repair_probe":
                state.last_repair_status = "repair_verified_failed"
                state.last_repair_detail = status
                state.last_repair_started_at = None
                return {"status": status, "repair": {"status": "repair_verified_failed"}}
            repair_decision = self._repair_decision_locked(state, target, source, now)
            if repair_decision != "run":
                return {"status": status, "repair": {"status": repair_decision}}
            state.repair_inflight = True
            state.last_repair_started_at = now
            state.last_repair_status = "running"

        repair_result = self._run_repair(workstation_id, target)
        finished_at = self.clock()
        with self._lock:
            state = self._state_for_locked(workstation_id)
            state.repair_inflight = False
            state.last_repair_finished_at = finished_at
            state.last_repair_status = (
                "repair_succeeded" if repair_result.get("ok") else "repair_failed"
            )
            state.last_repair_detail = str(repair_result.get("detail") or "")
            self._idle_condition.notify_all()
        return {"status": status, "repair": dict(repair_result, status=state.last_repair_status)}

    def _repair_decision_locked(
        self,
        state: WorkstationSshRecoveryState,
        target: Mapping[str, Any],
        source: str,
        now: float,
    ) -> str:
        if not self.policy.enabled:
            return "disabled"
        if self.repairer is None:
            return "no_repairer"
        if source not in self.policy.repair_sources:
            return "source_not_repairable"
        if str(target.get("connectivity_mode") or "direct") != "reverse_tunnel":
            return "not_reverse_tunnel"
        if state.failure_count < self.policy.failure_threshold:
            return "waiting_for_threshold"
        if state.repair_inflight:
            return "already_running"
        if state.last_repair_started_at is not None:
            elapsed = now - state.last_repair_started_at
            if elapsed < self.policy.min_repair_interval_seconds:
                return "throttled"
        return "run"

    def _run_repair(self, workstation_id: str, target: Mapping[str, Any]) -> dict[str, Any]:
        if self.repairer is None:
            return {"ok": False, "status": "no_repairer", "detail": ""}
        try:
            return self.repairer.repair(
                workstation_id,
                target,
                timeout_seconds=self.policy.repair_timeout_seconds,
                cancel_event=self._shutdown_event,
            )
        except Exception as exc:  # pragma: no cover - defensive boundary
            logger.warning("[SSH-Recovery] 工作站 %s 隧道修复异常: %s", workstation_id, exc)
            return {"ok": False, "status": "repair_exception", "detail": str(exc)}

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                workstation_id: state.to_dict()
                for workstation_id, state in self._states.items()
            }

    def reset_history(self) -> bool:
        with self._idle_condition:
            if any(state.repair_inflight for state in self._states.values()):
                return False
            self._states.clear()
            self._shutdown_event.clear()
            return True

    def shutdown(self, *, timeout: float = 2.0) -> bool:
        self._shutdown_event.set()
        deadline = time.monotonic() + max(0.0, timeout)
        with self._idle_condition:
            while any(state.repair_inflight for state in self._states.values()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle_condition.wait(timeout=remaining)
        return True


def default_recovery_manager(
    project_root: str | Path,
    *,
    local_worker_adapter: Any | None = None,
) -> WorkstationSshRecoveryManager:
    repairers: list[WorkstationTunnelRepairer] = []
    if local_worker_adapter is not None:
        repairers.append(LocalWorkerWorkstationTunnelRepairer(local_worker_adapter))
    repairers.append(SubprocessWorkstationTunnelRepairer(project_dir=str(project_root)))
    return WorkstationSshRecoveryManager(
        policy=WorkstationSshRecoveryPolicy.from_env(),
        repairer=CompositeWorkstationTunnelRepairer(repairers),
    )
