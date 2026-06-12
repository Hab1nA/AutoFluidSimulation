"""Local Windows worker registration client for split-daemon deployments."""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import urlopen

from engine.config import LOCAL_PATHS, get_step_filename, reload_config_from_toml
from engine.state_manager import StateManager
from engine.task_runner import TaskRunner
from ipc.protocol import (
    CMD_WORKER_HEARTBEAT,
    CMD_WORKER_POLL,
    CMD_WORKER_REGISTER,
    CMD_WORKER_STEP_COMPLETE,
    CMD_WORKER_STEP_ERROR,
    create_request,
    deserialize,
    serialize,
)
from utils.excel_reader import read_model_configs


DEFAULT_PUBLIC_IP_URL = "https://api.ipify.org"


@dataclass(frozen=True)
class LocalWorkerConfig:
    """Runtime configuration for a LocalWorker process."""

    worker_id: str
    server_host: str
    server_port: int
    auth_token: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)
    network: dict[str, Any] = field(default_factory=dict)
    heartbeat_interval: float = 30.0
    poll_interval: float = 2.0
    register_retry_interval: float = 2.0
    request_timeout: float = 10.0


TaskHandler = Callable[[dict[str, Any]], dict[str, Any]]


class LocalWorker:
    """Minimal LocalWorker that registers with the remote daemon and heartbeats."""

    def __init__(
        self,
        config: LocalWorkerConfig,
        task_handlers: dict[str, TaskHandler] | None = None,
    ) -> None:
        self.config = config
        self._task_handlers = task_handlers or {}
        self._default_runner: TaskRunner | None = None
        self._config_payload: dict[str, list[float]] | None = None
        self._last_heartbeat_at: float | None = None
        self._finished_reports: queue.Queue[dict[str, Any]] = queue.Queue()
        self._pending_reports: list[dict[str, Any]] = []
        self._deferred_tasks: list[dict[str, Any]] = []
        self._active_lane_counts: dict[str, int] = {"sw": 0, "sc": 0, "other": 0}
        self._lane_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "LocalWorker":
        """Build a LocalWorker from environment variables."""
        reload_config_from_toml()
        worker_id = os.environ.get("AUTOFLUID_WORKER_ID") or socket.gethostname()
        server_host = (
            os.environ.get("AUTOFLUID_SERVER_HOST")
            or os.environ.get("AUTOFLUID_IPC_HOST")
            or "127.0.0.1"
        )
        server_port = int(os.environ.get("AUTOFLUID_IPC_PORT", "9527"))
        auth_token = os.environ.get("AUTOFLUID_IPC_AUTH_TOKEN", "")
        capabilities = {
            "sw": True,
            "sc": True,
            "clean": True,
            "check": True,
            "sc_slots": int(os.environ.get("AUTOFLUID_WORKER_SC_SLOTS", "3")),
        }
        return cls(
            LocalWorkerConfig(
                worker_id=worker_id,
                server_host=server_host,
                server_port=server_port,
                auth_token=auth_token,
                capabilities=capabilities,
                network=build_worker_network(),
            )
        )

    def build_register_request(self) -> dict[str, Any]:
        """Return the IPC request used to register this LocalWorker."""
        params: dict[str, Any] = {
            "worker_id": self.config.worker_id,
            "capabilities": dict(self.config.capabilities),
            "network": dict(self.config.network),
        }
        config_payload = self._load_config_payload()
        if config_payload:
            params["configs"] = config_payload
        return create_request(
            CMD_WORKER_REGISTER,
            params,
            auth_token=self.config.auth_token,
        )

    def build_heartbeat_request(self) -> dict[str, Any]:
        """Return the IPC request used to refresh this LocalWorker heartbeat."""
        return create_request(
            CMD_WORKER_HEARTBEAT,
            {"worker_id": self.config.worker_id},
            auth_token=self.config.auth_token,
        )

    def build_poll_request(self) -> dict[str, Any]:
        """Return the IPC request used to pull one queued task."""
        return create_request(
            CMD_WORKER_POLL,
            {"worker_id": self.config.worker_id},
            auth_token=self.config.auth_token,
        )

    def build_step_complete_request(
        self,
        task_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the IPC request used to report task completion."""
        return create_request(
            CMD_WORKER_STEP_COMPLETE,
            {
                "worker_id": self.config.worker_id,
                "task_id": task_id,
                "result": dict(result),
            },
            auth_token=self.config.auth_token,
        )

    def build_step_error_request(self, task_id: str, error: str) -> dict[str, Any]:
        """Return the IPC request used to report task failure."""
        return create_request(
            CMD_WORKER_STEP_ERROR,
            {
                "worker_id": self.config.worker_id,
                "task_id": task_id,
                "error": error,
            },
            auth_token=self.config.auth_token,
        )

    def register_once(self) -> dict[str, Any]:
        """Register this worker with the daemon once."""
        return self._send_request(self.build_register_request())

    def heartbeat_once(self) -> dict[str, Any]:
        """Send one heartbeat to the daemon."""
        return self._send_request(self.build_heartbeat_request())

    def run_forever(self) -> None:
        """Register once, then keep polling tasks and heartbeating until interrupted."""
        self._register_until_available()
        self._last_heartbeat_at = time.monotonic()
        while True:
            time.sleep(self.config.poll_interval)
            try:
                self.run_once()
            except RuntimeError as exc:
                print(f"[WARN] {exc}", file=sys.stderr)
                self._register_until_available()
                self._last_heartbeat_at = time.monotonic()

    def _register_until_available(self) -> None:
        """Keep the worker alive while the remote daemon is still starting."""
        while True:
            try:
                self.register_once()
                return
            except RuntimeError as exc:
                print(f"[WARN] {exc}", file=sys.stderr)
                time.sleep(self.config.register_retry_interval)

    def run_once(self, now: float | None = None) -> str:
        """Advance the LocalWorker scheduler by one non-blocking tick."""
        current = time.monotonic() if now is None else now
        report_status = self._send_next_finished_report()
        if report_status is not None:
            return report_status

        if self._start_deferred_task_if_possible():
            return "dispatched"

        if not self._has_available_lane():
            return self._heartbeat_if_due(current)

        response = self._send_request(self.build_poll_request())
        task = response.get("data")
        if isinstance(task, dict):
            if self._try_start_task(task):
                return "dispatched"
            self._deferred_tasks.append(dict(task))
            return "deferred"
        return self._heartbeat_if_due(current)

    def _heartbeat_if_due(self, current: float) -> str:
        """Send a heartbeat when no report or dispatch work was performed."""
        if self._last_heartbeat_at is None:
            self._last_heartbeat_at = current
            return "idle"
        if current - self._last_heartbeat_at >= self.config.heartbeat_interval:
            self.heartbeat_once()
            self._last_heartbeat_at = current
            return "heartbeat"
        return "idle"

    def _send_next_finished_report(self) -> str | None:
        """Send one completed task report, preserving it for retry on IPC failure."""
        while True:
            try:
                self._pending_reports.append(self._finished_reports.get_nowait())
            except queue.Empty:
                break

        if not self._pending_reports:
            return None

        report = self._pending_reports[0]
        self._send_request(report)
        self._pending_reports.pop(0)
        return "completed" if report["command"] == CMD_WORKER_STEP_COMPLETE else "error"

    def _start_deferred_task_if_possible(self) -> bool:
        """Start the first deferred task whose execution lane has capacity."""
        for index, task in enumerate(list(self._deferred_tasks)):
            if self._try_start_task(task):
                del self._deferred_tasks[index]
                return True
        return False

    def _has_available_lane(self) -> bool:
        """Return True if any LocalWorker lane can accept more work."""
        with self._lane_lock:
            return any(
                self._active_lane_counts.get(lane, 0) < self._lane_limit(lane)
                for lane in ("sw", "sc", "other")
            )

    def _try_start_task(self, task: dict[str, Any]) -> bool:
        """Start one polled task in its lane without blocking the main poll loop."""
        lane = self._task_lane(str(task.get("step") or ""))
        with self._lane_lock:
            if self._active_lane_counts.get(lane, 0) >= self._lane_limit(lane):
                return False
            self._active_lane_counts[lane] = self._active_lane_counts.get(lane, 0) + 1

        thread = threading.Thread(
            target=self._run_polled_task_in_lane,
            args=(dict(task), lane),
            name=f"LocalWorker-{lane}-{task.get('task_id')}",
            daemon=True,
        )
        thread.start()
        return True

    def _run_polled_task_in_lane(self, task: dict[str, Any], lane: str) -> None:
        """Execute one task and hand its report back to the main IPC loop."""
        try:
            self._finished_reports.put(self.handle_polled_task(task))
        finally:
            with self._lane_lock:
                self._active_lane_counts[lane] = max(
                    0,
                    self._active_lane_counts.get(lane, 0) - 1,
                )

    @staticmethod
    def _task_lane(step: str) -> str:
        if step == "sw":
            return "sw"
        if step == "sc":
            return "sc"
        return "other"

    def _lane_limit(self, lane: str) -> int:
        if lane == "sc":
            raw_slots = self.config.capabilities.get("sc_slots", 3)
            try:
                return max(1, int(raw_slots))
            except (TypeError, ValueError):
                return 3
        return 1

    def handle_polled_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Execute one polled task and return the completion/error request."""
        task_id = str(task.get("task_id") or "")
        step = str(task.get("step") or "")
        params = task.get("params")
        if not isinstance(params, dict):
            params = {}
        try:
            timeout_seconds = self._task_timeout_seconds(task)
            if self._should_isolate_task(step, timeout_seconds):
                result = self._execute_task_in_subprocess(
                    step,
                    dict(params),
                    timeout_seconds,
                )
            else:
                result = self._execute_task(step, params)
        except TimeoutError as exc:
            self._cleanup_after_task_timeout(step)
            return self.build_step_error_request(task_id, str(exc))
        except Exception as exc:
            return self.build_step_error_request(task_id, f"{type(exc).__name__}: {exc}")
        return self.build_step_complete_request(task_id, result)

    @staticmethod
    def _task_timeout_seconds(task: dict[str, Any]) -> float:
        raw_timeout = task.get("timeout_seconds")
        if raw_timeout is None:
            return 0.0
        if not isinstance(raw_timeout, (int, float, str)):
            return 0.0
        try:
            return float(raw_timeout)
        except ValueError:
            return 0.0

    def _should_isolate_task(self, step: str, timeout_seconds: float) -> bool:
        if timeout_seconds <= 0:
            return False
        if step in self._task_handlers:
            return False
        return step in {"sw", "sc"}

    def _execute_task_in_subprocess(
        self,
        step: str,
        params: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="autofluid_worker_task_") as tmp_dir:
            task_path = os.path.join(tmp_dir, "task.json")
            result_path = os.path.join(tmp_dir, "result.json")
            with open(task_path, "w", encoding="utf-8") as handle:
                json.dump({"step": step, "params": params}, handle)

            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "engine.local_worker",
                    "--run-task-file",
                    task_path,
                    "--result-file",
                    result_path,
                ],
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                env=os.environ.copy(),
                creationflags=creationflags,
            )
            try:
                process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                self._terminate_task_process(process)
                raise TimeoutError(
                    f"LocalWorker 子任务超时: step={step}, timeout={timeout_seconds:g}s"
                ) from exc

            if not os.path.exists(result_path):
                raise RuntimeError(
                    f"LocalWorker 子任务未生成结果: step={step}, exit_code={process.returncode}"
                )
            with open(result_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise RuntimeError("LocalWorker 子任务结果格式无效")
            if not payload.get("ok"):
                raise RuntimeError(str(payload.get("error") or "LocalWorker 子任务失败"))
            result = payload.get("result", {})
            if not isinstance(result, dict):
                raise RuntimeError("LocalWorker 子任务 result 格式无效")
            return dict(result)

    @staticmethod
    def _terminate_task_process(process: subprocess.Popen) -> None:
        pid = getattr(process, "pid", None)
        if os.name == "nt" and pid:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    def _cleanup_after_task_timeout(self, step: str) -> None:
        try:
            runner = self._get_default_runner()
            if step == "sw":
                runner.shutdown_sw_processes()
            elif step == "sc":
                runner.shutdown_sc_pool()
        except Exception as exc:
            print(f"[WARN] LocalWorker 超时清理失败: {exc}", file=sys.stderr)

    def _execute_task(self, step: str, params: dict[str, Any]) -> dict[str, Any]:
        handler = self._task_handlers.get(step)
        if handler is None:
            handler = self._default_task_handler(step)
        result = handler(dict(params))
        if "ok" not in result:
            result = {"ok": True, **result}
        return result

    def _default_task_handler(self, step: str) -> TaskHandler:
        if step == "sw":
            return self._run_sw_task
        if step == "sc":
            return self._run_sc_task
        if step == "check_local_environment":
            return self._run_check_local_environment_task
        if step == "clean_local_files":
            return self._run_clean_local_files_task
        raise RuntimeError(f"LocalWorker 尚未配置步骤处理器: {step}")

    def _run_sw_task(self, params: dict[str, Any]) -> dict[str, Any]:
        runner = self._get_default_runner()
        config_name = params.get("config_name")
        if config_name is None:
            ok = runner.execute_sw_step()
        else:
            ok = runner.execute_sw_per_config(int(config_name))
        return {"ok": bool(ok)}

    def _run_sc_task(self, params: dict[str, Any]) -> dict[str, Any]:
        runner = self._get_default_runner()
        config_name = params.get("config_name")
        if config_name is None:
            raise RuntimeError("SC 任务缺少 config_name")
        config_id = int(config_name)
        ok = bool(runner.execute_sc_step(config_id))
        result: dict[str, Any] = {"ok": ok}
        if ok:
            result["scdoc_file"] = self._build_scdoc_payload(config_id)
        return result

    def _run_check_local_environment_task(self, _params: dict[str, Any]) -> dict[str, Any]:
        runner = self._get_default_runner()
        result = runner.run_local_system_check()
        if isinstance(result, dict) and "local_checks" in result:
            return {"ok": True, **result}
        return {"ok": True, "local_checks": result}

    def _run_clean_local_files_task(self, params: dict[str, Any]) -> dict[str, Any]:
        step_name = str(params.get("step_name") or "")
        if not step_name:
            raise RuntimeError("本地清理任务缺少 step_name")
        config_name = params.get("config_name")
        runner = self._get_default_runner()
        runner.clean_local_step_files(step_name, config_name)
        return {"ok": True}

    def _build_scdoc_payload(self, config_name: int) -> dict[str, Any]:
        """Read the generated SCDOC so the server daemon can continue transfer."""
        scdoc_name = get_step_filename("sc", config_name)
        if not scdoc_name:
            raise RuntimeError("无法生成 SCDOC 文件名")
        scdoc_path = os.path.join(LOCAL_PATHS["scdoc_dir"], scdoc_name)
        with open(scdoc_path, "rb") as handle:
            content = handle.read()
        if not content:
            raise RuntimeError(f"SCDOC 文件为空: {scdoc_path}")
        return {
            "config_name": config_name,
            "filename": scdoc_name,
            "size": len(content),
            "content_b64": base64.b64encode(content).decode("ascii"),
        }

    def _get_default_runner(self) -> TaskRunner:
        if self._default_runner is None:
            state = StateManager()
            if self._config_payload is not None:
                configs = {
                    int(config_name): list(values)
                    for config_name, values in self._config_payload.items()
                }
            else:
                configs = read_model_configs(LOCAL_PATHS["excel"])
            state.load_configs(configs)
            self._default_runner = TaskRunner(state)
        return self._default_runner

    def _load_config_payload(self) -> dict[str, list[float]] | None:
        """Best-effort local Excel payload for server-side state initialization."""
        if self._config_payload is not None:
            return {
                config_name: list(values)
                for config_name, values in self._config_payload.items()
            }
        try:
            configs = read_model_configs(LOCAL_PATHS["excel"])
        except (FileNotFoundError, OSError, ValueError):
            return None
        if not configs:
            return None
        self._config_payload = {
            str(config_name): list(values)
            for config_name, values in configs.items()
        }
        return {
            config_name: list(values)
            for config_name, values in self._config_payload.items()
        }

    def _send_request(self, request: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"{self.config.server_host}:{self.config.server_port}"
        try:
            with socket.create_connection(
                (self.config.server_host, self.config.server_port),
                timeout=self.config.request_timeout,
            ) as sock:
                sock.sendall(serialize(request))
                response = sock.recv(1024 * 1024)
        except (ConnectionError, OSError) as exc:
            raise RuntimeError(
                "LocalWorker 无法连接 daemon IPC "
                f"({endpoint}): {exc}. "
                "请确认 AutoFluid daemon 正在运行，且 AUTOFLUID_SERVER_HOST/AUTOFLUID_IPC_HOST/AUTOFLUID_IPC_PORT 指向正确的 IPC 端点。"
            ) from exc
        if not response:
            raise RuntimeError(f"LocalWorker 未收到 daemon IPC 响应 ({endpoint})")
        decoded = deserialize(response)
        if decoded is None:
            raise RuntimeError("LocalWorker 收到无法解析的 IPC 响应")
        if decoded.get("status") == "error":
            command = str(request.get("command") or "unknown")
            message = str(decoded.get("message") or "未知错误")
            raise RuntimeError(f"LocalWorker IPC 请求失败 [{command}]: {message}")
        return decoded


def build_worker_network() -> dict[str, Any]:
    """Collect local network metadata for daemon-side diagnostics."""
    network: dict[str, Any] = {}
    reachable_host = os.environ.get("AUTOFLUID_WORKER_REACHABLE_HOST", "").strip()
    connectivity_mode = os.environ.get("AUTOFLUID_WORKER_CONNECTIVITY_MODE", "").strip()
    ssh_port = int(os.environ.get("AUTOFLUID_WORKER_SSH_PORT", "22"))
    candidate_hosts = _candidate_hosts_from_env() + detect_candidate_hosts()
    candidate_hosts = list(dict.fromkeys(host for host in candidate_hosts if host))

    public_ip = os.environ.get("AUTOFLUID_WORKER_PUBLIC_IP", "").strip()
    if not public_ip and os.environ.get("AUTOFLUID_DISCOVER_PUBLIC_IP", "1") != "0":
        public_ip = discover_public_ip()

    if public_ip:
        network["public_ip"] = public_ip
    if candidate_hosts:
        network["candidate_hosts"] = candidate_hosts
    if reachable_host:
        network["reachable_host"] = reachable_host
    if connectivity_mode:
        network["connectivity_mode"] = connectivity_mode
    network["ssh_port"] = ssh_port
    return network


def detect_candidate_hosts() -> list[str]:
    """Return local interface addresses useful for operator diagnostics."""
    hosts: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            hosts.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        _hostname, _aliases, addresses = socket.gethostbyname_ex(socket.gethostname())
        hosts.extend(addresses)
    except OSError:
        pass
    return list(dict.fromkeys(host for host in hosts if not host.startswith("127.")))


def discover_public_ip(timeout: float = 3.0) -> str:
    """Best-effort public IP discovery for diagnostics."""
    url = os.environ.get("AUTOFLUID_PUBLIC_IP_URL", DEFAULT_PUBLIC_IP_URL)
    try:
        with urlopen(url, timeout=timeout) as response:
            public_ip: str = response.read(128).decode("utf-8").strip()
            return public_ip
    except (OSError, URLError):
        return ""


def _candidate_hosts_from_env() -> list[str]:
    raw = os.environ.get("AUTOFLUID_WORKER_CANDIDATE_HOSTS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def main() -> int:
    """Run the LocalWorker registration client."""
    parser = argparse.ArgumentParser(description="Run AutoFluid LocalWorker heartbeat client")
    parser.add_argument("--once", action="store_true", help="register once and send one heartbeat")
    parser.add_argument("--run-task-file", help=argparse.SUPPRESS)
    parser.add_argument("--result-file", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.run_task_file and args.result_file:
        return _run_task_file(args.run_task_file, args.result_file)
    try:
        worker = LocalWorker.from_env()
        if args.once:
            worker.register_once()
            worker.heartbeat_once()
            return 0
        worker.run_forever()
        return 0
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


def _run_task_file(task_file: str, result_file: str) -> int:
    try:
        with open(task_file, encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise RuntimeError("任务文件格式无效")
        step = str(payload.get("step") or "")
        params = payload.get("params")
        if not isinstance(params, dict):
            params = {}
        worker = LocalWorker.from_env()
        result = worker._execute_task(step, params)
        output = {"ok": True, "result": result}
        exit_code = 0
    except Exception as exc:
        output = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        exit_code = 1
    with open(result_file, "w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
