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
    LOCAL_PATHS, IPC_CONFIG, STEP_NAMES, ensure_directories, validate_config,
)
from engine.config_fingerprint import compute_config_fingerprint, get_db_path_for_fingerprint
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

        # 运行标志
        self._running = False
        self._stop_event = threading.Event()  # 主循环阻塞用，set() 唤醒

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
        """优雅关闭守护进程。"""
        logger.info("PipelineDaemon 正在关闭...")
        self._running = False
        self._stop_event.set()

        # 停止调度器
        if self.scheduler:
            self.scheduler.stop()

        # 断开 SSH
        if self.runner:
            self.runner.disconnect_ssh()

        # 停止 IPC 服务器
        if self.ipc_server:
            self.ipc_server.stop()

        # 释放进程锁
        release_process_lock()

        logger.info("PipelineDaemon 已关闭")

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

    def handle_start(self, params: dict | None = None) -> tuple[bool, Any, str]:
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
            # 暂停状态下恢复运行
            # 检查调度器主线程是否存活（SW 宏执行期间线程可能因异常退出）
            if not self.scheduler.pipeline_alive and not self.scheduler.is_paused:
                # 调度器线程已死亡且暂停标志未置位：
                # 可能因 SW 失败等原因退出，但引擎状态未正确切换为 stopped
                # 重新启动流水线（start_pipeline 会检查断点续传）
                logger.info("检测到调度器线程已退出且未暂停，重新启动流水线...")
                self.state.set_engine_status("running")
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
        self.state.set_engine_status("running")

        # 在独立线程中启动调度器（避免阻塞 IPC 响应）
        scheduler_thread = threading.Thread(
            target=self.scheduler.start_pipeline,
            daemon=True,
            name="SchedulerMain"
        )
        scheduler_thread.start()
        self.scheduler.set_pipeline_thread(scheduler_thread)

        return True, None, "流水线已启动"

    def handle_pause(self, params: dict | None = None) -> tuple[bool, Any, str]:
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

    def handle_stop(self, params: dict | None = None) -> tuple[bool, Any, str]:
        """处理 full_quit 命令。"""
        logger.info("收到 full_quit 命令，准备完全退出...")
        # 在另一个线程中执行关闭，以便给客户端返回响应
        threading.Thread(target=self.shutdown, daemon=True).start()
        return True, None, "后台引擎正在安全退出..."

    def handle_check(self, params: dict | None = None) -> tuple[bool, Any, str]:
        """处理 check 命令（系统自检）。"""
        if self.runner is None:
            raise RuntimeError("TaskRunner 未初始化，请先调用 start()")
        results = self.runner.run_system_check()
        return True, results, "系统自检完成"

    def handle_get_all_status(self, params: dict | None = None) -> tuple[bool, Any, str]:
        """获取所有构型的状态。"""
        if self.state is None:
            raise RuntimeError("StateManager 未初始化，请先调用 start()")
        statuses = self.state.get_all_statuses()
        return True, statuses, ""

    def handle_get_statistics(self, params: dict | None = None) -> tuple[bool, Any, str]:
        """获取统计信息。"""
        if self.state is None:
            raise RuntimeError("StateManager 未初始化，请先调用 start()")
        stats = self.state.get_statistics()
        engine_status = self.state.get_engine_status()
        stats["engine_status"] = engine_status
        stats["sw_macro_started"] = self.state.is_sw_macro_started()
        stats["barrier_passed"] = self.state.is_global_barrier_met()
        return True, stats, ""

    def handle_get_engine_status(self, params: dict | None = None) -> tuple[bool, Any, str]:
        """获取引擎状态。"""
        if self.state is None:
            raise RuntimeError("StateManager 未初始化，请先调用 start()")
        status = {
            "engine_status": self.state.get_engine_status(),
            "sw_macro_started": self.state.is_sw_macro_started(),
            "barrier_passed": self.state.is_global_barrier_met(),
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

        # 验证 step_name（"all" 是合法值，无需校验）
        if step_name != "all" and step_name not in STEP_NAMES:
            return False, None, f"无效步骤名: {step_name}，有效值: {STEP_NAMES} 或 all"

        # 判断是否需要后台线程（远程步骤 + 清理范围大）
        needs_background = (
            config_name in (None, "all")
            and (step_name == "all" or step_name in {"Meshing", "Solver"})
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
        return True, None, msg

    def handle_reload_config(self, params: dict | None = None) -> tuple[bool, Any, str]:
        """处理 reload_config 命令（从 TOML 文件重新加载配置）。"""
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
