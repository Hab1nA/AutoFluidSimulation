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
===============================================================================
"""
import os
import sys
import signal
import threading
import time
from typing import Any, Dict, Tuple

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config import (
    LOCAL_PATHS, REMOTE_CONFIG, IPC_CONFIG, ENGINE_CONFIG,
    STEP_NAMES, STEP_INDEX,
    STATUS_WAITING, STATUS_RUNNING, STATUS_COMPLETED, STATUS_ERROR,
    ensure_directories, validate_config,
)
from engine.state_manager import StateManager
from engine.task_runner import TaskRunner
from engine.scheduler import PipelineScheduler
from ipc.server import IPCServer
from utils.logger import setup_logger
from utils.excel_reader import read_model_configs

logger = setup_logger("PipelineDaemon")


class PipelineDaemon:
    """
    流水线后台守护进程。

    负责协调所有子系统：状态管理、任务调度、IPC 通信。
    作为独立进程运行，TUI 客户端通过 IPC 与之交互。
    """

    def __init__(self):
        """初始化守护进程各组件。"""
        logger.info("=" * 60)
        logger.info("PipelineDaemon 初始化中...")
        logger.info("=" * 60)

        # 1. 状态管理器
        self.state = StateManager()

        # 2. 任务执行器
        self.runner = TaskRunner(self.state)

        # 3. 流水线调度器
        self.scheduler = PipelineScheduler(self.state, self.runner)

        # 4. IPC 服务器
        self.ipc_server = IPCServer()
        self.ipc_server.register_default_handlers(self)

        # 运行标志
        self._running = False

        logger.info("PipelineDaemon 初始化完成")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self):
        """启动守护进程。"""
        logger.info("=" * 60)
        logger.info("PipelineDaemon 启动中...")
        logger.info("=" * 60)

        # 0. 确保目录存在 & 验证配置
        ensure_directories()
        config_warnings = validate_config()
        for w in config_warnings:
            logger.warning(f"[CONFIG] {w}")

        self._running = True

        # 1. 加载 Excel 数据（初始化状态库）
        self._load_excel_data()

        # 2. 启动 IPC 服务器（接受 TUI 客户端连接）
        try:
            self.ipc_server.start()
        except OSError as e:
            logger.error(f"IPC 服务器启动失败: {e}")
            logger.error("可能已有另一个 Daemon 在运行？")
            self._running = False
            self.ipc_server.stop()  # 清理部分初始化的 socket
            return

        # 3. 注册信号处理（优雅退出）
        self._setup_signal_handlers()

        logger.info("PipelineDaemon 已就绪，等待客户端指令...")
        logger.info(f"IPC 地址: {IPC_CONFIG['host']}:{IPC_CONFIG['port']}")

        # 4. 主循环（保持进程存活）
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("收到中断信号")
        finally:
            self.shutdown()

    def shutdown(self):
        """优雅关闭守护进程。"""
        logger.info("PipelineDaemon 正在关闭...")
        self._running = False

        # 停止调度器
        self.scheduler.stop()

        # 断开 SSH
        self.runner.disconnect_ssh()

        # 停止 IPC 服务器
        self.ipc_server.stop()

        logger.info("PipelineDaemon 已关闭")

    def _setup_signal_handlers(self):
        """设置系统信号处理器。"""
        def signal_handler(signum, frame):
            logger.info(f"收到信号 {signum}，正在关闭...")
            self._running = False

        # Windows 上仅支持 SIGINT 和 SIGTERM（部分）
        for sig in [signal.SIGINT, signal.SIGTERM]:
            try:
                signal.signal(sig, signal_handler)
            except (AttributeError, ValueError):
                pass  # Windows 不支持某些信号

    def _load_excel_data(self):
        """从 Excel 加载构型数据并同步到状态库。

        启动时自动与设计表同步：
        - 新增设计表中的构型（状态置为 Waiting）
        - 保留已有构型的状态（断点续传）
        - 删除设计表中已不存在的构型
        """
        excel_path = LOCAL_PATHS["excel"]
        try:
            configs = read_model_configs(excel_path)
            if not configs:
                logger.error("Excel 中未读取到任何构型数据！")
                return
            self.state.load_configs(configs)
            logger.info(f"已从 Excel 同步 {len(configs)} 个构型到状态库")
        except (ValueError, OSError) as e:
            logger.error(f"Excel 数据加载失败: {e}")

    # ------------------------------------------------------------------
    # IPC 命令处理器
    # ------------------------------------------------------------------

    def handle_start(self, params: dict = None) -> Tuple[bool, Any, str]:
        """处理 start 命令（启动或继续流水线）。"""
        engine_status = self.state.get_engine_status()
        if engine_status == "running":
            return True, None, "流水线已在运行中"

        if engine_status == "paused":
            # 暂停状态下：恢复运行
            self.scheduler.resume()
            return True, None, "流水线已恢复运行"

        # 先标记为 running（防止竞态：调度线程尚未设置状态时收到重复 start 命令）
        self.state.set_engine_status("running")

        # 在独立线程中启动调度器（避免阻塞 IPC 响应）
        scheduler_thread = threading.Thread(
            target=self.scheduler.start_pipeline,
            daemon=True,
            name="SchedulerMain"
        )
        scheduler_thread.start()

        return True, None, "流水线已启动"

    def handle_pause(self, params: dict = None) -> Tuple[bool, Any, str]:
        """处理 pause 命令。"""
        self.scheduler.pause()
        return True, None, "流水线已暂停（当前运行步骤完成后不再取新任务）"

    def handle_stop(self, params: dict = None) -> Tuple[bool, Any, str]:
        """处理 full_quit 命令。"""
        logger.info("收到 full_quit 命令，准备完全退出...")
        # 在另一个线程中执行关闭，以便给客户端返回响应
        threading.Thread(target=self.shutdown, daemon=True).start()
        return True, None, "后台引擎正在安全退出..."

    def handle_check(self, params: dict = None) -> Tuple[bool, Any, str]:
        """处理 check 命令（系统自检）。"""
        results = self.runner.run_system_check()
        return True, results, "系统自检完成"

    def handle_get_all_status(self, params: dict = None) -> Tuple[bool, Any, str]:
        """获取所有构型的状态。"""
        statuses = self.state.get_all_statuses()
        return True, statuses, ""

    def handle_get_statistics(self, params: dict = None) -> Tuple[bool, Any, str]:
        """获取统计信息。"""
        stats = self.state.get_statistics()
        engine_status = self.state.get_engine_status()
        stats["engine_status"] = engine_status
        stats["sw_macro_started"] = self.state.is_sw_macro_started()
        stats["barrier_passed"] = self.state.is_global_barrier_met()
        return True, stats, ""

    def handle_get_engine_status(self, params: dict = None) -> Tuple[bool, Any, str]:
        """获取引擎状态。"""
        status = {
            "engine_status": self.state.get_engine_status(),
            "sw_macro_started": self.state.is_sw_macro_started(),
            "barrier_passed": self.state.is_global_barrier_met(),
        }
        return True, status, ""

    def handle_reset_step(self, params: dict) -> Tuple[bool, Any, str]:
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

    def handle_clean_step(self, params: dict) -> Tuple[bool, Any, str]:
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


# ============================================================================
# 主入口
# ============================================================================

def main():
    """启动 Pipeline Daemon 的主入口。"""
    daemon = PipelineDaemon()
    daemon.start()


if __name__ == "__main__":
    main()
