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
    LOCAL_PATHS, IPC_CONFIG, STEP_NAMES, WORKSTATIONS, STATUS_RUNNING, STATUS_ERROR,
    ensure_directories, get_step_filename, get_workstation_ssh_health_interval,
    is_server_mode, validate_config,
)
from engine.config_assigner import ConfigAssigner
from engine.config_fingerprint import compute_config_fingerprint, get_db_path_for_fingerprint
from engine.local_worker_adapter import LocalWorkerAdapter
from engine.local_worker_registry import LocalWorkerRegistry
from engine.state_manager import StateManager
from engine.task_runner import TaskRunner
from engine.scheduler import PipelineScheduler
from ipc.server import IPCServer
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


def _json_size_bytes(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


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
        self._started_at_epoch: float | None = None
        self._last_worker_ssh_checks: dict[str, str] = {}
        self._ssh_health_thread: threading.Thread | None = None
        self._ssh_health_stop_event = threading.Event()
        self._ssh_health_interval_seconds = get_workstation_ssh_health_interval()
        self._config_warnings: list[str] = []

        # 运行标志
        self._running = False
        self._stop_event = threading.Event()  # 主循环阻塞用，set() 唤醒
        self._pipeline_ever_started = False  # 一旦流水线启动过即置 True，锁定配置

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
        config_warnings = validate_config()
        self._config_warnings = list(config_warnings)
        for w in config_warnings:
            logger.warning(f"[CONFIG] {w}")

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

            # ---- 2.5 启动时状态一致性检查 ----
            # 新进程没有调度器线程，残留的 paused/running 状态一定是不一致的
            # （stop() 挂起或进程被杀导致 set_engine_status("stopped") 未执行）
            stale_status = self.state.get_engine_status()
            if stale_status in ("paused", "running"):
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
                pass
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，守护进程正在退出...")
        finally:
            self.shutdown()

    def shutdown(self):
        """优雅关闭守护进程。

        每个清理步骤独立 try-except，确保任何一步失败都不阻断后续清理。
        """
        logger.info("PipelineDaemon 正在关闭...")
        self._running = False
        self._stop_event.set()
        self._stop_workstation_ssh_health_monitor()

        # 停止调度器（内部已包含 disconnect_ssh）
        if self.scheduler:
            try:
                self.scheduler.stop()
            except Exception as e:
                logger.warning(f"调度器停止异常: {e}")

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
        """Persist stable workstation assignments for newly loaded configs."""
        if self.state is None:
            raise RuntimeError("StateManager 未初始化，请先调用 start()")

        workstation_ids = [str(ws.get("id")) for ws in WORKSTATIONS if ws.get("id")]
        assigner = ConfigAssigner(workstation_ids, self.state.get_all_configs())
        assigned_count = 0
        for config_name in self.state.get_all_configs():
            if self.state.get_config_workstation(config_name) is not None:
                continue
            self.state.set_config_workstation(
                config_name,
                assigner.get_workstation(config_name),
            )
            assigned_count += 1
        if assigned_count:
            logger.info("[Config] 已持久化 %d 个构型的工作站分配", assigned_count)

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

        env = os.environ.copy()
        env["AUTOFLUID_IPC_HOST"] = str(IPC_CONFIG["host"])
        env["AUTOFLUID_IPC_PORT"] = str(IPC_CONFIG["port"])
        auth_token = str(IPC_CONFIG.get("auth_token", "") or "")
        if auth_token:
            env["AUTOFLUID_IPC_AUTH_TOKEN"] = auth_token

        log_dir = str(LOCAL_PATHS.get("log_dir") or os.path.join(_PROJECT_ROOT, "logs"))
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "alert_watcher.log")
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
                logger.warning("检测到引擎状态为 running 但调度器暂停标志已置位，执行恢复")
                self.scheduler.resume()
                return True, None, "流水线已恢复运行（修正不一致状态）"
            if not self.scheduler.pipeline_alive:
                logger.warning("检测到引擎状态为 running 但调度器线程已退出，重新启动流水线")
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

        log_dir = str(LOCAL_PATHS.get("log_dir") or os.path.join(_PROJECT_ROOT, "logs"))
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "local_worker_autostart.log")
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
        # 统一退出路径：仅设置 _stop_event，由 run() 的 finally 块执行 shutdown()
        self._stop_event.set()
        return True, None, "后台引擎正在安全退出..."

    def handle_check(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """处理 check 命令（系统自检）。"""
        if self.runner is None:
            raise RuntimeError("TaskRunner 未初始化，请先调用 start()")
        results = dict(self.runner.run_system_check())
        results["health"] = self._build_health_snapshot()
        return True, results, "系统自检完成"

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
            self._refresh_workstation_ssh_checks()
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
        worker = self.local_worker_registry.heartbeat(worker_id)
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
                self.runner.disconnect_ssh()
            except Exception as e:
                logger.warning("[Worker] 刷新配置后断开旧 SSH 连接异常: %s", e)

        results.update(self._refresh_workstation_ssh_checks())
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

    def _refresh_workstation_ssh_checks(self) -> dict[str, Any]:
        """Refresh workstation SSH readiness using the current runner."""
        results: dict[str, Any] = {
            "ssh_checks": {},
            "ssh_targets": {},
        }
        if self.runner is None:
            return results

        for ws in WORKSTATIONS:
            ws_id = str(ws.get("id", "default"))
            target = self._workstation_ssh_target(ws)
            results["ssh_targets"][ws_id] = target
            try:
                ssh = self.runner.get_ssh(ws_id)
                connected = ssh.is_connected()
                results["ssh_checks"][ws_id] = "ok" if connected else "disconnected"
            except Exception as e:
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
        while not self._ssh_health_stop_event.wait(self._ssh_health_interval_seconds):
            self._run_workstation_ssh_health_check_once()

    def _run_workstation_ssh_health_check_once(self) -> dict[str, Any]:
        if self.runner is None:
            return {"ssh_checks": {}, "ssh_targets": {}}
        try:
            return self._refresh_workstation_ssh_checks()
        except Exception as exc:
            logger.warning("[ServerMode] 后台工作站 SSH 健康检查失败: %s", exc)
            return {"ssh_checks": {}, "ssh_targets": {}}

    def _stop_workstation_ssh_health_monitor(self) -> None:
        stop_event = getattr(self, "_ssh_health_stop_event", None)
        if stop_event is not None:
            stop_event.set()
        thread = getattr(self, "_ssh_health_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._ssh_health_thread = None

    def handle_worker_stop(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """停止所有 worker：断开工作站 SSH 连接，清理 Registry 任务队列。"""
        params = params or {}
        results: dict[str, Any] = {
            "ssh_disconnected": [],
            "registry_cleared": False,
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
        ok_stop, data_stop, msg_stop = self.handle_worker_stop(params)
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
                "pipeline_started": self._pipeline_ever_started,
                "daemon_started_at": started_at,
                "daemon_started_at_display": started_at_display,
                "daemon_uptime_seconds": uptime_seconds,
                "config_load_error": getattr(self, "_config_load_error", None),
            }
            return True, status, ""
        status = {
            "engine_status": self.state.get_engine_status(),
            "sw_macro_started": self.state.is_sw_macro_started(),
            "barrier_passed": self.state.is_global_barrier_met(),
            "pipeline_started": self._pipeline_ever_started,
            "daemon_started_at": started_at,
            "daemon_started_at_display": started_at_display,
            "daemon_uptime_seconds": uptime_seconds,
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
        workstation_configs: list[Mapping[str, Any]] = list(WORKSTATIONS) or [{"id": "default"}]
        for workstation in workstation_configs:
            workstation_id = str(workstation.get("id", "default"))
            workstation_targets[workstation_id] = self._workstation_ssh_target(workstation)
            # Prefer the active health-check result over transport state; stale
            # Paramiko transports can outlive the reverse tunnel they depend on.
            if workstation_id in last_worker_checks:
                workstation_details[workstation_id] = str(last_worker_checks[workstation_id])
                continue
            ssh = ssh_pool.get(workstation_id)
            if ssh is None:
                workstation_details[workstation_id] = str(
                    last_worker_checks.get(workstation_id, "unknown")
                )
                continue
            try:
                connection_is_active = getattr(ssh, "connection_is_active", None)
                if callable(connection_is_active):
                    connected = bool(connection_is_active())
                else:
                    connected = bool(ssh.is_connected())
                workstation_details[workstation_id] = (
                    "ok" if connected else "disconnected"
                )
            except Exception as exc:
                workstation_details[workstation_id] = f"error: {exc}"

        detail_values = set(workstation_details.values())
        if any(value == "ok" for value in detail_values):
            server_to_workstation_ssh = "ok"
        elif any(value == "unknown" for value in detail_values):
            server_to_workstation_ssh = "unknown"
        else:
            server_to_workstation_ssh = "disconnected"

        return {
            "local_worker_online": bool(online_workers),
            "server_to_local_ssh": server_to_local_ssh,
            "server_to_workstation_ssh": server_to_workstation_ssh,
            "workstation_ssh_details": workstation_details,
            "workstation_ssh_targets": workstation_targets,
            "config_warnings": list(getattr(self, "_config_warnings", [])),
        }

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
            "engine": engine,
            "health": self._build_health_snapshot(),
            "logs": logs,
        }
        self._trim_dashboard_logs_to_budget(data)
        return True, data, ""

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
        kept = list(entries)
        while kept and _json_size_bytes(data) > _MAX_DASHBOARD_LOG_BYTES:
            kept.pop(0)
            logs["entries"] = kept
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
                    logger.info("clean all cache 后台任务完成")
                except Exception as e:
                    logger.error(f"clean all cache 后台任务异常: {e}", exc_info=True)

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
            and (step_name == "all" or step_name in {"meshing", "solver"})
        )

        if self.runner is None:
            raise RuntimeError("TaskRunner 未初始化，请先调用 start()")
        if needs_background:
            def _do_clean_step():
                try:
                    self.runner.clean_step_files(step_name, config_name)
                    logger.info(f"clean {step_name} (config={config_name}) 后台任务完成")
                except Exception as e:
                    logger.error(f"clean {step_name} (config={config_name}) 后台任务异常: {e}", exc_info=True)

            threading.Thread(target=_do_clean_step, daemon=True,
                           name=f"Clean-{step_name}-Bg").start()
            msg = f"已启动后台清理 {step_name} 步骤的文件"
            if config_name is not None and config_name != "all":
                msg += f" (构型{config_name})"
        else:
            self.runner.clean_step_files(step_name, config_name)
            msg = f"已清理 {step_name} 步骤的文件"
            if config_name is not None and config_name != "all":
                msg += f" (构型{config_name})"

        # 如果清理范围涉及 SW 步骤，请求文件监控器重置追踪状态。
        # 监控器不会修改调度器拥有的共享 pause。
        if step_name == "all" or step_name == "sw":
            if self.scheduler:
                self.scheduler.request_file_monitor_reset()

        return True, None, msg

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
            status = remote_executor.query_remote_task_status(
                task_config,
                task_step,
                workstation_id=workstation_id,
            )
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
        remote_steps = {"meshing", "solver"}
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
