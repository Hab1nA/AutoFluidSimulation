"""
===============================================================================
后台守护进程 (Pipeline Daemon)
独立的 Python 进程，负责：
1. 启动 IPC 服务器，监听 TUI 客户端命令
2. 管理共享状态数据库（StateManager）
3. 运行流水线调度器（PipelineScheduler）
4. 响应客户端命令并执行相应操作

启动方式：
    python start_daemon.py
    或
    python main.py --daemon

进程锁：
    同一时刻只允许一个 Daemon 运行。通过 PID 文件 + IPC 端口探测实现。

数据库分片：
    按构型组合指纹自动选择数据库文件。相同构型组合复用同一数据库，
    修改设计表后自动使用新数据库，互不干扰。
===============================================================================
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import sys
import signal
import sqlite3
import subprocess
import threading
import time
from typing import Any, Mapping

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config import (
    DEFAULT_WORKSTATION_ID,
    LOCAL_PATHS, IPC_CONFIG, STEP_NAMES, WORKSTATIONS,
    STATUS_COMPLETED, STATUS_ERROR, STATUS_PAUSED, STATUS_RETRYING, STATUS_RUNNING,
    STATUS_UNKNOWN_REMOTE, STATUS_WAITING,
    ensure_directories, get_step_filename, get_workstation_ssh_health_interval,
    is_server_mode, validate_config_details,
)
from engine.config_fingerprint import compute_config_fingerprint, get_db_path_for_fingerprint
from engine.local_worker_adapter import LocalWorkerAdapter
from engine.local_worker_registry import LocalWorkerRegistry
from engine.state_manager import StateManager
from engine.task_runner import TaskRunner
from engine.scheduler import PipelineScheduler
from engine.workstation_ssh_recovery import default_recovery_manager
from ipc.server import IPCServer
from utils.log_paths import service_log_file
from utils.infrastructure import InfrastructureUnavailableError
from utils.logger import setup_logger, install_broadcast_handler, get_broadcast_handler
from utils.excel_reader import read_model_configs
from utils.process_utils import (
    check_ipc_ready,
    cleanup_tunnel_watchdog_tasks,
    cleanup_worker_processes_from_pid_files,
    is_process_alive,
    read_pid_file,
    remove_pid_file,
    worker_pid_file,
    write_pid_file,
)

logger = setup_logger("PipelineDaemon")
_MAX_DASHBOARD_LOG_BYTES = 900_000

# PID 文件路径（与 main.py 保持一致）
_PID_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_DAEMON_PID_FILE = os.path.join(_PID_DIR, "daemon.pid")
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOCAL_WORKER_AUTOSTART_COOLDOWN_SECONDS = 30.0
_ALERT_WATCHER_STOP_TIMEOUT_SECONDS = 5.0
_ALERT_WATCHER_RESTART_COOLDOWN_SECONDS = 30.0
_CHILD_HEALTH_CHECK_INTERVAL_SECONDS = 10.0
_DEFAULT_WORKSTATION_SSH_ACTIVE_PROBE_INTERVAL_SECONDS = 300.0


def _json_size_bytes(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _workstation_ssh_active_probe_interval_seconds() -> float:
    raw_value = os.environ.get("AUTOFLUID_WORKSTATION_SSH_ACTIVE_PROBE_INTERVAL")
    if raw_value is None:
        return _DEFAULT_WORKSTATION_SSH_ACTIVE_PROBE_INTERVAL_SECONDS
    try:
        return max(1.0, float(raw_value))
    except ValueError:
        logger.warning(
            "[Worker] AUTOFLUID_WORKSTATION_SSH_ACTIVE_PROBE_INTERVAL=%r 无效，使用默认值 %.1f",
            raw_value,
            _DEFAULT_WORKSTATION_SSH_ACTIVE_PROBE_INTERVAL_SECONDS,
        )
        return _DEFAULT_WORKSTATION_SSH_ACTIVE_PROBE_INTERVAL_SECONDS


def _state_db_config_count(db_path: str) -> int:
    """Return how many configs are stored in an existing state DB."""
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute("SELECT COUNT(*) FROM configs").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row is not None else 0


def _latest_state_db_with_configs(data_dir: str) -> str | None:
    """Find the newest fingerprinted state DB that still has configs."""
    try:
        entries = list(os.scandir(data_dir))
    except OSError:
        return None

    candidates = [
        entry
        for entry in entries
        if entry.is_file()
        and entry.name.startswith("pipeline_state_")
        and entry.name.endswith(".db")
    ]
    candidates.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
    for entry in candidates:
        if _state_db_config_count(entry.path) > 0:
            return entry.path
    return None


# ------------------------------------------------------------------
# 进程锁工具
# ------------------------------------------------------------------

def acquire_process_lock() -> bool:
    """
    尝试获取进程锁。

    检查逻辑：
    1. PID 文件存在 → 进程存活 → IPC 有响应 → 另一个 Daemon 运行中 → 拒绝
    2. PID 文件存在 → 进程存活 → IPC 无响应 → 僵尸残留 → 清理后继续
    3. PID 文件存在 → 进程已死 → 僵尸残留 → 清理后继续
    4. PID 文件不存在 → 全新启动 → 写入 PID 后继续

    Returns:
        True:  成功获取锁（可以启动）
        False: 已有另一个 Daemon 在运行（拒绝启动）
    """
    stale_pid = read_pid_file(_DAEMON_PID_FILE)

    if stale_pid is not None:
        if is_process_alive(stale_pid):
            if check_ipc_ready(IPC_CONFIG["host"], IPC_CONFIG["port"]):
                logger.warning(
                    f"已有 Daemon 实例运行中 (PID: {stale_pid})，"
                    f"IPC {IPC_CONFIG['host']}:{IPC_CONFIG['port']} 已监听，拒绝重复启动"
                )
                return False
            else:
                logger.warning(
                    f"PID 文件存在且进程存活 (PID: {stale_pid})，"
                    f"但 IPC 无响应，视为僵尸残留，清理后继续"
                )
        else:
            logger.info(f"PID 文件中的进程已退出 (PID: {stale_pid})，清理残留 PID 文件")

        remove_pid_file(_DAEMON_PID_FILE)

    current_pid = os.getpid()
    write_pid_file(_DAEMON_PID_FILE, current_pid)
    logger.info(f"进程锁已获取 (PID: {current_pid})")
    return True


def release_process_lock():
    """释放进程锁（删除 PID 文件）。"""
    current_pid = os.getpid()
    stale_pid = read_pid_file(_DAEMON_PID_FILE)
    if stale_pid == current_pid:
        remove_pid_file(_DAEMON_PID_FILE)
        logger.info(f"进程锁已释放 (PID: {current_pid})")
    else:
        logger.debug(f"PID 文件不属于当前进程 (文件PID: {stale_pid}, 当前PID: {current_pid})，跳过清理")


class PipelineDaemon:
    """
    流水线后台守护进程。

    负责协调所有子系统：状态管理、任务调度、IPC 通信。
    作为独立进程运行，TUI 客户端通过 IPC 与之交互。
    """

    def __init__(self):
        """初始化守护进程基础环境（不创建业务组件）。"""
        logger.info("=" * 60)
        logger.info("PipelineDaemon 初始化中...")
        logger.info("=" * 60)

        # 0. 安装日志广播处理器（供 TUI 增量拉取）
        install_broadcast_handler(capacity=1000)

        # 业务组件在 start() 中创建，因为需要先读取 Excel 确定数据库路径
        self.state = None
        self.runner = None
        self.scheduler = None
        self.ipc_server = None
        self.local_worker_registry = LocalWorkerRegistry()
        self.local_worker_adapter = LocalWorkerAdapter(self.local_worker_registry)
        self._config_load_error: str | None = None
        self._worker_config_fingerprint: str | None = None
        self._local_worker_process: subprocess.Popen | None = None
        self._local_worker_last_start_attempt = 0.0
        self._alert_watcher_process: subprocess.Popen | None = None
        self._alert_watcher_last_start_attempt = 0.0
        self._last_child_health_check = 0.0
        self._child_health_check_interval_seconds = _CHILD_HEALTH_CHECK_INTERVAL_SECONDS
        self._started_at_epoch: float | None = None
        self._last_worker_ssh_checks: dict[str, str] = {}
        self._last_worker_ssh_check_times: dict[str, float] = {}
        self._ssh_health_thread: threading.Thread | None = None
        self._ssh_health_stop_event = threading.Event()
        self._ssh_health_interval_seconds = get_workstation_ssh_health_interval()
        self._ssh_health_active_probe_interval_seconds = (
            _workstation_ssh_active_probe_interval_seconds()
        )
        self._ssh_health_probe_lock = threading.Lock()
        self._ssh_health_force_active_probe = threading.Event()
        self._ssh_health_wake_event = threading.Event()
        self._last_worker_ssh_active_probe_time = time.time()
        self._last_worker_ssh_check_sources: dict[str, str] = {}
        self._workstation_ssh_recovery = default_recovery_manager(
            _PROJECT_ROOT,
            local_worker_adapter=self.local_worker_adapter,
        )
        self._config_warnings: list[str] = []

        # 运行标志
        self._running = False
        self._stop_event = threading.Event()  # 主循环阻塞用，set() 唤醒
        self._pipeline_ever_started = False  # 一旦流水线启动过即置 True，锁定配置
        self._preserve_pipeline_on_shutdown = True

        logger.info("PipelineDaemon 基础环境就绪")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self):
        """启动守护进程。"""
        logger.info("=" * 60)
        logger.info("PipelineDaemon 启动中...")
        logger.info("=" * 60)

        # ---- 0. 进程锁 ----
        if not acquire_process_lock():
            logger.error("进程锁获取失败，退出启动")
            self._running = False
            return

        # ---- 0.5 加载 TOML 配置 & 验证 ----
        from engine.config import reload_config_from_toml
        reload_config_from_toml()
        ensure_directories()
        config_issues = validate_config_details()
        self._config_warnings = [issue.message for issue in config_issues]
        for issue in config_issues:
            if issue.log_level == "warning":
                logger.warning("[CONFIG] %s", issue.message)
            else:
                logger.info("[CONFIG] %s", issue.message)

        self._running = True
        self._started_at_epoch = time.time()

        # ---- 1. 加载 Excel 数据，计算构型指纹，确定数据库路径 ----
        excel_path = LOCAL_PATHS["excel"]
        configs: dict[int, list[float]] = {}
        fingerprint: str | None = None
        if is_server_mode():
            self._config_load_error = "ServerMode 等待 LocalWorker 提供构型数据"
        else:
            try:
                configs = read_model_configs(excel_path)
            except FileNotFoundError as e:
                self._config_load_error = f"Excel 文件未找到: {e}"
            except (ValueError, OSError) as e:
                self._config_load_error = f"Excel 读取失败: {e}"

        if self._config_load_error is None and not configs:
            self._config_load_error = "Excel 中未读取到任何构型数据"

        if self._config_load_error and not is_server_mode():
            logger.error(self._config_load_error)
            release_process_lock()
            self._running = False
            return

        if self._config_load_error:
            recovered_db_path = _latest_state_db_with_configs(str(LOCAL_PATHS["data_dir"]))
            if recovered_db_path is not None:
                logger.warning(
                    "[ServerMode] %s；已恢复现有状态数据库: %s",
                    self._config_load_error,
                    recovered_db_path,
                )
                db_path = recovered_db_path
                self._config_load_error = None
                logger.info(f"状态数据库: {db_path}")
            else:
                logger.warning(
                    "[ServerMode] %s；仅启动 IPC/LocalWorker 控制面，start 将拒绝启动流水线",
                    self._config_load_error,
                )
                db_path = None
        else:
            fingerprint = compute_config_fingerprint(configs)
            db_path = get_db_path_for_fingerprint(fingerprint)
            logger.info(f"构型组合指纹: {fingerprint}（{len(configs)} 个构型）")
            logger.info(f"状态数据库: {db_path}")

        # ---- 2. 创建业务组件 ----
        if db_path is not None:
            self.state = StateManager(db_path=db_path)
            if configs:
                self.state.load_configs(configs)
                self._assign_config_workstations()
                logger.info(f"已同步 {len(configs)} 个构型到状态库")

            self.runner = TaskRunner(self.state, local_worker_adapter=self.local_worker_adapter)
            self.scheduler = PipelineScheduler(self.state, self.runner)
            self._restore_remote_tasks_from_db()

            # ---- 2.5 启动时状态一致性检查 ----
            # 新进程没有调度器线程，残留的 paused/running 状态一定是不一致的
            # （stop() 挂起或进程被杀导致 set_engine_status("stopped") 未执行）
            stale_status = self.state.get_engine_status()
            remote_tasks = self.state.get_all_remote_tasks()
            if stale_status in ("paused", "running") and not remote_tasks:
                logger.warning(
                    f"检测到残留引擎状态 '{stale_status}'（可能是上次退出时 stop() 未完成），"
                    f"重置为 stopped"
                )
                self.state.set_engine_status("stopped")

        self.ipc_server = IPCServer()
        self.ipc_server.register_default_handlers(self)

        # ---- 3. 启动 IPC 服务器 ----
        try:
            self.ipc_server.start()
        except OSError as e:
            logger.error(f"IPC 服务器启动失败: {e}")
            logger.error("可能已有另一个 Daemon 在运行？")
            self._running = False
            self.ipc_server.stop()
            release_process_lock()
            return

        self._start_workstation_ssh_health_monitor()
        self._start_alert_watcher()

        # ---- 4. 注册信号处理 ----
        self._setup_signal_handlers()

        logger.info("PipelineDaemon 已就绪，等待客户端指令...")
        logger.info(f"IPC 地址: {IPC_CONFIG['host']}:{IPC_CONFIG['port']}")
        logger.info(f"数据库指纹: {fingerprint or '未加载'}")

        # ---- 5. 主循环 ----
        try:
            # 必须带 timeout：无超时的 wait() 底层是 C 级 Lock.acquire()，
            # 不执行 Python 字节码，导致 KeyboardInterrupt 无法在 Windows 上被抛出。
            while not self._stop_event.wait(timeout=1.0):
                self._check_child_process_health()
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，守护进程正在退出...")
        finally:
            self.shutdown()

    def shutdown(self, *, preserve_pipeline: bool | None = None):
        """优雅关闭守护进程。

        每个清理步骤独立 try-except，确保任何一步失败都不阻断后续清理。
        """
        if preserve_pipeline is None:
            preserve_pipeline = bool(getattr(self, "_preserve_pipeline_on_shutdown", True))
        logger.info("PipelineDaemon 正在关闭...")
        self._running = False
        self._stop_event.set()
        self._stop_workstation_ssh_health_monitor()

        # 停止调度器（内部已包含 disconnect_ssh）
        if self.scheduler:
            try:
                self.scheduler.stop(cancel_remote_tasks=not preserve_pipeline)
            except Exception as e:
                logger.warning(f"调度器停止异常: {e}")
            if not preserve_pipeline:
                self._cancel_tracked_remote_tasks_on_shutdown()

        # 调度器不存在时才需要单独断开 SSH
        elif self.runner:
            try:
                self.runner.disconnect_ssh()
            except Exception as e:
                logger.warning(f"SSH 断开异常: {e}")

        # 停止 daemon 拥有的告警 watcher，避免 IPC 关闭后子进程残留
        self._stop_alert_watcher()

        # 停止 daemon 自动唤起的 LocalWorker，并清理跨会话残留 worker/tunnel PID。
        self._stop_local_worker_process()
        try:
            watchdog_results = cleanup_tunnel_watchdog_tasks(_PROJECT_ROOT)
            if watchdog_results.get("status") != "skipped":
                logger.info("[Worker] shutdown watchdog 清理结果: %s", watchdog_results)
        except Exception as e:
            logger.warning("[Worker] shutdown watchdog 清理异常: %s", e)
        try:
            cleanup_results = cleanup_worker_processes_from_pid_files()
            if cleanup_results:
                logger.info("[Worker] shutdown PID 清理结果: %s", cleanup_results)
        except Exception as e:
            logger.warning("[Worker] shutdown PID 清理异常: %s", e)

        # 停止 IPC 服务器
        if self.ipc_server:
            try:
                self.ipc_server.stop()
            except Exception as e:
                logger.warning(f"IPC 服务器停止异常: {e}")

        # 释放进程锁
        try:
            release_process_lock()
        except Exception as e:
            logger.warning(f"进程锁释放异常: {e}")

        logger.info("PipelineDaemon 已关闭")

    def _restore_remote_tasks_from_db(self) -> None:
        """Rebuild in-memory remote-task tracking after daemon restart."""
        runner = getattr(self, "runner", None)
        if runner is None:
            return
        get_remote_executor = getattr(runner, "get_remote_executor", None)
        if not callable(get_remote_executor):
            return
        remote_executor = get_remote_executor()
        restore_remote_tasks = getattr(remote_executor, "restore_remote_tasks_from_db", None)
        if callable(restore_remote_tasks):
            restore_remote_tasks()

    def _cancel_tracked_remote_tasks_on_shutdown(self) -> None:
        """Best-effort remote task cleanup if scheduler.stop() exits early."""
        runner = getattr(self, "runner", None)
        if runner is None:
            return
        try:
            remote_executor = runner.get_remote_executor()
            cancel_remote_tasks = getattr(remote_executor, "cancel_all_tracked_remote_tasks", None)
            if callable(cancel_remote_tasks):
                results = cancel_remote_tasks()
                if results.get("cancelled") or results.get("failed"):
                    logger.info("[Daemon] shutdown 兜底远程任务清理结果: %s", results)
        except Exception as e:
            logger.warning("[Daemon] shutdown 兜底远程任务清理异常: %s", e)

    def _stop_local_worker_process(self) -> None:
        """Stop the LocalWorker child process owned by this daemon instance."""
        process = getattr(self, "_local_worker_process", None)
        self._local_worker_process = None
        if process is None:
            return
        if process.poll() is not None:
            remove_pid_file(worker_pid_file("local_worker"))
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=3)
            except (OSError, subprocess.SubprocessError) as e:
                logger.warning("[LocalWorker] 强制终止异常: %s", e)
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("[LocalWorker] 终止异常: %s", e)
        finally:
            remove_pid_file(worker_pid_file("local_worker"))

    def _assign_config_workstations(self) -> None:
        """Compatibility hook; workstation assignment is now claim-time dynamic."""
        if self.state is None:
            raise RuntimeError("StateManager 未初始化，请先调用 start()")

        logger.debug(
            "[Config] 工作站分配采用动态槽位模式：构型将在 Transfer 前按空闲 Meshing 槽位 claim"
        )

    def _start_alert_watcher(self) -> None:
        """Start the server-side alert watcher as a daemon-owned child process."""
        if not is_server_mode():
            return
        webhook_url = os.environ.get("AUTOFLUID_OPENCLAW_WEBHOOK_URL", "").strip()
        if not webhook_url:
            logger.warning("[AlertWatcher] 未配置 AUTOFLUID_OPENCLAW_WEBHOOK_URL，跳过启动")
            return

        existing_process = getattr(self, "_alert_watcher_process", None)
        if existing_process is not None and existing_process.poll() is None:
            return

        now = time.monotonic()
        last_attempt = float(getattr(self, "_alert_watcher_last_start_attempt", 0.0))
        if now - last_attempt < _ALERT_WATCHER_RESTART_COOLDOWN_SECONDS:
            return
        self._alert_watcher_last_start_attempt = now

        env = os.environ.copy()
        env["AUTOFLUID_IPC_HOST"] = str(IPC_CONFIG["host"])
        env["AUTOFLUID_IPC_PORT"] = str(IPC_CONFIG["port"])
        auth_token = str(IPC_CONFIG.get("auth_token", "") or "")
        if auth_token:
            env["AUTOFLUID_IPC_AUTH_TOKEN"] = auth_token

        log_path = service_log_file("alert-watcher", "alert_watcher.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        try:
            with open(log_path, "a", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    [
                        self._daemon_python_executable(),
                        "-m",
                        "tools.autofluid_cli",
                        "alerts",
                        "watch",
                    ],
                    cwd=_PROJECT_ROOT,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=env,
                    creationflags=creationflags,
                )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("[AlertWatcher] 启动失败: %s", e)
            return

        self._alert_watcher_process = process
        logger.info("[AlertWatcher] 已启动: pid=%s, log=%s", process.pid, log_path)

    def _check_child_process_health(self) -> None:
        """Periodically restart daemon-owned children that have exited."""
        now = time.monotonic()
        last_check = float(getattr(self, "_last_child_health_check", 0.0))
        interval = float(
            getattr(
                self,
                "_child_health_check_interval_seconds",
                _CHILD_HEALTH_CHECK_INTERVAL_SECONDS,
            )
        )
        if now - last_check < interval:
            return
        self._last_child_health_check = now
        self._check_child_process_health_once()

    def _check_child_process_health_once(self) -> None:
        """Restart daemon-owned child processes after unexpected exit."""
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is not None and stop_event.is_set():
            return

        alert_process = getattr(self, "_alert_watcher_process", None)
        if alert_process is not None and alert_process.poll() is not None:
            logger.warning("[AlertWatcher] 子进程已退出，准备按退避策略重启")
            self._alert_watcher_process = None
            self._start_alert_watcher()

        worker_process = getattr(self, "_local_worker_process", None)
        if worker_process is not None and worker_process.poll() is not None:
            logger.warning("[LocalWorker] 自动唤起的子进程已退出，准备按退避策略重启")
            self._local_worker_process = None
            self._ensure_local_worker_autostarted()

    def _stop_alert_watcher(self) -> None:
        """Stop the daemon-owned alert watcher child process if it is still alive."""
        process = getattr(self, "_alert_watcher_process", None)
        self._alert_watcher_process = None
        if process is None or process.poll() is not None:
            return

        try:
            process.terminate()
            process.wait(timeout=_ALERT_WATCHER_STOP_TIMEOUT_SECONDS)
            logger.info("[AlertWatcher] 已停止: pid=%s", process.pid)
        except subprocess.TimeoutExpired:
            logger.warning("[AlertWatcher] 停止超时，强制结束: pid=%s", process.pid)
            process.kill()
            process.wait(timeout=_ALERT_WATCHER_STOP_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("[AlertWatcher] 停止异常: %s", e)

    def _setup_signal_handlers(self):
        """设置系统信号处理器。"""
        def signal_handler(signum, frame):
            logger.info(f"收到信号 {signum}，正在关闭...")
            self._running = False
            self._stop_event.set()

        # SIGTERM 用于外部 kill 命令优雅终止
        for sig in [signal.SIGTERM]:
            try:
                signal.signal(sig, signal_handler)
            except (AttributeError, ValueError):
                pass  # Windows 不支持某些信号

    # ------------------------------------------------------------------
    # IPC 命令处理器
    # ------------------------------------------------------------------

    def handle_start(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """处理 start 命令（启动或继续流水线）。

        状态机：
        - running → 已是运行中，若暂停标志不一致则修复
        - paused  → 检查调度器状态后恢复或重启
        - stopped / 其他 → 全新启动
        """
        if self.state is None or self.scheduler is None:
            raise RuntimeError("Daemon 组件未初始化，请先调用 start()")
        config_load_error = getattr(self, "_config_load_error", None)
        if config_load_error:
            if is_server_mode():
                self._ensure_local_worker_autostarted()
            return (
                False,
                None,
                f"server 模式下构型数据未就绪，不能启动流水线: {config_load_error}",
            )
        engine_status = self.state.get_engine_status()

        if engine_status == "running":
            # 二次确认：检查调度器的暂停标志是否被意外置位
            # （防止 start_pipeline 末尾覆盖 engine_status 导致的不一致）
            if self.scheduler.is_paused:
                latest_status = self.state.get_engine_status()
                if latest_status == "paused":
                    logger.info("检测到暂停状态已同步，start 命令保持暂停，不执行恢复")
                    return True, None, "流水线已暂停，使用 start 可在状态稳定后恢复"
                logger.info("检测到引擎状态为 running 但调度器暂停标志已置位，执行恢复")
                self.scheduler.resume()
                return True, None, "流水线已恢复运行（修正不一致状态）"
            if not self.scheduler.pipeline_alive:
                logger.info("检测到引擎状态为 running 但调度器线程已退出，重新启动流水线")
                self.state.set_engine_status("running")
                self._pipeline_ever_started = True
                scheduler_thread = threading.Thread(
                    target=self.scheduler.start_pipeline,
                    daemon=True,
                    name="SchedulerMain",
                )
                scheduler_thread.start()
                self.scheduler.set_pipeline_thread(scheduler_thread)
                return True, None, "流水线已重新启动（调度线程恢复）"
            return True, None, "流水线已在运行中"

        if engine_status == "paused":
            if self._server_mode_requires_worker():
                self._ensure_local_worker_autostarted()
                return self._server_mode_worker_missing_response()
            auth_error = self._server_mode_remote_auth_error()
            if auth_error:
                return False, None, auth_error
            ssh_error = self._server_mode_ssh_not_ready()
            if ssh_error:
                return False, None, ssh_error
            # 暂停状态下恢复运行
            # 检查调度器主线程是否存活（SW 宏执行期间线程可能因异常退出）
            if not self.scheduler.pipeline_alive and not self.scheduler.is_paused:
                # 调度器线程已死亡且暂停标志未置位：
                # 可能因 SW 失败等原因退出，但引擎状态未正确切换为 stopped
                # 重新启动流水线（start_pipeline 会检查断点续传）
                logger.info("检测到调度器线程已退出且未暂停，重新启动流水线...")
                self.state.set_engine_status("running")
                self._pipeline_ever_started = True
                scheduler_thread = threading.Thread(
                    target=self.scheduler.start_pipeline,
                    daemon=True,
                    name="SchedulerMain"
                )
                scheduler_thread.start()
                self.scheduler.set_pipeline_thread(scheduler_thread)
                return True, None, "流水线已重新启动（从断点恢复）"
            # 正常暂停恢复：pipeline 存活或暂停标志正常置位
            self.scheduler.resume()
            return True, None, "流水线已恢复运行"

        # 全新启动（engine_status 为 stopped 或其他）
        if self._server_mode_requires_worker():
            self._ensure_local_worker_autostarted()
            return self._server_mode_worker_missing_response()
        auth_error = self._server_mode_remote_auth_error()
        if auth_error:
            return False, None, auth_error
        ssh_error = self._server_mode_ssh_not_ready()
        if ssh_error:
            return False, None, ssh_error

        self.state.set_engine_status("running")
        self._pipeline_ever_started = True

        # 在独立线程中启动调度器（避免阻塞 IPC 响应）
        scheduler_thread = threading.Thread(
            target=self.scheduler.start_pipeline,
            daemon=True,
            name="SchedulerMain"
        )
        scheduler_thread.start()
        self.scheduler.set_pipeline_thread(scheduler_thread)

        return True, None, "流水线已启动"

    def _server_mode_requires_worker(self) -> bool:
        """Return True when server mode cannot execute local SW/SC yet."""
        state = getattr(self, "state", None)
        if is_server_mode() and state is not None:
            try:
                config_names = state.get_all_configs()
                if config_names and all(
                    state.get_step_status(config_name, "sw") == "Completed"
                    and state.get_step_status(config_name, "sc") == "Completed"
                    for config_name in config_names
                ):
                    return False
            except Exception as e:
                logger.warning(f"[ServerMode] 检查本地步骤状态失败，要求 LocalWorker 在线: {e}")

        registry = getattr(self, "local_worker_registry", None)
        has_worker = (
            registry is not None
            and registry.has_online_worker()
        )
        has_adapter = getattr(self, "local_worker_adapter", None) is not None
        return is_server_mode() and (not has_worker or not has_adapter)

    @staticmethod
    def _server_mode_remote_auth_error() -> str | None:
        """Return an operator-facing error when server-mode SSH auth is absent."""
        if not is_server_mode():
            return None
        missing: list[str] = []
        for workstation in WORKSTATIONS:
            auth_method = str(workstation.get("auth_method") or "password").lower()
            if auth_method in {"key", "none"}:
                continue
            password = str(workstation.get("password") or "").strip()
            if not password or (password.startswith("${") and password.endswith("}")):
                missing.append(str(workstation.get("id") or "default"))
        if not missing:
            return None
        return (
            "server 模式下 ocar 需要工作站 SSH 密码才能执行 transfer/meshing/solver；"
            f"缺少工作站 {', '.join(missing)} 的密码。请在 ocar 运行环境设置 "
            "AUTOFLUID_SSH_PASSWORD 或对应 workstations[].password 环境变量。"
        )

    def _server_mode_ssh_not_ready(self) -> str | None:
        """Return an operator-facing error when the last worker SSH check failed."""
        if not is_server_mode():
            return None
        try:
            health = self._build_health_snapshot()
            if health.get("server_to_workstation_ssh") == "ok":
                return None
        except Exception as exc:
            logger.debug("[ServerMode] 检查工作站 SSH 健康状态失败: %s", exc)

        ssh_checks = getattr(self, "_last_worker_ssh_checks", {})
        if not ssh_checks:
            return "server 模式下工作站 SSH 未就绪，不能启动流水线；请先执行 worker start 建立 SSH 连通性。"
        failed_checks = {
            workstation_id: status
            for workstation_id, status in ssh_checks.items()
            if status != "ok"
        }
        if not failed_checks:
            return None
        return (
            "server 模式下工作站 SSH 未就绪，不能启动流水线；"
            f"请先执行 worker start 建立 SSH 连通性: {failed_checks}"
        )

    @staticmethod
    def _server_mode_worker_missing_response() -> tuple[bool, None, str]:
        return (
            False,
            None,
            "server 模式下必须先接入 LocalWorker 才能启动流水线；已尝试自动唤起本机 LocalWorker",
        )

    def _ensure_local_worker_autostarted(self) -> None:
        """Start the local worker process once when server mode needs one."""
        if not is_server_mode():
            return
        autostart_override = os.environ.get("AUTOFLUID_LOCAL_WORKER_AUTOSTART", "").lower()
        if sys.platform != "win32" and autostart_override not in {"1", "true", "yes"}:
            logger.debug("[LocalWorker] 非 Windows 环境跳过本机 LocalWorker 自动唤起")
            return
        if getattr(self, "local_worker_adapter", None) is None:
            return
        registry = getattr(self, "local_worker_registry", None)
        if registry is not None and registry.has_online_worker():
            return

        existing_process = getattr(self, "_local_worker_process", None)
        if existing_process is not None and existing_process.poll() is None:
            return

        now = time.monotonic()
        last_attempt = float(getattr(self, "_local_worker_last_start_attempt", 0.0))
        if now - last_attempt < _LOCAL_WORKER_AUTOSTART_COOLDOWN_SECONDS:
            return
        self._local_worker_last_start_attempt = now

        worker_script = os.path.join(_PROJECT_ROOT, "main.py")
        if not os.path.isfile(worker_script):
            logger.warning("[LocalWorker] 自动唤起失败，启动脚本不存在: %s", worker_script)
            return

        python_exe = self._local_worker_python_executable()
        env = os.environ.copy()
        env.setdefault("AUTOFLUID_IPC_HOST", str(IPC_CONFIG["host"]))
        env.setdefault("AUTOFLUID_IPC_PORT", str(IPC_CONFIG["port"]))
        env.setdefault("AUTOFLUID_WORKER_REACHABLE_HOST", "127.0.0.1")
        env.setdefault("AUTOFLUID_WORKER_SSH_PORT", "2223")
        env.setdefault("AUTOFLUID_WORKER_CONNECTIVITY_MODE", "reverse_tunnel")

        log_path = service_log_file("local-worker", "local_worker_autostart.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        try:
            with open(log_path, "a", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    [python_exe, worker_script, "--worker"],
                    cwd=_PROJECT_ROOT,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=env,
                    creationflags=creationflags,
                )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("[LocalWorker] 自动唤起失败: %s", e)
            return

        self._local_worker_process = process
        write_pid_file(worker_pid_file("local_worker"), process.pid)
        logger.info(
            "[LocalWorker] 已自动唤起本机 LocalWorker: pid=%s, log=%s",
            process.pid,
            log_path,
        )

    @staticmethod
    def _local_worker_python_executable() -> str:
        return PipelineDaemon._daemon_python_executable()

    @staticmethod
    def _daemon_python_executable() -> str:
        if sys.platform == "win32":
            candidate = os.path.join(_PROJECT_ROOT, ".venv", "Scripts", "python.exe")
        else:
            candidate = os.path.join(_PROJECT_ROOT, ".venv", "bin", "python")
        if os.path.isfile(candidate):
            return candidate
        return sys.executable

    def handle_pause(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """处理 pause 命令。

        暂停行为取决于当前所处阶段：
        - SW 宏执行中：标志置位，宏继续运行但完成后立即暂停
        - 下游步骤执行中：当前步骤完成后暂停
        - 空闲状态：直接标记为暂停
        """
        if self.state is None or self.scheduler is None:
            raise RuntimeError("Daemon 组件未初始化，请先调用 start()")
        engine_status = self.state.get_engine_status()
        if engine_status == "paused":
            return True, None, "流水线已在暂停状态"
        if engine_status == "stopped":
            return True, None, "流水线已停止，无需暂停"

        self.scheduler.pause()
        return True, None, "流水线已暂停（当前运行步骤完成后不再取新任务）"

    def handle_stop(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """处理 full_quit 命令。"""
        logger.info("收到 full_quit 命令，准备完全退出...")
        self._preserve_pipeline_on_shutdown = False
        # 统一退出路径：仅设置 _stop_event，由 run() 的 finally 块执行 shutdown()
        self._stop_event.set()
        return True, None, "后台引擎正在安全退出..."

    def handle_check(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """处理 check 命令（系统自检）。"""
        if self.runner is None:
            raise RuntimeError("TaskRunner 未初始化，请先调用 start()")
        from engine.config import reload_config_from_toml

        reload_config_from_toml()
        results = dict(self.runner.run_system_check())
        self._sync_workstation_ssh_checks_from_system_check(results)
        results["health"] = self._build_health_snapshot()
        self._refresh_check_summary_from_health(results)
        return True, results, "系统自检完成"

    def _sync_workstation_ssh_checks_from_system_check(self, results: Mapping[str, Any]) -> None:
        """Promote active system-check SSH results into the start readiness cache."""
        remote_checks = results.get("remote_checks")
        if not isinstance(remote_checks, Mapping):
            return
        workstations = remote_checks.get("workstations")
        if not isinstance(workstations, Mapping):
            return
        ssh_checks: dict[str, str] = {}
        for workstation_id, checks in workstations.items():
            if not isinstance(checks, Mapping):
                continue
            ssh_connected = checks.get("ssh_connected")
            ssh_status = str(checks.get("ssh") or "")
            if ssh_connected is True or "成功" in ssh_status:
                ssh_checks[str(workstation_id)] = "ok"
            elif ssh_connected is False or "失败" in ssh_status:
                ssh_checks[str(workstation_id)] = "disconnected"
            elif ssh_status.startswith("错误"):
                ssh_checks[str(workstation_id)] = f"error: {ssh_status}"
        if not ssh_checks:
            return
        self._last_worker_ssh_checks = ssh_checks
        now = time.time()
        self._last_worker_ssh_check_times = {workstation_id: now for workstation_id in ssh_checks}
        self._last_worker_ssh_check_sources = {
            workstation_id: "system_check" for workstation_id in ssh_checks
        }

    @staticmethod
    def _refresh_check_summary_from_health(results: dict[str, Any]) -> None:
        health = results.get("health")
        if not isinstance(health, dict):
            return
        summary = dict(results.get("summary") or {})
        passed = int(summary.get("passed", 0) or 0)
        failed = int(summary.get("failed", 0) or 0)
        warnings = int(summary.get("warnings", 0) or 0)

        local_worker_online = health.get("local_worker_online")
        if isinstance(local_worker_online, bool):
            if local_worker_online:
                passed += 1
            elif health.get("local_worker_required") is not False:
                failed += 1
        status = health.get("server_to_local_ssh")
        if status == "ok":
            passed += 1
        elif status in {"disconnected", "error"}:
            failed += 1
        details = health.get("workstation_ssh_details")
        if isinstance(details, dict):
            for status in details.values():
                if status == "ok":
                    passed += 1
                elif status == "stale":
                    warnings += 1
                elif status == "disconnected" or str(status).startswith("error:"):
                    failed += 1
        config_warnings = health.get("config_warnings")
        if isinstance(config_warnings, list):
            warnings += len(config_warnings)

        results["summary"] = {"passed": passed, "failed": failed, "warnings": warnings}
        results["overall_ok"] = failed == 0
        results["status"] = "failed" if failed else ("warning" if warnings else "passed")

    def handle_worker_register(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """Handle LocalWorker registration."""
        params = params or {}
        worker_id = str(params.get("worker_id") or "")
        if not worker_id:
            return False, None, "缺少 worker_id"
        capabilities = params.get("capabilities")
        if not isinstance(capabilities, dict):
            capabilities = {}
        network = params.get("network")
        if not isinstance(network, dict):
            network = {}
        remote_addr = params.get("remote_addr")
        worker = self.local_worker_registry.register(
            worker_id,
            capabilities,
            network=network,
            remote_addr=str(remote_addr) if remote_addr else None,
        )
        worker_configs = params.get("configs")
        if isinstance(worker_configs, dict):
            try:
                self._load_configs_from_worker(worker_configs)
            except ValueError as e:
                return False, None, f"LocalWorker 构型数据无效: {e}"
            self._refresh_workstation_ssh_checks(connect=False)
        return True, worker, "LocalWorker 已注册"

    def _load_configs_from_worker(self, raw_configs: dict[Any, Any]) -> None:
        """Initialize server-mode state from LocalWorker-provided Excel configs."""
        if self._pipeline_ever_started:
            return
        configs = self._coerce_worker_configs(raw_configs)
        fingerprint = compute_config_fingerprint(configs)
        db_path = get_db_path_for_fingerprint(fingerprint)
        if (
            getattr(self, "_worker_config_fingerprint", None) == fingerprint
            and self.state is not None
            and getattr(self.state, "db_path", None) == db_path
        ):
            return
        if self.state is None or getattr(self.state, "db_path", None) != db_path:
            self.state = StateManager(db_path=db_path)
            self.runner = TaskRunner(
                self.state,
                local_worker_adapter=self.local_worker_adapter,
            )
            self.scheduler = PipelineScheduler(self.state, self.runner)
        self.state.load_configs(configs)
        self._assign_config_workstations()
        self._config_load_error = None
        self._worker_config_fingerprint = fingerprint
        logger.info(
            "[LocalWorker] 已从 worker 注册信息同步 %d 个构型，状态数据库: %s",
            len(configs),
            db_path,
        )

    @staticmethod
    def _coerce_worker_configs(raw_configs: dict[Any, Any]) -> dict[int, list[float]]:
        configs: dict[int, list[float]] = {}
        for raw_name, raw_values in raw_configs.items():
            try:
                config_name = int(raw_name)
            except (TypeError, ValueError) as e:
                raise ValueError(f"无效构型编号: {raw_name!r}") from e
            if not isinstance(raw_values, (list, tuple)):
                raise ValueError(f"构型 {config_name} 参数不是列表")
            if len(raw_values) != 4:
                raise ValueError(f"构型 {config_name} 参数数量必须为 4")
            try:
                configs[config_name] = [float(value) for value in raw_values]
            except (TypeError, ValueError) as e:
                raise ValueError(f"构型 {config_name} 参数无法转换为数字") from e
        if not configs:
            raise ValueError("构型数据为空")
        return configs

    def handle_worker_heartbeat(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """Handle LocalWorker heartbeat."""
        params = params or {}
        worker_id = str(params.get("worker_id") or "")
        if not worker_id:
            return False, None, "缺少 worker_id"
        capabilities = params.get("capabilities")
        if not isinstance(capabilities, dict):
            capabilities = None
        network = params.get("network")
        if not isinstance(network, dict):
            network = None
        remote_addr = params.get("remote_addr")
        worker = self.local_worker_registry.heartbeat(
            worker_id,
            capabilities=capabilities,
            network=network,
            remote_addr=str(remote_addr) if remote_addr else None,
        )
        return True, worker, "LocalWorker 心跳已更新"

    def handle_worker_poll(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """Handle LocalWorker task polling."""
        params = params or {}
        worker_id = str(params.get("worker_id") or "")
        if not worker_id:
            return False, None, "缺少 worker_id"
        task = self.local_worker_registry.poll_task(worker_id)
        if task is None:
            return True, None, "LocalWorker 暂无任务"
        return True, task, "LocalWorker 已领取任务"

    def _should_reject_worker_result_after_engine_stopped(self, task: dict[str, Any] | None) -> bool:
        """Return True for pipeline-mutating LocalWorker tasks that arrived after stop."""
        if self.state is None or self.state.get_engine_status() != "stopped":
            return False
        if not task:
            return False
        return str(task.get("step") or "") in {"sw", "sc"}

    def handle_worker_step_complete(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """Handle LocalWorker task completion."""
        params = params or {}
        worker_id = str(params.get("worker_id") or "")
        task_id = str(params.get("task_id") or "")
        if not worker_id or not task_id:
            return False, None, "缺少 worker_id 或 task_id"
        result = params.get("result")
        if not isinstance(result, dict):
            result = {}
        task = self.local_worker_registry.get_task(task_id)
        if self._should_reject_worker_result_after_engine_stopped(task):
            task = self.local_worker_registry.fail_task(
                task_id,
                worker_id,
                "engine stopped",
            )
            config_name = task.get("params", {}).get("config_name") if task else None
            step_name = task.get("step") if task else None
            if config_name is not None and step_name in {"sw", "sc"}:
                self.state.set_step_status(
                    int(config_name),
                    str(step_name),
                    STATUS_ERROR,
                    "engine stopped",
                )
            logger.warning(
                "[LocalWorker] 丢弃 stopped 后到达的任务结果: task_id=%s worker_id=%s",
                task_id,
                worker_id,
            )
            return True, task, "LocalWorker 任务已丢弃: engine stopped"
        try:
            scdoc_metadata = self._persist_worker_scdoc(result)
        except (OSError, TypeError, ValueError, binascii.Error) as exc:
            try:
                task = self.local_worker_registry.fail_task(task_id, worker_id, str(exc))
            except KeyError:
                logger.warning(
                    "[LocalWorker] 丢弃未知任务结果: task_id=%s worker_id=%s",
                    task_id,
                    worker_id,
                )
                return True, None, "LocalWorker 任务已丢弃: unknown task"
            return False, task, f"LocalWorker SCDOC 接收失败: {exc}"
        if scdoc_metadata is not None:
            result = dict(result)
            result["scdoc_file"] = scdoc_metadata
        try:
            task = self.local_worker_registry.complete_task(task_id, worker_id, result)
        except KeyError:
            logger.warning(
                "[LocalWorker] 丢弃未知任务结果: task_id=%s worker_id=%s",
                task_id,
                worker_id,
            )
            return True, None, "LocalWorker 任务已丢弃: unknown task"
        return True, task, "LocalWorker 任务完成"

    def _persist_worker_scdoc(self, result: dict[str, Any]) -> dict[str, Any] | None:
        """Persist a LocalWorker-produced SCDOC for server-side transfer."""
        payload = result.get("scdoc_file")
        if not isinstance(payload, dict):
            return None
        raw_config_name = payload.get("config_name")
        if raw_config_name is None:
            raise ValueError("SCDOC 载荷缺少 config_name")
        config_name = int(raw_config_name)
        expected_name = get_step_filename("sc", config_name)
        filename = str(payload.get("filename") or "")
        if not expected_name or filename != expected_name or os.path.basename(filename) != filename:
            raise ValueError(f"非法 SCDOC 文件名: {filename!r}")
        content_b64 = str(payload.get("content_b64") or "")
        content = base64.b64decode(content_b64.encode("ascii"), validate=True)
        expected_size = int(payload.get("size") or 0)
        if expected_size and len(content) != expected_size:
            raise ValueError(
                f"SCDOC 文件大小不匹配: expected={expected_size}, actual={len(content)}"
            )
        if not content:
            raise ValueError("SCDOC 文件为空")
        scdoc_dir = self._worker_scdoc_receive_dir()
        os.makedirs(scdoc_dir, exist_ok=True)
        target_path = os.path.join(scdoc_dir, filename)
        with open(target_path, "wb") as handle:
            handle.write(content)
        logger.info(
            "[LocalWorker] 已接收构型%s SCDOC: %s (%d bytes)",
            config_name,
            target_path,
            len(content),
        )
        return {
            "config_name": config_name,
            "filename": filename,
            "size": len(content),
            "server_path": target_path,
        }

    @staticmethod
    def _worker_scdoc_receive_dir() -> str:
        """Return the daemon-local directory for LocalWorker SCDOC payloads."""
        if is_server_mode():
            return os.path.join(str(LOCAL_PATHS["data_dir"]), "scdoc")
        return str(LOCAL_PATHS["scdoc_dir"])

    def handle_worker_step_error(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """Handle LocalWorker task failure."""
        params = params or {}
        worker_id = str(params.get("worker_id") or "")
        task_id = str(params.get("task_id") or "")
        if not worker_id or not task_id:
            return False, None, "缺少 worker_id 或 task_id"
        error = str(params.get("error") or "")
        task = self.local_worker_registry.get_task(task_id)
        if self._should_reject_worker_result_after_engine_stopped(task):
            task = self.local_worker_registry.fail_task(
                task_id,
                worker_id,
                "engine stopped",
            )
            config_name = task.get("params", {}).get("config_name") if task else None
            step_name = task.get("step") if task else None
            if config_name is not None and step_name in {"sw", "sc"}:
                self.state.set_step_status(
                    int(config_name),
                    str(step_name),
                    STATUS_ERROR,
                    "engine stopped",
                )
            logger.warning(
                "[LocalWorker] 丢弃 stopped 后到达的任务失败上报: task_id=%s worker_id=%s",
                task_id,
                worker_id,
            )
            return True, task, "LocalWorker 任务已丢弃: engine stopped"
        try:
            task = self.local_worker_registry.fail_task(task_id, worker_id, error)
        except KeyError:
            logger.warning(
                "[LocalWorker] 丢弃未知任务失败回报: task_id=%s worker_id=%s error=%s",
                task_id,
                worker_id,
                error,
            )
            return True, None, "LocalWorker 任务已丢弃: unknown task"
        return True, task, "LocalWorker 任务失败"

    # ------------------------------------------------------------------
    # Worker 生命周期管理命令
    # ------------------------------------------------------------------

    def handle_worker_start(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """启动所有 worker：验证工作站 SSH 连通性，准备 Registry 接受注册。"""
        params = params or {}
        results: dict[str, Any] = {}
        from engine.config import reload_config_from_toml

        if reload_config_from_toml() and self.runner is not None:
            try:
                self.runner.disconnect_ssh(lock_timeout=1.0)
            except TypeError:
                self.runner.disconnect_ssh()
            except Exception as e:
                logger.debug("[Worker] 刷新配置后断开旧 SSH 连接异常: %s", e)
            scheduler = getattr(self, "scheduler", None)
            if scheduler is not None and hasattr(scheduler, "refresh_workstation_slots"):
                try:
                    scheduler.refresh_workstation_slots()
                except Exception as e:
                    logger.warning("[Worker] 刷新工作站槽位异常: %s", e)
                    return False, None, f"刷新工作站槽位失败: {e}"

        self._reset_workstation_ssh_recovery_history()
        results.update(self._refresh_workstation_ssh_checks(source="worker_start"))
        results["registry_ready"] = False

        ssh_checks = results["ssh_checks"]
        failed_ssh_checks = {
            ws_id: status
            for ws_id, status in ssh_checks.items()
            if status != "ok"
        }
        if ssh_checks and len(failed_ssh_checks) == len(ssh_checks):
            self.local_worker_registry.clear_online_workers()
            logger.warning("[Worker] worker_start 失败，所有工作站 SSH 连通检查失败: %s", ssh_checks)
            self._request_workstation_ssh_active_probe()
            self._start_workstation_ssh_health_monitor()
            target_summary = ", ".join(
                f"{ws_id}={target['host']}:{target['port']}({target['connectivity_mode']})"
                for ws_id, target in results["ssh_targets"].items()
            )
            return False, results, (
                f"SSH 连通检查全部失败: {ssh_checks}; "
                f"实际检查目标: {target_summary}"
            )

        # 清除旧的在线 worker 标记（允许重新注册）
        self.local_worker_registry.clear_online_workers()
        results["registry_ready"] = True
        self._start_workstation_ssh_health_monitor()

        logger.info("[Worker] worker_start 完成: %s", results)
        if failed_ssh_checks:
            return True, results, f"部分工作站 SSH 连通检查失败: {failed_ssh_checks}"
        return True, results, "Worker 启动准备就绪，等待本地 Worker 和工作站 Worker 连接"

    def _get_runner_ssh_for_check(self, ws_id: str):
        if self.runner is None:
            raise RuntimeError("TaskRunner 未初始化")
        try:
            return self.runner.get_ssh(ws_id, log_failure=False)
        except TypeError as exc:
            if "log_failure" not in str(exc):
                raise
            return self.runner.get_ssh(ws_id)

    def _reset_workstation_ssh_recovery_history(self) -> None:
        recovery_manager = getattr(self, "_workstation_ssh_recovery", None)
        reset_history = getattr(recovery_manager, "reset_history", None)
        if not callable(reset_history):
            return
        try:
            if reset_history() is False:
                logger.warning("[Worker] SSH 恢复历史仍有修复进行中，跳过重置")
        except Exception as exc:
            logger.warning("[Worker] SSH 恢复历史重置异常: %s", exc)

    def _ensure_ssh_health_probe_controls(self) -> tuple[threading.Lock, threading.Event]:
        lock = getattr(self, "_ssh_health_probe_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._ssh_health_probe_lock = lock
        force_event = getattr(self, "_ssh_health_force_active_probe", None)
        if force_event is None:
            force_event = threading.Event()
            self._ssh_health_force_active_probe = force_event
        return lock, force_event

    def _ensure_ssh_health_wake_event(self) -> threading.Event:
        wake_event = getattr(self, "_ssh_health_wake_event", None)
        if wake_event is None:
            wake_event = threading.Event()
            self._ssh_health_wake_event = wake_event
        return wake_event

    def _request_workstation_ssh_active_probe(self) -> None:
        lock, force_event = self._ensure_ssh_health_probe_controls()
        with lock:
            self._last_worker_ssh_active_probe_time = 0.0
            force_event.set()
        self._ensure_ssh_health_wake_event().set()

    def _should_run_workstation_ssh_active_probe(self, now: float, interval: float) -> bool:
        lock, force_event = self._ensure_ssh_health_probe_controls()
        with lock:
            force_active = force_event.is_set()
            last_active = float(getattr(self, "_last_worker_ssh_active_probe_time", now))
            if force_active or (interval > 0 and now - last_active >= interval):
                force_event.clear()
                self._last_worker_ssh_active_probe_time = now
                return True
            return False

    def _refresh_workstation_ssh_checks(
        self,
        *,
        connect: bool = True,
        source: str = "active_probe",
    ) -> dict[str, Any]:
        """Refresh workstation SSH readiness using the current runner.

        Args:
            connect: True for explicit readiness checks; False for passive background health.
        """
        results: dict[str, Any] = {
            "ssh_checks": {},
            "ssh_targets": {},
        }
        if self.runner is None:
            return results

        previous_checks = dict(getattr(self, "_last_worker_ssh_checks", {}))
        previous_sources = getattr(self, "_last_worker_ssh_check_sources", {})
        if not isinstance(previous_sources, dict):
            previous_sources = {}
        ssh_pool = getattr(self.runner, "_ssh_pool", {})
        if not isinstance(ssh_pool, dict):
            ssh_pool = {}
        for ws in WORKSTATIONS:
            ws_id = str(ws.get("id", "default"))
            target = self._workstation_ssh_target(ws)
            results["ssh_targets"][ws_id] = target
            try:
                if connect:
                    try:
                        ssh = self.runner.get_ssh(ws_id, log_failure=False)
                    except TypeError as e:
                        if "log_failure" not in str(e):
                            raise
                        ssh = self.runner.get_ssh(ws_id)
                    connected = bool(ssh.is_connected())
                else:
                    # Passive health must not promote cached Paramiko transport state
                    # to "ok"; stale transports can outlive reverse tunnels.
                    # Active paths such as worker_start and task execution remain
                    # responsible for real heartbeats and reconnects.
                    results["ssh_checks"][ws_id] = str(
                        previous_checks.get(ws_id, "unknown")
                    )
                    continue
                results["ssh_checks"][ws_id] = "ok" if connected else "disconnected"
                if connected and previous_checks.get(ws_id) not in (None, "ok"):
                    logger.warning(
                        "[Worker] 工作站 %s SSH 连接已恢复 "
                        "(host=%s, port=%s, connectivity_mode=%s)",
                        ws_id,
                        target["host"],
                        target["port"],
                        target["connectivity_mode"],
                    )
            except Exception as e:
                if isinstance(e, TypeError):
                    raise
                results["ssh_checks"][ws_id] = f"error: {e}"
                logger.warning(
                    "[Worker] 工作站 %s SSH 连通检查失败 "
                    "(host=%s, port=%s, connectivity_mode=%s): %s",
                    ws_id,
                    target["host"],
                    target["port"],
                    target["connectivity_mode"],
                    e,
                )

        self._last_worker_ssh_checks = dict(results["ssh_checks"])
        recovery_manager = getattr(self, "_workstation_ssh_recovery", None)
        if connect and recovery_manager is not None:
            recovery_updates: dict[str, Any] = {}
            for ws_id, status in results["ssh_checks"].items():
                target = results["ssh_targets"].get(ws_id, {})
                record_check = getattr(recovery_manager, "record_check", None)
                if not callable(record_check):
                    continue
                try:
                    update = record_check(ws_id, status, target, source=source)
                except Exception as exc:
                    logger.warning("[Worker] 工作站 %s SSH 恢复协调异常: %s", ws_id, exc)
                    update = {"status": status, "repair": {"status": "error", "detail": str(exc)}}
                recovery_updates[ws_id] = update
                repair_status = str(update.get("repair", {}).get("status", ""))
                if repair_status == "repair_succeeded" and self.runner is not None:
                    try:
                        self.runner.disconnect_ssh(ws_id, lock_timeout=1.0)
                    except TypeError:
                        self.runner.disconnect_ssh(ws_id)
                    except Exception as exc:
                        logger.debug("[Worker] 工作站 %s SSH 修复后断开旧连接异常: %s", ws_id, exc)
                    post_repair_status = "disconnected"
                    try:
                        ssh = self._get_runner_ssh_for_check(ws_id)
                        if bool(ssh.is_connected()):
                            results["ssh_checks"][ws_id] = "ok"
                            self._last_worker_ssh_checks[ws_id] = "ok"
                            record_check(ws_id, "ok", target, source="post_repair_probe")
                            post_repair_status = "ok"
                    except Exception as exc:
                        post_repair_status = f"error: {exc}"
                        logger.debug("[Worker] 工作站 %s SSH 修复后复查仍失败: %s", ws_id, exc)
                    if post_repair_status != "ok":
                        record_check(ws_id, post_repair_status, target, source="post_repair_probe")
            if recovery_updates:
                results["ssh_recovery"] = recovery_updates
        if connect:
            now = time.time()
            self._last_worker_ssh_check_times = {
                ws_id: now for ws_id in results["ssh_checks"]
            }
            self._last_worker_ssh_check_sources = {
                ws_id: source for ws_id in results["ssh_checks"]
            }
        else:
            # Passive background checks intentionally do not prove fresh
            # connectivity. Preserve the last active-check timestamp so a
            # cached "ok" naturally ages into "stale" on the dashboard.
            previous_times = getattr(self, "_last_worker_ssh_check_times", {})
            self._last_worker_ssh_check_times = (
                dict(previous_times) if isinstance(previous_times, dict) else {}
            )
            self._last_worker_ssh_check_sources = dict(previous_sources)
        return results

    def _start_workstation_ssh_health_monitor(self) -> None:
        """Start the server-mode background workstation SSH health monitor."""
        if not is_server_mode():
            return
        if not hasattr(self, "_ssh_health_stop_event"):
            self._ssh_health_stop_event = threading.Event()
        if not hasattr(self, "_ssh_health_interval_seconds"):
            self._ssh_health_interval_seconds = get_workstation_ssh_health_interval()
        if not hasattr(self, "_ssh_health_thread"):
            self._ssh_health_thread = None
        if self._ssh_health_interval_seconds <= 0:
            return
        thread = self._ssh_health_thread
        if thread is not None and thread.is_alive():
            return
        self._ssh_health_stop_event.clear()
        self._ssh_health_thread = threading.Thread(
            target=self._workstation_ssh_health_loop,
            name="AutoFluidWorkstationSshHealth",
            daemon=True,
        )
        self._ssh_health_thread.start()

    def _workstation_ssh_health_loop(self) -> None:
        self._run_workstation_ssh_health_check_once()
        if self._ssh_health_stop_event.is_set():
            return
        wake_event = self._ensure_ssh_health_wake_event()
        while True:
            wake_event.wait(self._ssh_health_interval_seconds)
            wake_event.clear()
            if self._ssh_health_stop_event.is_set():
                break
            self._run_workstation_ssh_health_check_once()

    def _run_workstation_ssh_health_check_once(self) -> dict[str, Any]:
        if getattr(self, "runner", None) is None:
            return {"ssh_checks": {}, "ssh_targets": {}}
        try:
            now = time.time()
            interval = float(
                getattr(self, "_ssh_health_active_probe_interval_seconds", 300.0)
            )
            if self._should_run_workstation_ssh_active_probe(now, interval):
                return self._refresh_workstation_ssh_checks(
                    connect=True,
                    source="active_probe",
                )
            return self._refresh_workstation_ssh_checks(connect=False)
        except Exception as exc:
            logger.warning("[ServerMode] 后台工作站 SSH 健康检查失败: %s", exc)
            return {"ssh_checks": {}, "ssh_targets": {}}

    def _stop_workstation_ssh_health_monitor(self) -> None:
        stop_event = getattr(self, "_ssh_health_stop_event", None)
        if stop_event is not None:
            stop_event.set()
        wake_event = getattr(self, "_ssh_health_wake_event", None)
        if wake_event is not None:
            wake_event.set()
        thread = getattr(self, "_ssh_health_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._ssh_health_thread = None
        self._shutdown_workstation_ssh_recovery()

    def _shutdown_workstation_ssh_recovery(self) -> None:
        recovery_manager = getattr(self, "_workstation_ssh_recovery", None)
        shutdown_recovery = getattr(recovery_manager, "shutdown", None)
        if not callable(shutdown_recovery):
            return
        try:
            if shutdown_recovery() is False:
                logger.warning("[Worker] SSH 恢复协调器关闭超时")
        except Exception as e:
            logger.warning("[Worker] SSH 恢复协调器关闭异常: %s", e)

    def handle_worker_stop(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """停止所有 worker：断开工作站 SSH 连接，清理 Registry 任务队列。"""
        params = params or {}
        preserve_remote_tasks = bool(params.get("preserve_remote_tasks", False))
        results: dict[str, Any] = {
            "ssh_disconnected": [],
            "registry_cleared": False,
            "remote_tasks_cleared": False,
        }
        self._stop_workstation_ssh_health_monitor()

        # 断开 TaskRunner 的所有 SSH 连接
        if self.runner is not None:
            try:
                self.runner.disconnect_ssh()
                results["ssh_disconnected"] = [str(ws.get("id", "default")) for ws in WORKSTATIONS]
            except Exception as e:
                logger.warning("[Worker] SSH 断开异常: %s", e)

        # 清理 Registry 中的在线 worker 标记和待处理任务
        self.local_worker_registry.clear_online_workers()
        self.local_worker_registry.clear_pending_tasks()
        results["registry_cleared"] = True
        state = getattr(self, "state", None)
        delete_all_remote_tasks = getattr(state, "delete_all_remote_tasks", None)
        if not preserve_remote_tasks and callable(delete_all_remote_tasks):
            delete_all_remote_tasks()
            results["remote_tasks_cleared"] = True
        self._last_worker_ssh_checks = {}
        try:
            results["watchdog_cleanup"] = cleanup_tunnel_watchdog_tasks(_PROJECT_ROOT)
        except Exception as e:
            logger.warning("[Worker] watchdog 清理异常: %s", e)
            results["watchdog_cleanup"] = {"status": "failed", "error": str(e)}
        results["process_cleanup"] = cleanup_worker_processes_from_pid_files()

        logger.info("[Worker] worker_stop 完成: %s", results)
        return True, results, "所有 Worker 已停止"

    def handle_worker_restart(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """重启所有 worker：先停止再启动。"""
        stop_params = dict(params or {})
        stop_params["preserve_remote_tasks"] = True
        ok_stop, data_stop, msg_stop = self.handle_worker_stop(stop_params)
        if not ok_stop:
            return False, data_stop, f"Worker 重启失败（停止阶段）: {msg_stop}"
        ok_start, data_start, msg_start = self.handle_worker_start(params)
        if not ok_start:
            return False, data_start, f"Worker 重启失败（启动阶段）: {msg_start}"
        return True, {"stop": data_stop, "start": data_start}, "所有 Worker 已重启"

    def handle_get_all_status(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """获取所有构型的状态。"""
        if self.state is None:
            raise RuntimeError("StateManager 未初始化，请先调用 start()")
        statuses = self.state.get_all_statuses()
        return True, statuses, ""

    def handle_get_statistics(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """获取统计信息。"""
        if self.state is None:
            raise RuntimeError("StateManager 未初始化，请先调用 start()")
        stats = self.state.get_statistics()
        engine_status = self.state.get_engine_status()
        stats["engine_status"] = engine_status
        stats["sw_macro_started"] = self.state.is_sw_macro_started()
        stats["barrier_passed"] = self.state.is_global_barrier_met()
        return True, stats, ""

    def _workstation_barrier_snapshot(self) -> dict[str, bool]:
        """Return current workstation barrier states for dashboard consumers."""
        scheduler = getattr(self, "scheduler", None)
        barrier_coordinator = getattr(scheduler, "barrier_coordinator", None)
        snapshot = getattr(barrier_coordinator, "workstation_barrier_snapshot", None)
        if not callable(snapshot):
            return {}
        snapshot_data = snapshot()
        if not isinstance(snapshot_data, Mapping):
            return {}
        return {str(key): bool(value) for key, value in snapshot_data.items()}

    def _solver_quarantine_snapshot(self) -> dict[str, Any]:
        """Return current solver workstation quarantine data for dashboard consumers."""
        scheduler = getattr(self, "scheduler", None)
        barrier_coordinator = getattr(scheduler, "barrier_coordinator", None)
        snapshot = getattr(barrier_coordinator, "solver_quarantine_snapshot", None)
        if not callable(snapshot):
            return {}
        snapshot_data = snapshot()
        return dict(snapshot_data) if isinstance(snapshot_data, Mapping) else {}

    def handle_get_engine_status(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """获取引擎状态。"""
        started_at = getattr(self, "_started_at_epoch", None)
        uptime_seconds = None
        started_at_display = None
        if started_at is not None:
            uptime_seconds = max(0, int(time.time() - started_at))
            started_at_display = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(started_at),
            )
        if self.state is None:
            status = {
                "engine_status": "stopped",
                "sw_macro_started": False,
                "barrier_passed": False,
                "workstation_barriers": {},
                "solver_quarantine": {},
                "pipeline_started": self._pipeline_ever_started,
                "daemon_started_at": started_at,
                "daemon_started_at_display": started_at_display,
                "daemon_uptime_seconds": uptime_seconds,
                "config_load_error": getattr(self, "_config_load_error", None),
                "solver_progress": None,
                "solver_progress_by_config": {},
            }
            return True, status, ""
        get_solver_progress_by_config = getattr(self.state, "get_solver_progress_by_config", None)
        solver_progress_by_config = (
            get_solver_progress_by_config()
            if callable(get_solver_progress_by_config)
            else {}
        )
        status = {
            "engine_status": self.state.get_engine_status(),
            "sw_macro_started": self.state.is_sw_macro_started(),
            "barrier_passed": self.state.is_global_barrier_met(),
            "workstation_barriers": self._workstation_barrier_snapshot(),
            "solver_quarantine": self._solver_quarantine_snapshot(),
            "pipeline_started": self._pipeline_ever_started,
            "daemon_started_at": started_at,
            "daemon_started_at_display": started_at_display,
            "daemon_uptime_seconds": uptime_seconds,
            "config_load_error": getattr(self, "_config_load_error", None),
            "solver_progress": self.state.get_solver_progress(),
            "solver_progress_by_config": solver_progress_by_config,
        }
        return True, status, ""

    def _build_health_snapshot(self) -> dict[str, Any]:
        """Return low-cost dashboard health data without creating new connections."""
        registry = getattr(self, "local_worker_registry", None)
        online_workers: list[dict[str, Any]] = []
        if registry is not None:
            online_workers_fn = getattr(registry, "online_workers", None)
            if callable(online_workers_fn):
                online_workers = list(online_workers_fn())

        server_to_local_ssh = "disconnected" if online_workers else "unknown"
        for worker in online_workers:
            network = worker.get("network", {})
            if not isinstance(network, dict):
                continue
            reachable_host = str(network.get("reachable_host") or "").strip()
            ssh_port = network.get("ssh_port")
            if reachable_host and ssh_port:
                server_to_local_ssh = "ok"
                break

        workstation_details: dict[str, str] = {}
        workstation_targets: dict[str, dict[str, Any]] = {}
        runner = getattr(self, "runner", None)
        ssh_pool = getattr(runner, "_ssh_pool", {}) if runner is not None else {}
        if not isinstance(ssh_pool, dict):
            ssh_pool = {}
        last_worker_checks = getattr(self, "_last_worker_ssh_checks", {})
        if not isinstance(last_worker_checks, dict):
            last_worker_checks = {}
        last_check_times = getattr(self, "_last_worker_ssh_check_times", {})
        if not isinstance(last_check_times, dict):
            last_check_times = {}
        last_check_sources = getattr(self, "_last_worker_ssh_check_sources", {})
        if not isinstance(last_check_sources, dict):
            last_check_sources = {}
        health_interval = float(getattr(self, "_ssh_health_interval_seconds", 30.0))
        active_probe_interval = float(
            getattr(
                self,
                "_ssh_health_active_probe_interval_seconds",
                _DEFAULT_WORKSTATION_SSH_ACTIVE_PROBE_INTERVAL_SECONDS,
            )
        )
        stale_after = max(120.0, health_interval * 3, active_probe_interval + health_interval)
        now = time.time()
        workstation_checked_at: dict[str, float] = {}
        workstation_sources: dict[str, str] = {}
        workstation_configs: list[Mapping[str, Any]] = list(WORKSTATIONS) or [{"id": "default"}]
        for workstation in workstation_configs:
            workstation_id = str(workstation.get("id", "default"))
            workstation_targets[workstation_id] = self._workstation_ssh_target(workstation)
            # Dashboard health is passive: report the latest active health check
            # instead of trusting cached Paramiko transport state.
            status = str(last_worker_checks.get(workstation_id, "unknown"))
            checked_at = last_check_times.get(workstation_id)
            if isinstance(checked_at, int | float):
                workstation_checked_at[workstation_id] = float(checked_at)
            source = last_check_sources.get(workstation_id)
            if isinstance(source, str) and source:
                workstation_sources[workstation_id] = source
            if status == "ok" and isinstance(checked_at, int | float):
                if now - float(checked_at) > stale_after:
                    status = "stale"
            workstation_details[workstation_id] = status
        detail_values = set(workstation_details.values())
        if any(value == "ok" for value in detail_values):
            server_to_workstation_ssh = "ok"
        elif any(value == "stale" for value in detail_values):
            server_to_workstation_ssh = "stale"
        elif any(value == "unknown" for value in detail_values):
            server_to_workstation_ssh = "unknown"
        else:
            server_to_workstation_ssh = "disconnected"

        health = {
            "local_worker_online": bool(online_workers),
            "local_worker_required": self._server_mode_requires_worker(),
            "server_to_local_ssh": server_to_local_ssh,
            "server_to_workstation_ssh": server_to_workstation_ssh,
            "workstation_ssh_details": workstation_details,
            "workstation_ssh_targets": workstation_targets,
            "config_warnings": list(getattr(self, "_config_warnings", [])),
        }
        if workstation_checked_at:
            health["workstation_ssh_checked_at"] = workstation_checked_at
        if workstation_sources:
            health["workstation_ssh_source"] = workstation_sources
        recovery_manager = getattr(self, "_workstation_ssh_recovery", None)
        snapshot = getattr(recovery_manager, "snapshot", None)
        if callable(snapshot):
            recovery_snapshot = snapshot()
            if recovery_snapshot:
                health["workstation_ssh_recovery"] = recovery_snapshot
        return health

    @staticmethod
    def _workstation_ssh_target(workstation: Mapping[str, Any]) -> dict[str, Any]:
        """Return the effective workstation SSH target without opening connections."""
        reachable_host = str(workstation.get("reachable_host", "")).strip()
        if reachable_host:
            host = reachable_host
            port = int(workstation.get("reachable_port", workstation.get("port", 22)) or 22)
            mode = str(workstation.get("connectivity_mode", "") or "reachable")
        else:
            host = str(workstation.get("host", "")).strip()
            port = int(workstation.get("port", 22) or 22)
            mode = str(workstation.get("connectivity_mode", "") or "direct")
        return {
            "host": host,
            "port": port,
            "connectivity_mode": mode,
        }

    def handle_get_dashboard(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """批量获取 TUI 仪表盘所需的状态、引擎信息和日志增量。"""
        params = params or {}
        log_params = {
            "since_id": params.get("since_log_id", params.get("since_id", 0)),
            "limit": params.get("log_limit", params.get("limit", 50)),
            "level_filter": params.get("level_filter"),
            "source_filter": params.get("source_filter"),
            "include_polling": bool(params.get("include_polling", False)),
            "include_lifecycle": bool(params.get("include_lifecycle", False)),
            "include_config_scoped": bool(params.get("include_config_scoped", False)),
        }
        statuses: dict[int, dict[str, str]]
        if self.state is None:
            statuses = {}
        else:
            _, statuses, _ = self.handle_get_all_status(None)
        _, engine, _ = self.handle_get_engine_status(None)
        _, logs, _ = self.handle_get_log_entries(log_params)
        data = {
            "statuses": statuses,
            "config_workstations": self._build_config_workstation_snapshot(statuses),
            "engine": engine,
            "health": self._build_health_snapshot(),
            "logs": logs,
        }
        self._trim_dashboard_logs_to_budget(data)
        return True, data, ""

    def _build_config_workstation_snapshot(
        self,
        statuses: Mapping[Any, Any],
    ) -> dict[str, str]:
        """Return config-to-workstation assignments for dashboard consumers."""
        state = getattr(self, "state", None)
        get_config_workstation = getattr(state, "get_config_workstation", None)
        if not callable(get_config_workstation):
            return {}

        assignments: dict[str, str] = {}
        for config_name in statuses:
            try:
                workstation_id = get_config_workstation(int(config_name))
            except (TypeError, ValueError):
                workstation_id = None
            if workstation_id and str(workstation_id) != "default":
                assignments[str(config_name)] = str(workstation_id)
        return assignments

    @staticmethod
    def _trim_dashboard_logs_to_budget(data: dict[str, Any]) -> None:
        """Keep dashboard IPC responses below the Rust client line-size cap."""
        logs = data.get("logs")
        if not isinstance(logs, dict):
            return
        entries = logs.get("entries")
        if not isinstance(entries, list) or not entries:
            return
        if _json_size_bytes(data) <= _MAX_DASHBOARD_LOG_BYTES:
            return

        original_entries = entries
        logs["entries"] = []
        base_size = _json_size_bytes(data)
        entry_budget = max(0, _MAX_DASHBOARD_LOG_BYTES - base_size)
        kept_reversed: list[Any] = []
        used = 0
        for entry in reversed(original_entries):
            entry_size = _json_size_bytes(entry) + 1
            if kept_reversed and used + entry_size > entry_budget:
                break
            if not kept_reversed and entry_size > entry_budget:
                break
            kept_reversed.append(entry)
            used += entry_size
        logs["entries"] = list(reversed(kept_reversed))

        if _json_size_bytes(data) > _MAX_DASHBOARD_LOG_BYTES:
            logs["entries"] = []
        logs["truncated"] = True

    def handle_reset_step(self, params: dict) -> tuple[bool, Any, str]:
        """
        处理 reset 命令，config_name 和 step_name 均支持 "all"。

        params: {"config_name": int|str, "step_name": str|None}
        """
        config_name = params.get("config_name")
        step_name = params.get("step_name")

        if config_name is None:
            return False, None, "请指定构型名称 (config_name)"
        valid_config_name, config_name, config_error = self._normalize_config_name_for_mutation(
            config_name,
            allow_none=False,
        )
        if not valid_config_name:
            return False, None, config_error

        # 校验 step_name（空字符串视为无效）
        if step_name is not None and step_name != "all" and step_name not in STEP_NAMES:
            return False, None, f"无效步骤名: {step_name}，有效值: {STEP_NAMES} 或 all"

        guard_ok, guard_msg = self._validate_mutation_safe(
            action_name="重置",
            config_name=config_name,
            step_name=step_name,
            include_downstream=True,
        )
        if not guard_ok:
            return False, None, guard_msg

        if self.scheduler is None:
            raise RuntimeError("PipelineScheduler 未初始化，请先调用 start()")
        self.scheduler.reset_config(config_name, step_name)

        # 构建可读的消息
        if config_name == "all":
            cfg_desc = "所有构型"
        else:
            cfg_desc = f"构型{config_name}"

        if step_name in (None, "all"):
            step_desc = "所有步骤"
        else:
            step_desc = f"{step_name} 及后续步骤"

        msg = f"已重置{cfg_desc}的{step_desc}"
        return True, None, msg

    def handle_clean_step(self, params: dict) -> tuple[bool, Any, str]:
        """
        处理 clean 命令，config_name 和 step_name 均支持 "all"。

        params: {"step_name": str, "config_name": int|str|None}
        """
        step_name = params.get("step_name")
        config_name = params.get("config_name")
        valid_config_name, config_name, config_error = self._normalize_config_name_for_mutation(
            config_name,
            allow_none=True,
        )
        if not valid_config_name:
            return False, None, config_error

        guard_ok, guard_msg = self._validate_mutation_safe(
            action_name="清理",
            config_name=config_name,
            step_name=step_name,
            include_downstream=False,
        )
        if not guard_ok:
            return False, None, guard_msg

        if step_name == "cache":
            if config_name not in (None, "all"):
                return False, None, 'clean cache 仅支持用法: clean all cache'
            if self.runner is None:
                raise RuntimeError("TaskRunner 未初始化，请先调用 start()")

            def _do_clean_cache():
                try:
                    self.runner.clean_all_cache()
                    logger.info("[Cleaner] 后台清理完成: step=cache, config=all")
                except Exception as e:
                    logger.error(
                        f"[Cleaner] 后台清理失败: step=cache, config=all, error={e}",
                        exc_info=True,
                    )

            threading.Thread(
                target=_do_clean_cache,
                daemon=True,
                name="Clean-Cache-Bg",
            ).start()
            return True, None, "已启动后台清理远程缓存文件"

        # 验证 step_name（"all" 是合法值，无需校验）
        if step_name != "all" and step_name not in STEP_NAMES:
            return False, None, f"无效步骤名: {step_name}，有效值: {STEP_NAMES} 或 all"

        # 判断是否需要后台线程（远程步骤 + 清理范围大）
        needs_background = (
            config_name in (None, "all")
            and (step_name == "all" or step_name in {"meshing", "solver", "postprocess"})
        )

        if self.runner is None:
            raise RuntimeError("TaskRunner 未初始化，请先调用 start()")
        if needs_background:
            def _do_clean_step():
                try:
                    self.runner.clean_step_files(step_name, config_name)
                    self._release_cleaned_workstation_slots(step_name, config_name)
                    logger.info(
                        f"[Cleaner] 后台清理完成: step={step_name}, "
                        f"config={config_name or 'all'}"
                    )
                except Exception as e:
                    logger.error(
                        f"[Cleaner] 后台清理失败: step={step_name}, "
                        f"config={config_name or 'all'}, error={e}",
                        exc_info=True,
                    )

            threading.Thread(target=_do_clean_step, daemon=True,
                name=f"Clean-{step_name}-Bg").start()
            msg = f"已启动后台清理 {step_name} 步骤的文件"
            if config_name is not None and config_name != "all":
                msg += f" (构型{config_name})"
        else:
            self.runner.clean_step_files(step_name, config_name)
            self._release_cleaned_workstation_slots(step_name, config_name)
            msg = f"已清理 {step_name} 步骤的文件"
            if config_name is not None and config_name != "all":
                msg += f" (构型{config_name})"

        # 如果清理范围涉及 SW 步骤，请求文件监控器重置追踪状态。
        # 监控器不会修改调度器拥有的共享 pause。
        if step_name == "all" or step_name == "sw":
            if self.scheduler:
                self.scheduler.request_file_monitor_reset()

        return True, None, msg

    def handle_stop_step(self, params: dict) -> tuple[bool, Any, str]:
        """停止指定构型的一个远程步骤，不影响其他正在运行的任务。"""
        config_name = params.get("config_name")
        step_name = str(params.get("step_name") or "").lower()
        reason = str(params.get("reason") or "用户请求停止远程任务").strip()
        valid_config_name, config_name, config_error = self._normalize_config_name_for_mutation(
            config_name,
            allow_none=False,
        )
        if not valid_config_name:
            return False, None, config_error
        if config_name == "all":
            return False, None, "stop_step 必须指定单个构型"
        if step_name not in {"meshing", "solver", "postprocess"}:
            return False, None, "stop_step 仅支持 meshing、solver、postprocess"
        state = getattr(self, "state", None)
        runner = getattr(self, "runner", None)
        if state is None or runner is None:
            return False, None, "Daemon 尚未初始化流水线组件"
        get_all_remote_tasks = getattr(state, "get_all_remote_tasks", None)
        get_remote_executor = getattr(runner, "get_remote_executor", None)
        if not callable(get_all_remote_tasks) or not callable(get_remote_executor):
            return False, None, "远程任务管理接口不可用"

        target_task: dict[str, Any] | None = None
        for task in get_all_remote_tasks():
            try:
                task_config = int(task.get("config_name"))
            except (TypeError, ValueError):
                continue
            if task_config == config_name and str(task.get("step_name")) == step_name:
                target_task = dict(task)
                break
        if target_task is None:
            return False, None, f"未找到构型{config_name}的 {step_name} 远程任务"

        workstation_id = str(target_task.get("workstation_id", "default"))
        remote_executor = get_remote_executor()
        stop_remote_step = getattr(remote_executor, "stop_remote_step", None)
        if not callable(stop_remote_step):
            return False, None, "远程任务停止接口不可用"
        try:
            stopped = bool(stop_remote_step(config_name, step_name, workstation_id, reason))
        except Exception as exc:
            logger.warning(
                "[IPC] stop_step 失败: config=%s step=%s workstation=%s error=%s",
                config_name,
                step_name,
                workstation_id,
                exc,
            )
            return False, None, f"停止构型{config_name}的 {step_name} 远程任务异常: {exc}"
        if not stopped:
            return False, None, f"停止构型{config_name}的 {step_name} 远程任务失败"
        state.set_step_status(config_name, step_name, STATUS_ERROR, reason)
        data = {
            "config_name": config_name,
            "step_name": step_name,
            "workstation_id": workstation_id,
            "stopped": True,
        }
        return True, data, f"已停止构型{config_name}的 {step_name} 远程任务"

    def handle_migrate_config_workstation(self, params: dict) -> tuple[bool, Any, str]:
        """Safely migrate a config's workstation assignment and required artifact."""
        params = params or {}
        valid_config_name, config_name, config_error = self._normalize_config_name_for_mutation(
            params.get("config_name"),
            allow_none=False,
        )
        if not valid_config_name:
            return False, None, config_error
        if config_name == "all":
            return False, None, "migrate_config_workstation 必须指定单个构型"

        target_ok, target_workstation_id, target_error = self._normalize_target_workstation(
            params.get("target_workstation_id"),
        )
        if not target_ok:
            return False, None, target_error
        delete_source = bool(params.get("delete_source", True))

        state = getattr(self, "state", None)
        runner = getattr(self, "runner", None)
        scheduler = getattr(self, "scheduler", None)
        if state is None or runner is None or scheduler is None:
            return False, None, "Daemon 尚未初始化流水线组件"
        if state.get_engine_status() == "running":
            return False, None, "流水线运行中，迁移前请先 pause"

        source_workstation_id = self._config_workstation_for_migration(config_name)
        if not source_workstation_id or source_workstation_id == DEFAULT_WORKSTATION_ID:
            return False, None, f"构型{config_name}尚未绑定到具体工作站"
        if source_workstation_id == target_workstation_id:
            return False, None, f"构型{config_name}已位于 {target_workstation_id}"

        statuses = self._config_statuses_for_migration(config_name)
        all_completed = all(
            statuses.get(step_name) == STATUS_COMPLETED
            for step_name in STEP_NAMES
        )
        if all_completed:
            return False, None, f"构型{config_name}所有步骤已完成，不迁移"

        unsafe_statuses = {
            STATUS_RUNNING,
            STATUS_PAUSED,
            STATUS_RETRYING,
            STATUS_UNKNOWN_REMOTE,
        }
        for step_name in ("transfer", "meshing", "solver", "postprocess"):
            if statuses.get(step_name) in unsafe_statuses:
                return (
                    False,
                    None,
                    f"构型{config_name}的 {step_name} 状态为 {statuses.get(step_name)}，不允许迁移",
                )

        mode: str
        artifact: str
        if (
            statuses.get("transfer") == STATUS_COMPLETED
            and statuses.get("meshing") == STATUS_WAITING
            and statuses.get("solver") == STATUS_WAITING
            and statuses.get("postprocess") == STATUS_WAITING
        ):
            mode = "pre_meshing_scdoc"
            artifact = "scdoc"
        elif (
            statuses.get("meshing") == STATUS_COMPLETED
            and statuses.get("solver") == STATUS_WAITING
            and statuses.get("postprocess") == STATUS_WAITING
        ):
            mode = "post_meshing_msh"
            artifact = "msh"
        else:
            return (
                False,
                None,
                "当前步骤窗口不支持迁移：仅支持 transfer Completed + meshing Waiting，"
                "或 meshing Completed + solver Waiting",
            )

        health_ok, health_message = self._validate_migration_workstation_health(
            source_workstation_id,
            target_workstation_id,
        )
        if not health_ok:
            return False, None, health_message

        remote_ok, remote_message = self._validate_no_active_remote_task_for_migration(
            config_name,
        )
        if not remote_ok:
            return False, None, remote_message

        get_remote_executor = getattr(runner, "get_remote_executor", None)
        if not callable(get_remote_executor):
            return False, None, "远程执行器不可用"
        remote_executor = get_remote_executor()
        copy_artifact = getattr(
            remote_executor,
            "copy_config_artifact_between_workstations",
            None,
        )
        delete_artifact = getattr(remote_executor, "delete_config_artifact", None)
        if not callable(copy_artifact) or not callable(delete_artifact):
            return False, None, "远程 artifact 迁移接口不可用"

        copied_files: list[dict[str, Any]] = []
        deleted_source_files: list[dict[str, Any]] = []
        warnings: list[str] = []
        try:
            copied_files.append(dict(copy_artifact(
                config_name,
                artifact,
                source_workstation_id,
                target_workstation_id,
            )))
        except Exception as exc:
            logger.warning(
                "[IPC] migrate copy failed: config=%s artifact=%s source=%s target=%s error=%s",
                config_name,
                artifact,
                source_workstation_id,
                target_workstation_id,
                exc,
            )
            return False, None, f"迁移文件复制失败: {exc}"

        migrate_config_workstation = getattr(scheduler, "migrate_config_workstation", None)
        if not callable(migrate_config_workstation):
            return False, None, "调度器迁移接口不可用"
        ok, message = migrate_config_workstation(config_name, target_workstation_id)
        if not ok:
            return False, None, message or "构型工作站归属迁移失败"

        if delete_source:
            try:
                deleted = bool(delete_artifact(config_name, artifact, source_workstation_id))
                if deleted:
                    deleted_source_files.append({
                        "artifact": artifact,
                        "workstation_id": source_workstation_id,
                        "path": copied_files[0].get("source_path"),
                    })
                else:
                    warnings.append(
                        f"源工作站 {source_workstation_id} 的 {artifact} 文件删除失败"
                    )
            except Exception as exc:
                warning = (
                    f"源工作站 {source_workstation_id} 的 {artifact} 文件删除异常: {exc}"
                )
                warnings.append(warning)
                logger.warning("[IPC] migrate source cleanup warning: %s", warning)

        data = {
            "config_name": config_name,
            "source_workstation_id": source_workstation_id,
            "target_workstation_id": target_workstation_id,
            "mode": mode,
            "copied_files": copied_files,
            "deleted_source_files": deleted_source_files,
            "warnings": warnings,
        }
        return True, data, f"构型{config_name}已从 {source_workstation_id} 迁移到 {target_workstation_id}"

    @staticmethod
    def _normalize_target_workstation(value: Any) -> tuple[bool, str, str]:
        target = str(value or "").strip().upper()
        if not target:
            return False, "", "请指定目标工作站 (target_workstation_id)"
        configured: dict[str, str] = {
            str(workstation.get("id")).upper(): str(workstation.get("id"))
            for workstation in WORKSTATIONS
            if workstation.get("id")
        }
        configured.update(
            {workstation_id: workstation_id for workstation_id in ("WS-A", "WS-B", "WS-C", "WS-D")}
        )
        if target not in configured:
            return False, target, f"无效目标工作站: {target}"
        return True, configured[target], ""

    def _config_workstation_for_migration(self, config_name: int) -> str | None:
        state = getattr(self, "state", None)
        get_config_workstation = getattr(state, "get_config_workstation", None)
        if not callable(get_config_workstation):
            return None
        workstation_id = get_config_workstation(config_name)
        return str(workstation_id) if workstation_id else None

    def _config_statuses_for_migration(self, config_name: int) -> dict[str, str]:
        state = getattr(self, "state", None)
        get_all_steps_for_config = getattr(state, "get_all_steps_for_config", None)
        if callable(get_all_steps_for_config):
            raw = get_all_steps_for_config(config_name)
            if isinstance(raw, dict):
                statuses: dict[str, str] = {}
                for step, value in raw.items():
                    if isinstance(value, dict):
                        statuses[str(step)] = str(value.get("status", STATUS_WAITING))
                    else:
                        statuses[str(step)] = str(value)
                return statuses
        return {
            step_name: str(state.get_step_status(config_name, step_name))
            for step_name in STEP_NAMES
        }

    def _validate_migration_workstation_health(
        self,
        source_workstation_id: str,
        target_workstation_id: str,
    ) -> tuple[bool, str]:
        health = self._build_health_snapshot()
        details = health.get("workstation_ssh_details")
        if not isinstance(details, dict):
            return False, "无法读取工作站 SSH 健康状态"
        for workstation_id in (source_workstation_id, target_workstation_id):
            status = str(details.get(workstation_id, "unknown"))
            if status != "ok":
                return False, f"工作站 {workstation_id} SSH 健康状态为 {status}，不允许迁移"
        return True, ""

    def _validate_no_active_remote_task_for_migration(
        self,
        config_name: int,
    ) -> tuple[bool, str]:
        state = getattr(self, "state", None)
        runner = getattr(self, "runner", None)
        get_all_remote_tasks = getattr(state, "get_all_remote_tasks", None)
        get_remote_executor = getattr(runner, "get_remote_executor", None)
        if not callable(get_all_remote_tasks) or not callable(get_remote_executor):
            return True, ""
        remote_executor = get_remote_executor()
        query_remote_task_status = getattr(remote_executor, "query_remote_task_status", None)
        if not callable(query_remote_task_status):
            return True, ""
        for task in get_all_remote_tasks():
            try:
                task_config = int(task.get("config_name"))
            except (TypeError, ValueError):
                continue
            step_name = str(task.get("step_name") or "")
            if task_config != config_name or step_name not in {"meshing", "solver", "postprocess"}:
                continue
            workstation_id = str(task.get("workstation_id", DEFAULT_WORKSTATION_ID))
            try:
                status = str(query_remote_task_status(config_name, step_name, workstation_id))
            except Exception as exc:
                return False, f"无法确认构型{config_name}的 {step_name} 远程任务状态: {exc}"
            if status not in {"completed", "lost"}:
                return (
                    False,
                    f"构型{config_name}的 {step_name} 远程任务状态为 {status}，不允许迁移",
                )
        return True, ""

    def _release_cleaned_workstation_slots(
        self,
        step_name: str | None,
        config_name: int | str | None,
    ) -> None:
        """Release workstation slots whose remote execution files were cleaned."""
        if step_name not in {"all", "transfer", "meshing", "solver", "postprocess"}:
            return
        scheduler = getattr(self, "scheduler", None)
        workstation_slots = getattr(scheduler, "workstation_slots", None)
        if workstation_slots is None:
            return
        if config_name is None or config_name == "all":
            clear = getattr(workstation_slots, "clear", None)
            if callable(clear):
                clear()
            return
        release_config = getattr(workstation_slots, "release_config", None)
        if callable(release_config):
            release_config(config_name)

    def _validate_mutation_safe(
        self,
        *,
        action_name: str,
        config_name: int | str | None,
        step_name: str | None,
        include_downstream: bool,
    ) -> tuple[bool, str]:
        """拒绝会破坏活跃流水线或未确认远程任务现场的 reset/clean 操作。"""
        state = getattr(self, "state", None)
        if state is not None and state.get_engine_status() == "running":
            return False, f"流水线运行中，{action_name}前请先 pause 或 stop"
        blocking_local = self._find_blocking_running_step(
            config_name=config_name,
            step_name=step_name,
            include_downstream=include_downstream,
        )
        if blocking_local is not None:
            cfg, step = blocking_local
            return (
                False,
                f"构型{cfg}的 {step} 步骤仍在执行，"
                f"{action_name}前请先等待该步骤结束或 stop 后再操作",
            )

        blocking = self._find_blocking_remote_task(
            config_name=config_name,
            step_name=step_name,
            include_downstream=include_downstream,
        )
        if blocking is None:
            return True, ""

        cfg, step, status = blocking
        return (
            False,
            f"构型{cfg}的 {step} 远程任务状态为 {status}，"
            f"{action_name}前请先确认任务结束或停止后台任务",
        )

    @staticmethod
    def _normalize_config_name_for_mutation(
        config_name: int | str | None,
        *,
        allow_none: bool,
    ) -> tuple[bool, int | str | None, str]:
        """Normalize IPC reset/clean config selectors before mutation."""
        if config_name is None:
            if allow_none:
                return True, None, ""
            return False, None, "请指定构型名称 (config_name)"
        if isinstance(config_name, bool) or not isinstance(config_name, int | str):
            return (
                False,
                config_name,
                f"无效构型名称: {config_name}，必须是整数或 all",
            )
        if config_name == "all":
            return True, "all", ""
        try:
            normalized = int(config_name)
        except (TypeError, ValueError):
            return (
                False,
                config_name,
                f"无效构型名称: {config_name}，必须是整数或 all",
            )
        if normalized <= 0:
            return (
                False,
                config_name,
                f"无效构型名称: {config_name}，必须是正整数或 all",
            )
        return True, normalized, ""

    def _find_blocking_running_step(
        self,
        *,
        config_name: int | str | None,
        step_name: str | None,
        include_downstream: bool,
    ) -> tuple[int, str] | None:
        state = getattr(self, "state", None)
        if state is None or state.get_engine_status() != "paused":
            return None

        get_all_statuses = getattr(state, "get_all_statuses", None)
        if not callable(get_all_statuses):
            return None

        affected_steps = self._affected_steps(step_name, include_downstream)
        if not affected_steps:
            return None

        statuses = get_all_statuses()
        for raw_config, step_statuses in statuses.items():
            try:
                task_config = int(raw_config)
            except (TypeError, ValueError):
                task_config = raw_config
            if not isinstance(task_config, int) or not self._config_matches(config_name, task_config):
                continue
            if not isinstance(step_statuses, dict):
                continue
            for step in STEP_NAMES:
                if step in affected_steps and step_statuses.get(step) == STATUS_RUNNING:
                    return task_config, step
        return None

    def _find_blocking_remote_task(
        self,
        *,
        config_name: int | str | None,
        step_name: str | None,
        include_downstream: bool,
    ) -> tuple[int, str, str] | None:
        state = getattr(self, "state", None)
        runner = getattr(self, "runner", None)
        if state is None or runner is None:
            return None

        get_all_remote_tasks = getattr(state, "get_all_remote_tasks", None)
        get_remote_executor = getattr(runner, "get_remote_executor", None)
        if not callable(get_all_remote_tasks) or not callable(get_remote_executor):
            return None

        affected_steps = self._affected_remote_steps(step_name, include_downstream)
        if not affected_steps:
            return None

        try:
            remote_executor = get_remote_executor()
            remote_tasks = get_all_remote_tasks()
        except Exception as e:
            logger.warning(f"[IPC] 检查远程任务状态失败，拒绝变更以保护现场: {e}")
            return (-1, "remote", "unknown")

        for task in remote_tasks:
            task_step = str(task.get("step_name", ""))
            if task_step not in affected_steps:
                continue

            task_config = int(task["config_name"])
            if not self._config_matches(config_name, task_config):
                continue

            workstation_id = str(task.get("workstation_id", "default"))
            try:
                status = remote_executor.query_remote_task_status(
                    task_config,
                    task_step,
                    workstation_id=workstation_id,
                )
            except InfrastructureUnavailableError as e:
                logger.warning(
                    "[IPC] 构型%s %s 远程状态查询基础设施不可用: %s",
                    task_config, task_step, e,
                )
                return task_config, task_step, "disconnected"
            if status in {"running", "unknown"}:
                return task_config, task_step, status

        return None

    @staticmethod
    def _affected_steps(step_name: str | None, include_downstream: bool) -> set[str]:
        if step_name == "cache":
            return set(STEP_NAMES)
        if step_name in (None, "all"):
            return set(STEP_NAMES)
        if step_name not in STEP_NAMES:
            return set()
        if not include_downstream:
            return {step_name}

        step_index = STEP_NAMES.index(step_name)
        return set(STEP_NAMES[step_index:])

    @staticmethod
    def _affected_remote_steps(step_name: str | None, include_downstream: bool) -> set[str]:
        remote_steps = {"meshing", "solver", "postprocess"}
        if step_name in (None, "all", "cache"):
            return remote_steps
        if step_name not in STEP_NAMES:
            return set()
        if not include_downstream:
            return {step_name} & remote_steps

        step_index = STEP_NAMES.index(step_name)
        return set(STEP_NAMES[step_index:]) & remote_steps

    @staticmethod
    def _config_matches(requested: int | str | None, task_config: int) -> bool:
        if requested is None or requested == "all":
            return True
        try:
            return int(requested) == task_config
        except (TypeError, ValueError):
            return str(requested) == str(task_config)

    def handle_reload_config(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """处理 reload_config 命令（从 TOML 文件重新加载配置）。"""
        if self._pipeline_ever_started:
            logger.warning("[Config] 流水线已启动过，拒绝重新加载配置（需重启 Daemon）")
            return False, None, "流水线已启动过，配置已锁定。请重启 Daemon 后再修改配置"
        from engine.config import reload_config_from_toml
        if reload_config_from_toml():
            return True, None, "配置已从 autofluid_config.toml 重新加载"
        return True, None, "TOML 配置文件不存在，使用默认配置"

    def handle_get_log_entries(self, params: dict) -> tuple[bool, Any, str]:
        """处理 get_log_entries 命令（增量拉取日志条目）。

        params: {
            "since_id": int,       # 返回 ID 大于此值的条目
            "limit": int,          # 最大返回条数（默认 50）
            "level_filter": str,   # 按级别过滤（可选）
            "source_filter": str,  # 按来源过滤（可选）
        }
        """
        handler = get_broadcast_handler()
        if handler is None:
            return True, {"entries": [], "latest_id": 0, "total": 0}, "日志处理器未安装"

        since_id = params.get("since_id", 0)
        limit = params.get("limit", 50)
        level_filter = params.get("level_filter")
        source_filter = params.get("source_filter")
        include_polling = bool(params.get("include_polling", False))
        include_lifecycle = bool(params.get("include_lifecycle", False))
        include_config_scoped = bool(params.get("include_config_scoped", False))

        result = handler.get_entries(
            since_id=since_id,
            limit=limit,
            level_filter=level_filter,
            source_filter=source_filter,
            include_polling=include_polling,
            include_lifecycle=include_lifecycle,
            include_config_scoped=include_config_scoped,
        )
        return True, result, ""


# ============================================================================
# 主入口
# ============================================================================

def main():
    """启动 Pipeline Daemon 的主入口。"""
    daemon = PipelineDaemon()
    daemon.start()


if __name__ == "__main__":
    main()
