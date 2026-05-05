#!/usr/bin/env python3
# =============================================================================
# pipeline_controller.py — 液氧甲烷火箭发动机仿真自动化跑批总控脚本
#
# 功能：
#   串联 SolidWorks → SpaceClaim → 远程 Fluent 的端到端仿真流水线，
#   带断点续传与 Rich TUI 动态监控界面。
#
# 用法：
#   python pipeline_controller.py                    # 启动 TUI 模式
#   python pipeline_controller.py --no-tui           # 纯日志模式（无 TUI）
#   python pipeline_controller.py --retry 5          # 最大重试次数
#   python pipeline_controller.py --resume           # 仅断点续传（跳过已完成任务）
# =============================================================================
import os
import sys
import signal
import time
import threading
import logging
import argparse
from datetime import datetime
from typing import List, Dict, Optional

from config import (
    LOCAL_CONFIG,
    REMOTE_CONFIG,
    GLOBAL_CONFIG,
    CONFIG_COMBINATIONS,
    ConfigCombination,
    DEFAULT_PARAMS,
    setup_logging,
    ensure_directories,
)
from modules.state_manager import StateManager, Status
from modules.tui_display import TUIManager
from rich.live import Live

# ---------------------------------------------------------------------------
# 日志设置
# ---------------------------------------------------------------------------
logger = logging.getLogger("PipelineController")

# 全局标志：优雅关闭
_shutdown_requested = False


def signal_handler(signum, frame):
    """SIGINT/SIGTERM 信号处理器"""
    global _shutdown_requested
    logger.info("收到中断信号，正在优雅关闭...")
    _shutdown_requested = True


# ---------------------------------------------------------------------------
# CLI 参数解析
# ---------------------------------------------------------------------------
def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="液氧甲烷火箭发动机仿真自动化跑批控制脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python pipeline_controller.py                        # 标准 TUI 模式
  python pipeline_controller.py --no-tui               # 无 TUI，纯日志
  python pipeline_controller.py --retry 3              # 最大重试 3 次
  python pipeline_controller.py --no-tui --retry 5     # 组合使用
        """,
    )
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="禁用 TUI 界面，仅输出纯文本日志",
    )
    parser.add_argument(
        "--retry",
        type=int,
        default=None,
        help=f"最大重试次数（默认: {GLOBAL_CONFIG['max_retry']}）",
    )
    parser.add_argument(
        "--resume-only",
        action="store_true",
        help="仅执行断点续传恢复，不启动新任务（检查 Computing 状态）",
    )
    parser.add_argument(
        "--poll-only",
        action="store_true",
        help="仅轮询远程任务状态，不执行本地阶段",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# 阶段执行器
# ---------------------------------------------------------------------------
class PipelineExecutor:
    """
    流水线执行器。

    在每个构型上依次调用各阶段驱动器。
    通过 StateManager 记录状态，实现断点续传。
    """

    def __init__(self, state_manager: StateManager, tui: Optional[TUIManager] = None):
        self.state_manager = state_manager
        self.tui = tui
        self.configs = CONFIG_COMBINATIONS
        self.total = len(self.configs)

    def _update_tui(self, config_id: str, stage: str, status: str):
        """更新 TUI 显示（如果有）"""
        if self.tui:
            self.tui.push_log(config_id, stage, status)

    def _should_process_sw(self, st: dict) -> bool:
        """判断是否需要执行 SolidWorks 阶段"""
        return st["sw_status"] in (Status.PENDING, Status.ERROR, Status.IN_PROGRESS)

    def _should_process_sc(self, st: dict) -> bool:
        """判断是否需要执行 SpaceClaim 阶段"""
        return st["sc_status"] in (Status.PENDING, Status.ERROR, Status.IN_PROGRESS)

    def _should_transfer(self, st: dict) -> bool:
        """判断是否需要传输"""
        return st["transfer_status"] in (Status.PENDING, Status.ERROR, Status.IN_PROGRESS)

    def _should_launch_remote(self, st: dict) -> bool:
        """判断是否需要启动远程任务"""
        return st["fluent_status"] in (
            Status.PENDING, Status.ERROR, Status.IN_PROGRESS, Status.TRANSFERRED
        )

    def _should_poll(self, st: dict) -> bool:
        """判断是否需要轮询远程任务"""
        return st["fluent_status"] == Status.COMPUTING

    def run_sw_phase(self, config: ConfigCombination, st: dict):
        """
        执行 SolidWorks 阶段。

        Args:
            config: 构型组合
            st: 当前状态记录
        """
        if not self._should_process_sw(st):
            logger.debug("构型 %s 跳过 SW 阶段 (状态: %s)", config.id, st["sw_status"])
            return

        logger.info(">>> 构型 %s — 开始 SolidWorks 阶段", config.id)
        self._update_tui(config.id, "SW建模", "InProgress")
        self.state_manager.update_sw_status(config.id, Status.IN_PROGRESS)

        try:
            from modules.solidworks_driver import SolidWorksDriver
            sw = SolidWorksDriver()
            success = sw.process_config(config.id, config.params, st["retry_count"])
            sw.cleanup()

            if success:
                self.state_manager.update_sw_status(config.id, Status.DONE)
                self._update_tui(config.id, "SW建模", "Done")
                logger.info("构型 %s SW 阶段完成 ✓", config.id)
            else:
                self.state_manager.increment_retry(config.id)
                self.state_manager.update_sw_status(config.id, Status.ERROR,
                                                    "SolidWorks 处理失败")
                self._update_tui(config.id, "SW建模", "Error")
        except Exception as e:
            logger.error("SW 阶段异常: %s", e)
            self.state_manager.increment_retry(config.id)
            self.state_manager.update_sw_status(config.id, Status.ERROR, str(e))
            self._update_tui(config.id, "SW建模", "Error")

    def run_sc_phase(self, config: ConfigCombination, st: dict):
        """
        执行 SpaceClaim 阶段。

        Args:
            config: 构型组合
            st: 当前状态记录
        """
        if not self._should_process_sc(st):
            logger.debug("构型 %s 跳过 SC 阶段 (状态: %s)", config.id, st["sc_status"])
            return

        logger.info(">>> 构型 %s — 开始 SpaceClaim 阶段", config.id)
        self._update_tui(config.id, "SC处理", "InProgress")
        self.state_manager.update_sc_status(config.id, Status.IN_PROGRESS)

        try:
            from modules.spaceclaim_driver import SpaceClaimDriver
            sc = SpaceClaimDriver()
            success = sc.process_config(config.id)

            if success:
                self.state_manager.update_sc_status(config.id, Status.DONE)
                self._update_tui(config.id, "SC处理", "Done")
                logger.info("构型 %s SC 阶段完成 ✓", config.id)
            else:
                self.state_manager.update_sc_status(config.id, Status.ERROR,
                                                    "SpaceClaim 处理失败")
                self._update_tui(config.id, "SC处理", "Error")
        except Exception as e:
            logger.error("SC 阶段异常: %s", e)
            self.state_manager.update_sc_status(config.id, Status.ERROR, str(e))
            self._update_tui(config.id, "SC处理", "Error")

    def run_transfer_phase(self, config: ConfigCombination, st: dict):
        """
        执行 SFTP 传输 + 远程启动阶段。

        Args:
            config: 构型组合
            st: 当前状态记录
        """
        if not self._should_transfer(st):
            logger.debug("构型 %s 跳过传输阶段 (状态: %s)", config.id, st["transfer_status"])
            return

        if not self._should_launch_remote(st):
            logger.debug("构型 %s 跳过远程启动 (fluent_status: %s)", config.id, st["fluent_status"])
            return

        logger.info(">>> 构型 %s — 开始远程传输与启动", config.id)
        self._update_tui(config.id, "文件传输", "InProgress")
        self.state_manager.update_transfer_status(config.id, Status.IN_PROGRESS)

        try:
            from modules.remote_scheduler import RemoteScheduler
            remote = RemoteScheduler()
            success = remote.process_config(config.id)
            remote.cleanup()

            if success:
                self.state_manager.update_transfer_status(config.id, Status.DONE)
                self.state_manager.update_fluent_status(config.id, Status.COMPUTING)
                self._update_tui(config.id, "文件传输", "Done")
                self._update_tui(config.id, "Fluent求解", "Computing")
                logger.info("构型 %s 已提交到远程计算 ✓", config.id)
            else:
                self.state_manager.update_transfer_status(config.id, Status.ERROR,
                                                         "远程传输或启动失败")
                self._update_tui(config.id, "文件传输", "Error")
        except Exception as e:
            logger.error("远程传输阶段异常: %s", e)
            self.state_manager.update_transfer_status(config.id, Status.ERROR, str(e))
            self._update_tui(config.id, "文件传输", "Error")

    def poll_remote_phase(self):
        """
        轮询所有 Computing 状态的远程任务。
        """
        computing_configs = self.state_manager.get_configs_by_fluent_status(Status.COMPUTING)
        if not computing_configs:
            return

        logger.info("开始轮询 %d 个远程任务...", len(computing_configs))

        try:
            from modules.remote_scheduler import RemoteScheduler
            remote = RemoteScheduler()

            for cfg_id in computing_configs:
                if remote.check_remote_done(cfg_id):
                    self.state_manager.update_fluent_status(cfg_id, Status.COMPLETED)
                    self._update_tui(cfg_id, "Fluent求解", "Completed")
                    logger.info("构型 %s 远程计算完成 ✓", cfg_id)
                else:
                    # 检查进程是否还在运行
                    if not remote.check_remote_running(cfg_id):
                        logger.warning("构型 %s 远程进程可能已终止", cfg_id)
                        # 不立即标记错误，给下次轮询机会

            remote.cleanup()
        except Exception as e:
            logger.error("远程轮询异常: %s", e)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main():
    global _shutdown_requested

    # --- 解析参数 ---
    args = parse_args()

    # --- 覆盖全局配置 ---
    if args.retry is not None:
        GLOBAL_CONFIG["max_retry"] = args.retry
        logger.info("最大重试次数已设置为: %d", args.retry)

    # --- 初始化 ---
    ensure_directories()
    setup_logging()

    logger.info("=" * 60)
    logger.info("🔥 液氧甲烷火箭发动机仿真 — 自动化跑批流水线启动")
    logger.info("启动时间: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("构型总数: %d", len(CONFIG_COMBINATIONS))
    logger.info("最大重试: %d", GLOBAL_CONFIG["max_retry"])
    logger.info("=" * 60)

    # --- 注册信号处理（Ctrl+C 优雅关闭）---
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # --- 初始化状态管理器 ---
    state_manager = StateManager()
    state_manager.initialize_configs(CONFIG_COMBINATIONS)

    # --- 断点续传：恢复 Computing 状态 ---
    computing_configs = state_manager.get_configs_by_fluent_status(Status.COMPUTING)
    if computing_configs:
        logger.info("检测到 %d 个 Computing 状态的任务，尝试检查远程完成标志...", len(computing_configs))
        try:
            from modules.remote_scheduler import RemoteScheduler
            remote = RemoteScheduler()
            for cfg_id in computing_configs:
                if remote.check_remote_done(cfg_id):
                    state_manager.update_fluent_status(cfg_id, Status.COMPLETED)
                    logger.info("断点续传恢复: 构型 %s → Completed", cfg_id)
            remote.cleanup()
        except Exception as e:
            logger.warning("断点续传检查异常（将在后续轮询中处理）: %s", e)

    # --- 纯轮询模式 ---
    if args.poll_only:
        logger.info("进入纯轮询模式...")
        executor = PipelineExecutor(state_manager, tui=None)
        while not _shutdown_requested:
            executor.poll_remote_phase()
            # 检查是否全部完成
            stats = state_manager.get_stats()
            if stats["computing"] == 0:
                logger.info("所有远程任务已完成，退出轮询模式")
                break
            time.sleep(GLOBAL_CONFIG.get("poll_interval", 30))
        return

    # --- 初始化 TUI ---
    tui = None
    if not args.no_tui:
        tui = TUIManager(state_manager)
        logger.info("TUI 界面已初始化，刷新率: %d fps", tui.refresh_rate)

    # --- 创建执行器 ---
    executor = PipelineExecutor(state_manager, tui=tui)

    # --- 构建参数映射（config_id → ConfigCombination）---
    config_map = {c.id: c for c in CONFIG_COMBINATIONS}

    # =====================================================================
    # 主循环：逐构型顺序执行各阶段
    # =====================================================================
    def pipeline_worker():
        """
        流水线工作函数（在后台线程中执行）。

        依次执行：SW → SC → 传输+远程启动 → 远程轮询
        通过 StateManager 记录每个阶段的状态，实现断点续传。
        """
        try:
            # --- 阶段 1-2: 本地 SW + SC 串行处理 ---
            for config in CONFIG_COMBINATIONS:
                if _shutdown_requested:
                    logger.info("收到关闭信号，停止新任务调度")
                    break

                st = state_manager.get_state(config.id)
                if st is None:
                    continue

                # 如果该构型已全部完成，跳过
                if st["fluent_status"] == Status.COMPLETED:
                    logger.info("构型 %s 已完成，跳过", config.id)
                    continue

                # SW 阶段
                if st["retry_count"] < GLOBAL_CONFIG["max_retry"]:
                    executor.run_sw_phase(config, st)
                else:
                    logger.warning("构型 %s 重试次数已达上限 (%d)，跳过 SW 阶段",
                                   config.id, st["retry_count"])

                if _shutdown_requested:
                    break

                # SC 阶段（仅在 SW 完成后进行）
                st = state_manager.get_state(config.id)  # 刷新状态
                if st is None:
                    logger.error("构型 %s 在数据库中失联，跳过 SC", config.id)
                    continue
                if st["sw_status"] == Status.DONE:
                    executor.run_sc_phase(config, st)

                if _shutdown_requested:
                    break

            # --- 阶段 3: 传输 + 远程启动 ---
            for config in CONFIG_COMBINATIONS:
                if _shutdown_requested:
                    break

                st = state_manager.get_state(config.id)
                if st is None:
                    continue

                if st["sc_status"] == Status.DONE or st["transfer_status"] in (
                    Status.PENDING, Status.ERROR, Status.IN_PROGRESS
                ):
                    executor.run_transfer_phase(config, st)

            # --- 阶段 4: 远程轮询（直到全部完成）---
            logger.info("所有构型已提交，进入远程轮询阶段...")
            if tui:
                tui.push_event("status_update", "", "进入远程计算轮询阶段")

            poll_count = 0
            while not _shutdown_requested:
                stats = state_manager.get_stats()
                if stats["computing"] == 0:
                    logger.info("所有远程任务已完成！")
                    if tui:
                        tui.push_event("status_update", "", "🎉 全部任务完成！")
                    break

                executor.poll_remote_phase()

                # 更新重试计数器
                poll_count += 1
                if poll_count % 10 == 0:  # 每 10 次轮询输出一次汇总
                    logger.info("轮询汇总 #%d: 完成=%d 计算中=%d 错误=%d",
                                poll_count, stats["completed"],
                                stats["computing"], stats["error"])

                time.sleep(GLOBAL_CONFIG.get("poll_interval", 30))

        except Exception as e:
            logger.critical("流水线工作线程发生未处理异常: %s", e, exc_info=True)
        finally:
            pipeline_complete.set()

    # =====================================================================
    # 启动执行（TUI 在主线程渲染，流水线在 Worker 线程）
    # =====================================================================
    pipeline_complete = threading.Event()

    try:
        if tui:
            # TUI Live 在主线程中渲染（Rich 最佳实践）
            # 使用 Live 上下文管理器确保退出时正确清理
            with Live(tui._render_loop(), console=tui.console,
                      refresh_per_second=tui.refresh_rate, screen=True) as live:
                tui._live = live
                tui._running = True

                # 启动流水线工作线程
                worker = threading.Thread(target=pipeline_worker, daemon=False)
                worker.start()

                # 主线程负责 TUI 刷新，直到流水线完成或被中断
                while not pipeline_complete.is_set() and not _shutdown_requested:
                    live.update(tui._render_loop())
                    time.sleep(0.15)  # ~6.7 fps，流畅且节省 CPU

                tui._running = False

            worker.join()
        else:
            # 无 TUI 模式：直接在主线程同步运行
            pipeline_worker()

    except KeyboardInterrupt:
        logger.info("用户中断 (Ctrl+C)，正在保存状态...")
        _shutdown_requested = True
    except Exception as e:
        logger.critical("总控制器发生未处理异常: %s", e, exc_info=True)
    finally:
        # --- 收尾 ---
        if tui:
            tui.stop()
        logger.info("流水线总控制器已停止")

        # 输出最终统计
        stats = state_manager.get_stats()
        logger.info("=" * 60)
        logger.info("📊 最终统计:")
        logger.info("  总任务数: %d", stats["total"])
        logger.info("  已完成: %d", stats["completed"])
        logger.info("  计算中: %d", stats["computing"])
        logger.info("  错误: %d", stats["error"])
        logger.info("=" * 60)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    main()