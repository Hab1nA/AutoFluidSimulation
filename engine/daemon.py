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

import os
import sys
import signal
import threading
from typing import Any

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config import (
    LOCAL_PATHS, IPC_CONFIG, STEP_NAMES, WORKSTATIONS, ensure_directories,
    is_server_mode, validate_config,
)
from engine.config_assigner import ConfigAssigner
from engine.config_fingerprint import compute_config_fingerprint, get_db_path_for_fingerprint
from engine.local_worker_registry import LocalWorkerRegistry
from engine.state_manager import StateManager
from engine.task_runner import TaskRunner
from engine.scheduler import PipelineScheduler
from ipc.server import IPCServer
from utils.logger import setup_logger, install_broadcast_handler, get_broadcast_handler
from utils.excel_reader import read_model_configs
from utils.process_utils import is_process_alive, read_pid_file, write_pid_file, remove_pid_file, check_ipc_ready

logger = setup_logger("PipelineDaemon")

# PID 文件路径（与 main.py 保持一致）
_PID_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_DAEMON_PID_FILE = os.path.join(_PID_DIR, "daemon.pid")


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

        # 0. 确保必要目录存在
        ensure_directories()

        # 1. 安装日志广播处理器（供 TUI 增量拉取）
        install_broadcast_handler(capacity=1000)

        # 业务组件在 start() 中创建，因为需要先读取 Excel 确定数据库路径
        self.state = None
        self.runner = None
        self.scheduler = None
        self.ipc_server = None
        self.local_worker_registry = LocalWorkerRegistry()

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
        config_warnings = validate_config()
        for w in config_warnings:
            logger.warning(f"[CONFIG] {w}")

        self._running = True

        # ---- 1. 加载 Excel 数据，计算构型指纹，确定数据库路径 ----
        excel_path = LOCAL_PATHS["excel"]
        try:
            configs = read_model_configs(excel_path)
        except FileNotFoundError as e:
            logger.error(f"Excel 文件未找到: {e}")
            release_process_lock()
            self._running = False
            return
        except (ValueError, OSError) as e:
            logger.error(f"Excel 读取失败: {e}")
            release_process_lock()
            self._running = False
            return

        if not configs:
            logger.error("Excel 中未读取到任何构型数据！")
            release_process_lock()
            self._running = False
            return

        fingerprint = compute_config_fingerprint(configs)
        db_path = get_db_path_for_fingerprint(fingerprint)
        logger.info(f"构型组合指纹: {fingerprint}（{len(configs)} 个构型）")
        logger.info(f"状态数据库: {db_path}")

        # ---- 2. 创建业务组件 ----
        self.state = StateManager(db_path=db_path)
        self.state.load_configs(configs)
        self._assign_config_workstations()
        logger.info(f"已同步 {len(configs)} 个构型到状态库")

        self.runner = TaskRunner(self.state)
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

        # ---- 4. 注册信号处理 ----
        self._setup_signal_handlers()

        logger.info("PipelineDaemon 已就绪，等待客户端指令...")
        logger.info(f"IPC 地址: {IPC_CONFIG['host']}:{IPC_CONFIG['port']}")
        logger.info(f"数据库指纹: {fingerprint}")

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
        engine_status = self.state.get_engine_status()

        if engine_status == "running":
            # 二次确认：检查调度器的暂停标志是否被意外置位
            # （防止 start_pipeline 末尾覆盖 engine_status 导致的不一致）
            if self.scheduler.is_paused:
                logger.warning("检测到引擎状态为 running 但调度器暂停标志已置位，执行恢复")
                self.scheduler.resume()
                return True, None, "流水线已恢复运行（修正不一致状态）"
            return True, None, "流水线已在运行中"

        if engine_status == "paused":
            if self._server_mode_requires_worker():
                return self._server_mode_worker_missing_response()
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
            return self._server_mode_worker_missing_response()

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
        registry = getattr(self, "local_worker_registry", None)
        has_worker = (
            registry is not None
            and registry.has_online_worker()
        )
        has_adapter = getattr(self, "local_worker_adapter", None) is not None
        return is_server_mode() and (not has_worker or not has_adapter)

    @staticmethod
    def _server_mode_worker_missing_response() -> tuple[bool, None, str]:
        return (
            False,
            None,
            "server 模式下后端运行在 ocar，必须先接入 LocalWorker 才能启动流水线",
        )

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
        results = self.runner.run_system_check()
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
        worker = self.local_worker_registry.register(worker_id, capabilities)
        return True, worker, "LocalWorker 已注册"

    def handle_worker_heartbeat(self, params: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
        """Handle LocalWorker heartbeat."""
        params = params or {}
        worker_id = str(params.get("worker_id") or "")
        if not worker_id:
            return False, None, "缺少 worker_id"
        worker = self.local_worker_registry.heartbeat(worker_id)
        return True, worker, "LocalWorker 心跳已更新"

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
        if self.state is None:
            raise RuntimeError("StateManager 未初始化，请先调用 start()")
        status = {
            "engine_status": self.state.get_engine_status(),
            "sw_macro_started": self.state.is_sw_macro_started(),
            "barrier_passed": self.state.is_global_barrier_met(),
            "pipeline_started": self._pipeline_ever_started,
        }
        return True, status, ""

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

        result = handler.get_entries(
            since_id=since_id,
            limit=limit,
            level_filter=level_filter,
            source_filter=source_filter,
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
