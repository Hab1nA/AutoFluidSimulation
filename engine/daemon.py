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
from typing import Any, Tuple

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
from utils.logger import setup_logger, install_broadcast_handler, get_broadcast_handler
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

        # 0. 确保必要目录存在（必须在 StateManager 之前，因为 StateManager 需要 data/ 目录存放 SQLite 数据库）
        ensure_directories()

        # 1. 安装日志广播处理器（供 TUI 增量拉取）
        install_broadcast_handler(capacity=1000)

        # 2. 状态管理器
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

        # 0. 验证配置（目录已在 __init__ 中确保存在）
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
        """从 Excel 加载构型数据并同步到状态库。"""
        excel_path = LOCAL_PATHS["excel"]
        try:
            configs = read_model_configs(excel_path)
            if not configs:
                logger.error("Excel 中未读取到任何构型数据！")
                return
            self.state.load_configs(configs)
            logger.info(f"已从 Excel 同步 {len(configs)} 个构型到状态库")
        except FileNotFoundError as e:
            logger.error(f"Excel 文件未找到: {e}")
        except ValueError as e:
            logger.error(f"Excel 数据格式错误: {e}")
        except OSError as e:
            logger.error(f"Excel 读取失败: {e}")
        except Exception as e:
            logger.error(f"Excel 数据加载失败: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # IPC 命令处理器
    # ------------------------------------------------------------------

    def handle_start(self, params: dict = None) -> Tuple[bool, Any, str]:
        """处理 start 命令（启动或继续流水线）。

        状态机：
        - running → 已是运行中，若暂停标志不一致则修复
        - paused  → 检查调度器状态后恢复或重启
        - stopped / 其他 → 全新启动
        """
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
                self.scheduler._pipeline_thread = scheduler_thread
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
        self.scheduler._pipeline_thread = scheduler_thread

        return True, None, "流水线已启动"

    def handle_pause(self, params: dict = None) -> Tuple[bool, Any, str]:
        """处理 pause 命令。

        暂停行为取决于当前所处阶段：
        - SW 宏执行中：标志置位，宏继续运行但完成后立即暂停
        - 下游步骤执行中：当前步骤完成后暂停
        - 空闲状态：直接标记为暂停
        """
        engine_status = self.state.get_engine_status()
        if engine_status == "paused":
            return True, None, "流水线已在暂停状态"
        if engine_status == "stopped":
            return True, None, "流水线已停止，无需暂停"

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

    def handle_get_log_entries(self, params: dict) -> Tuple[bool, Any, str]:
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
