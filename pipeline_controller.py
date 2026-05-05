#!/usr/bin/env python3
# =============================================================================
# pipeline_controller.py — 液氧甲烷火箭发动机仿真自动化跑批总控脚本
#
# v2.0 变更说明：
#   网格划分与仿真求解拆分为两个独立阶段，各自拥有独立的完成标志和状态字段。
#   新增交互式命令 Shell（Ctrl+T 打开），支持 pause/resume/status/retry 等命令。
#   远程子阶段轮询逻辑拆分：meshing_status → solving_status，解决异步管控问题。
#
# 用法：
#   python pipeline_controller.py                    # 启动 TUI 模式
#   python pipeline_controller.py --no-tui           # 纯日志模式（无 TUI）
#   python pipeline_controller.py --retry 5          # 最大重试次数
#   python pipeline_controller.py --poll-only        # 仅轮询远程任务
#   python pipeline_controller.py --shell            # 直接进入交互 Shell（无流水线）
# =============================================================================
import os
import sys
import signal
import time
import threading
import logging
import argparse
import queue
import traceback
from datetime import datetime
from typing import Dict, Optional

from config import (
    LOCAL_CONFIG,
    REMOTE_CONFIG,
    GLOBAL_CONFIG,
    CONFIG_COMBINATIONS,
    ConfigCombination,
    setup_logging,
    ensure_directories,
)
from modules.state_manager import StateManager, Status
from modules.tui_display import TUIManager
from modules.remote_scheduler import SUBSTAGE_MESHING, SUBSTAGE_SOLVING
from rich.live import Live

# ---------------------------------------------------------------------------
# 日志设置
# ---------------------------------------------------------------------------
logger = logging.getLogger("PipelineController")

# 全局标志：优雅关闭
_shutdown_requested = False
# 暂停标志（交互 Shell 用）
# 使用 threading.Event 替代 bool + Lock，消除 check-then-wait 竞态窗口
_pause_event = threading.Event()  # set() = 暂停，clear() = 继续


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
  python pipeline_controller.py --poll-only            # 仅轮询远程任务
  python pipeline_controller.py --shell                # 仅启动交互 Shell
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
        "--poll-only",
        action="store_true",
        help="仅轮询远程任务状态，不执行本地阶段",
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="启动交互式命令 Shell（不执行流水线，仅管理任务）",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# 交互式命令 Shell
# ---------------------------------------------------------------------------
class InteractiveShell:
    """
    交互式命令 Shell。

    在流水线运行期间，按 Ctrl+T 可打开内嵌 Shell，
    或在流水线完成后自动进入交互模式。
    支持的命令：status, pause, resume, retry, reset, quit
    """

    def __init__(self, state_manager: StateManager, tui: Optional[TUIManager] = None):
        self.state_manager = state_manager
        self.tui = tui
    def run_command(self, cmd_line: str) -> str:
        """解析并执行一条命令，返回响应字符串"""
        global _pause_event, _shutdown_requested

        parts = cmd_line.strip().split()
        if not parts:
            return ""

        cmd = parts[0].lower()

        if cmd in ("help", "h", "?"):
            return self._cmd_help()

        elif cmd in ("status", "st", "s"):
            return self._cmd_status()

        elif cmd in ("pause", "p"):
            _pause_event.set()
            logger.info("⏸ 流水线已暂停")
            return "⏸ 流水线已暂停。输入 'resume' 继续。"

        elif cmd in ("resume", "r"):
            _pause_event.clear()
            logger.info("▶ 流水线已恢复")
            return "▶ 流水线已恢复"

        elif cmd in ("retry", "rt"):
            if len(parts) < 2:
                return "用法: retry <config_id>"
            config_id = parts[1]
            return self._cmd_retry(config_id)

        elif cmd in ("reset", "rst"):
            if len(parts) < 3:
                return "用法: reset <config_id> <stage>\n阶段: sw, sc, transfer, meshing, solving, all"
            config_id = parts[1]
            stage = parts[2].lower()
            return self._cmd_reset(config_id, stage)

        elif cmd in ("quit", "q", "exit"):
            global _shutdown_requested
            _shutdown_requested = True
            logger.info("收到退出命令")
            return "正在优雅关闭..."

        elif cmd == "stats":
            return self._cmd_stats()

        elif cmd == "errors":
            return self._cmd_errors()

        elif cmd == "computing":
            return self._cmd_computing()

        else:
            return f"未知命令: {cmd}。输入 'help' 查看帮助。"

    def _cmd_help(self) -> str:
        return """
┌─────────────────────────────────────────────────────────┐
│  交互式命令 Shell 帮助                                    │
├─────────────────────────────────────────────────────────┤
│  status / st      查看所有任务状态                        │
│  stats            查看统计信息                            │
│  errors           列出所有出错的任务                       │
│  computing        列出所有正在计算的任务                   │
│  pause / p        暂停流水线                              │
│  resume / r       恢复流水线                              │
│  retry <id>       重试指定构型的全部失败阶段               │
│  reset <id> <stg> 重置指定构型的指定阶段状态               │
│  quit / q         优雅退出程序                            │
│  help / h         显示此帮助信息                          │
└─────────────────────────────────────────────────────────┘
"""

    def _cmd_status(self) -> str:
        all_states = self.state_manager.get_all_states()
        if not all_states:
            return "暂无任务记录"

        header = (
            f"{'构型 ID':<20s} {'SW':<8s} {'SC':<8s} {'传输':<8s} {'网格':<8s} {'求解':<8s} {'重试':<4s}"
        )
        lines = [header, "-" * 80]

        for st in all_states:
            line = (
                f"{st['config_id']:<20s} "
                f"{st['sw_status']:<8s} "
                f"{st['sc_status']:<8s} "
                f"{st['transfer_status']:<8s} "
                f"{st.get('meshing_status', '?')!s:<8s} "
                f"{st.get('solving_status', '?')!s:<8s} "
                f"{st['retry_count']:<4d}"
            )
            lines.append(line)

        return "\n".join(lines)

    def _cmd_stats(self) -> str:
        stats = self.state_manager.get_stats()
        return (
            f"📊 统计信息:\n"
            f"  总任务:   {stats['total']}\n"
            f"  已完成:   {stats['completed']}\n"
            f"  进行中:   {stats['in_progress']}\n"
            f"  错误:     {stats['error']}\n"
            f"  等待:     {stats.get('pending', '?')}"
        )

    def _cmd_errors(self) -> str:
        error_configs = self.state_manager.get_error_configs()
        if not error_configs:
            return "✅ 没有出错的任务"
        lines = ["❌ 出错的任务列表:"]
        for ec in error_configs:
            lines.append(f"  - {ec['config_id']}: {ec.get('error_msg', '未知错误')}")
        return "\n".join(lines)

    def _cmd_computing(self) -> str:
        # 检查 mesh computing
        mesh_list = self.state_manager.get_configs_by_status("meshing_status", Status.COMPUTING)
        # 检查 solver computing
        sol_list = self.state_manager.get_configs_by_status("solving_status", Status.COMPUTING)

        lines = []
        if mesh_list:
            lines.append("⚡ 网格划分中:")
            for m in mesh_list:
                lines.append(f"  - {m}")
        if sol_list:
            lines.append("⚡ 仿真求解中:")
            for s in sol_list:
                lines.append(f"  - {s}")
        if not lines:
            return "没有正在计算的任务"
        return "\n".join(lines)

    def _cmd_retry(self, config_id: str) -> str:
        st = self.state_manager.get_state(config_id)
        if st is None:
            return f"构型 {config_id} 不存在"

        # 重置所有非完成状态的阶段
        changed = []
        if st["sw_status"] in (Status.ERROR, Status.PENDING):
            self.state_manager.update_sw_status(config_id, Status.PENDING)
            self.state_manager.reset_retry(config_id)
            changed.append("SW")
        if st["sc_status"] in (Status.ERROR, Status.PENDING):
            self.state_manager.update_sc_status(config_id, Status.PENDING)
            changed.append("SC")
        if st["transfer_status"] in (Status.ERROR, Status.PENDING):
            self.state_manager.update_transfer_status(config_id, Status.PENDING)
            changed.append("Transfer")

        return f"已重置 {config_id} 的阶段: {', '.join(changed) if changed else '(无需重置)'}"

    def _cmd_reset(self, config_id: str, stage: str) -> str:
        stage_map = {
            "sw": ("sw_status", Status.PENDING, "SW建模"),
            "sc": ("sc_status", Status.PENDING, "SC处理"),
            "transfer": ("transfer_status", Status.PENDING, "文件传输"),
            "meshing": ("meshing_status", Status.PENDING, "网格划分"),
            "solving": ("solving_status", Status.PENDING, "仿真求解"),
            "all": (None, None, "全部"),
        }

        if stage == "all":
            self.state_manager.full_reset(config_id)
            return f"已完全重置构型 {config_id}"

        if stage not in stage_map:
            return f"未知阶段: {stage}。可用: sw, sc, transfer, meshing, solving, all"

        field, new_status, label = stage_map[stage]
        st = self.state_manager.get_state(config_id)
        if st is None:
            return f"构型 {config_id} 不存在"

        if field == "meshing_status":
            self.state_manager.update_meshing_status(config_id, new_status)
        elif field == "solving_status":
            self.state_manager.update_solving_status(config_id, new_status)
        else:
            # 使用通用更新方法
            self.state_manager._update_field(config_id, field, new_status)

        return f"已重置 {config_id} 的 {label} 阶段"

    def start_interactive_mode(self):
        """启动交互式命令输入循环"""
        global _shutdown_requested
        print("\n" + "=" * 60)
        print("💻 进入交互式命令 Shell 模式")
        print("输入 'help' 查看可用命令，'quit' 退出")
        print("=" * 60 + "\n")

        while not _shutdown_requested:
            try:
                user_input = input("AutoFluid> ").strip()
                if user_input:
                    response = self.run_command(user_input)
                    if response:
                        print(response)
            except (EOFError, KeyboardInterrupt):
                print("\n退出交互 Shell")
                _shutdown_requested = True
                break


# ---------------------------------------------------------------------------
# 阶段执行器
# ---------------------------------------------------------------------------
class PipelineExecutor:
    """
    流水线执行器（v2.0 — 网格/求解分阶段）。

    在每个构型上依次调用各阶段驱动器。
    通过 StateManager 记录状态，实现断点续传。
    """

    def __init__(self, state_manager: StateManager, tui: Optional[TUIManager] = None):
        self.state_manager = state_manager
        self.tui = tui
        self.configs = CONFIG_COMBINATIONS
        self.total = len(self.configs)
        self._remote = None  # 延迟初始化的 RemoteScheduler 实例

    def _get_remote(self):
        """延迟初始化 RemoteScheduler"""
        if self._remote is None:
            from modules.remote_scheduler import RemoteScheduler
            self._remote = RemoteScheduler()
            self._remote.connect()
        return self._remote

    def _cleanup_remote(self):
        """内部清理远程连接"""
        if self._remote:
            self._remote.cleanup()
            self._remote = None

    def connect_remote(self) -> Optional[object]:
        """公共接口：建立远程连接并返回远程调度器实例"""
        return self._get_remote()

    def disconnect_remote(self):
        """公共接口：断开远程连接"""
        self._cleanup_remote()

    def _update_tui(self, config_id: str, stage: str, status: str):
        """更新 TUI 显示（如果有）"""
        if self.tui:
            self.tui.push_log(config_id, stage, status)

    # -------------------------------------------------------------------
    # 阶段判断辅助（v2.0 — 使用 meshing/solving 字段）
    # -------------------------------------------------------------------
    def _should_process_sw(self, st: dict) -> bool:
        return st["sw_status"] in (Status.PENDING, Status.ERROR, Status.IN_PROGRESS)

    def _should_process_sc(self, st: dict) -> bool:
        return st["sc_status"] in (Status.PENDING, Status.ERROR, Status.IN_PROGRESS)

    def _should_transfer(self, st: dict) -> bool:
        return st["transfer_status"] in (Status.PENDING, Status.ERROR, Status.IN_PROGRESS)

    def _should_launch_meshing(self, st: dict) -> bool:
        return st.get("meshing_status", Status.PENDING) in (
            Status.PENDING, Status.ERROR, Status.IN_PROGRESS, Status.TRANSFERRED
        )

    def _should_launch_solving(self, st: dict) -> bool:
        """
        仅在 PENDING 或 ERROR 时允许启动求解。
        IN_PROGRESS 和 COMPUTING 均表示已经提交过，不应重复启动。
        """
        current = st.get("solving_status", Status.PENDING)
        # PENDING          → 可启动（全新任务）
        # ERROR            → 可启动（重试失败任务）
        # IN_PROGRESS      → 不可启动（主线程正将其置为 COMPUTING 的过程中）
        # COMPUTING/DONE   → 不可启动（已提交/已完成）
        # 注意：不要包含 COMPUTED，枚举中不存在该值
        return current in (Status.PENDING, Status.ERROR)

    def _should_poll_meshing(self, st: dict) -> bool:
        return st.get("meshing_status") == Status.COMPUTING

    def _should_poll_solving(self, st: dict) -> bool:
        return st.get("solving_status") == Status.COMPUTING

    # -------------------------------------------------------------------
    # 阶段 2: SolidWorks
    # -------------------------------------------------------------------
    def run_sw_phase(self, config: ConfigCombination, st: dict):
        if not self._should_process_sw(st):
            logger.debug("构型 %s 跳过 SW 阶段 (状态: %s)", config.id, st["sw_status"])
            return

        logger.info(">>> 构型 %s — 开始 SolidWorks 阶段", config.id)
        self._update_tui(config.id, "SW建模", "InProgress")
        self.state_manager.update_sw_status(config.id, Status.IN_PROGRESS)

        try:
            from modules.solidworks_driver import SolidWorksDriver
            sw = SolidWorksDriver()
            success = sw.process_config(config.id, config.params)
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

    # -------------------------------------------------------------------
    # 阶段 3: SpaceClaim
    # -------------------------------------------------------------------
    def run_sc_phase(self, config: ConfigCombination, st: dict):
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

    # -------------------------------------------------------------------
    # 阶段 4a: SFTP 上传 + 远程网格启动
    # -------------------------------------------------------------------
    def run_transfer_and_meshing(self, config: ConfigCombination, st: dict):
        """
        上传 SCDOC 到远程并启动网格划分。
        成功后设置 meshing_status = Computing。
        """
        if not self._should_transfer(st) and not self._should_launch_meshing(st):
            logger.debug("构型 %s 跳过传输/网格启动 (状态: transfer=%s meshing=%s)",
                        config.id, st["transfer_status"], st.get("meshing_status"))
            return

        logger.info(">>> 构型 %s — 开始传输与网格划分启动", config.id)

        remote = self._get_remote()

        # Step 1: 上传 SCDOC
        if self._should_transfer(st):
            self._update_tui(config.id, "文件传输", "InProgress")
            self.state_manager.update_transfer_status(config.id, Status.IN_PROGRESS)

            if remote.upload_scdoc(config.id):
                self.state_manager.update_transfer_status(config.id, Status.DONE)
                self._update_tui(config.id, "文件传输", "Done")
                logger.info("构型 %s 文件传输完成 ✓", config.id)
            else:
                self.state_manager.update_transfer_status(config.id, Status.ERROR, "SFTP 上传失败")
                self._update_tui(config.id, "文件传输", "Error")
                return

        # Step 2: 启动网格划分
        if self._should_launch_meshing(st):
            self._update_tui(config.id, "网格划分", "InProgress")
            self.state_manager.update_meshing_status(config.id, Status.IN_PROGRESS)

            if remote.launch_substage(config.id, SUBSTAGE_MESHING):
                self.state_manager.update_meshing_status(config.id, Status.COMPUTING)
                self._update_tui(config.id, "网格划分", "Computing")
                logger.info("构型 %s 网格划分已提交 ✓", config.id)
            else:
                self.state_manager.update_meshing_status(config.id, Status.ERROR, "网格启动失败")
                self._update_tui(config.id, "网格划分", "Error")

    # -------------------------------------------------------------------
    # 阶段 4b: 远程求解启动（网格完成后调用）
    # -------------------------------------------------------------------
    def launch_solving_if_ready(self, config: ConfigCombination, st: dict):
        """网格完成后，启动求解阶段"""
        if not self._should_launch_solving(st):
            return

        # 网格必须已完成
        if st.get("meshing_status") not in (Status.DONE, Status.COMPLETED):
            return

        # 二次确认：若求解已为 COMPUTING 则说明前一次提交已成功（防止竞态）
        if st.get("solving_status") == Status.COMPUTING:
            logger.debug("构型 %s 求解已在 Computing 中，跳过重复启动", config.id)
            return

        logger.info(">>> 构型 %s — 启动仿真求解", config.id)
        self._update_tui(config.id, "仿真求解", "InProgress")
        self.state_manager.update_solving_status(config.id, Status.IN_PROGRESS)

        remote = self._get_remote()
        if remote.launch_substage(config.id, SUBSTAGE_SOLVING):
            self.state_manager.update_solving_status(config.id, Status.COMPUTING)
            self._update_tui(config.id, "仿真求解", "Computing")
            logger.info("构型 %s 仿真求解已提交 ✓", config.id)
        else:
            self.state_manager.update_solving_status(config.id, Status.ERROR, "求解启动失败")
            self._update_tui(config.id, "仿真求解", "Error")

    # -------------------------------------------------------------------
    # 轮询：网格划分完成检查
    # -------------------------------------------------------------------
    def poll_meshing_phase(self):
        """轮询所有 Computing 状态的网格任务"""
        computing = self.state_manager.get_configs_by_status("meshing_status", Status.COMPUTING)
        if not computing:
            return

        logger.info("轮询网格任务: %d 个 Computing", len(computing))
        remote = self._get_remote()

        for config_id in computing:
            try:
                if remote.check_substage_done(config_id, SUBSTAGE_MESHING):
                    self.state_manager.update_meshing_status(config_id, Status.DONE)
                    self._update_tui(config_id, "网格划分", "Done")
                    logger.info("构型 %s 网格划分完成 ✓", config_id)
                elif remote.check_substage_error(config_id, SUBSTAGE_MESHING):
                    self.state_manager.update_meshing_status(config_id, Status.ERROR, "网格错误")
                    self._update_tui(config_id, "网格划分", "Error")
                    logger.error("构型 %s 网格划分出错 ✗", config_id)
                elif not remote.check_substage_running(config_id, SUBSTAGE_MESHING):
                    logger.warning("构型 %s 网格进程似乎已终止但无 done.txt", config_id)
            except Exception as e:
                logger.error("轮询网格 %s 异常: %s", config_id, e)

    # -------------------------------------------------------------------
    # 轮询：仿真求解完成检查
    # -------------------------------------------------------------------
    def poll_solving_phase(self):
        """轮询所有 Computing 状态的求解任务"""
        computing = self.state_manager.get_configs_by_status("solving_status", Status.COMPUTING)
        if not computing:
            return

        logger.info("轮询求解任务: %d 个 Computing", len(computing))
        remote = self._get_remote()

        for config_id in computing:
            try:
                if remote.check_substage_done(config_id, SUBSTAGE_SOLVING):
                    self.state_manager.update_solving_status(config_id, Status.COMPLETED)
                    self._update_tui(config_id, "仿真求解", "Completed")
                    logger.info("构型 %s 仿真求解完成 ✓", config_id)
                elif remote.check_substage_error(config_id, SUBSTAGE_SOLVING):
                    self.state_manager.update_solving_status(config_id, Status.ERROR, "求解错误")
                    self._update_tui(config_id, "仿真求解", "Error")
                    logger.error("构型 %s 仿真求解出错 ✗", config_id)
                elif not remote.check_substage_running(config_id, SUBSTAGE_SOLVING):
                    logger.warning("构型 %s 求解进程似乎已终止但无 done.txt", config_id)
            except Exception as e:
                logger.error("轮询求解 %s 异常: %s", config_id, e)

    def poll_all_remote(self):
        """综合轮询：网格 + 求解"""
        self.poll_meshing_phase()
        self.poll_solving_phase()

    # -------------------------------------------------------------------
    # 检查是否全部完成
    # -------------------------------------------------------------------
    def all_remote_done(self) -> bool:
        """检查所有远程任务是否已全部完成（网格和求解）"""
        stats = self.state_manager.get_stats()
        # 没有 Computing 状态的任务即表示全部完成
        return stats.get("computing_meshing", 0) == 0 and stats.get("computing_solving", 0) == 0


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
    logger.info("🔥 液氧甲烷火箭发动机仿真 — 自动化跑批流水线 (v2.0)")
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

    # --- 纯 Shell 模式 ---
    if args.shell:
        shell = InteractiveShell(state_manager)
        shell.start_interactive_mode()
        return

    # --- 断点续传：恢复 Computing 状态 ---
    _restore_computing_status(state_manager)

    # --- 纯轮询模式 ---
    if args.poll_only:
        _run_poll_only(state_manager)
        return

    # --- 初始化 TUI ---
    tui = None
    if not args.no_tui:
        tui = TUIManager(state_manager)
        logger.info("TUI 界面已初始化，刷新率: %d fps", tui.refresh_rate)

    # --- 创建执行器 ---
    executor = PipelineExecutor(state_manager, tui=tui)
    shell = InteractiveShell(state_manager, tui=tui)

    # =====================================================================
    # 流水线工作函数
    # =====================================================================
    def pipeline_worker():
        """
        流水线工作函数（在后台线程中执行）。

        v2.0 执行顺序：
          1. SW → SC（本地串行）
          2. 传输 + 远程网格启动
          3. 网格完成轮询 → 启动求解
          4. 求解完成轮询
        """
        try:
            # --- 阶段 1-2: 本地 SW + SC 串行处理 ---
            for config in CONFIG_COMBINATIONS:
                if _shutdown_requested:
                    logger.info("收到关闭信号，停止新任务调度")
                    break

                # 暂停检查（事件驱动，零 CPU 浪费）
                _pause_event.wait()  # 阻塞直到 resume 调用 clear()

                st = state_manager.get_state(config.id)
                if st is None:
                    continue

                # 如果该构型已全部完成，跳过
                if st.get("solving_status") == Status.COMPLETED:
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

            # --- 阶段 3: 传输 + 远程网格启动 ---
            for config in CONFIG_COMBINATIONS:
                if _shutdown_requested:
                    break
                _pause_event.wait()

                st = state_manager.get_state(config.id)
                if st is None:
                    continue

                # SC 完成 或 传输/网格状态为 Pending/Error 时尝试
                if st["sc_status"] == Status.DONE:
                    executor.run_transfer_and_meshing(config, st)

            # --- 阶段 4: 远程轮询（网格 → 启动求解 → 求解轮询）---
            logger.info("所有本地阶段已完成，进入远程监控阶段...")
            if tui:
                tui.push_event("status_update", "", "进入远程计算监控阶段")

            poll_count = 0
            while not _shutdown_requested:
                # 暂停检查（事件驱动）
                _pause_event.wait()

                # Step A: 轮询网格完成
                executor.poll_meshing_phase()

                # Step B: 网格完成后启动求解
                for config in CONFIG_COMBINATIONS:
                    st = state_manager.get_state(config.id)
                    if st and st.get("meshing_status") == Status.DONE:
                        executor.launch_solving_if_ready(config, st)

                # Step C: 轮询求解完成
                executor.poll_solving_phase()

                # 检查是否全部完成
                if executor.all_remote_done():
                    logger.info("🎉 所有远程任务已完成！")
                    if tui:
                        tui.push_event("status_update", "", "🎉 全部任务完成！")
                    break

                poll_count += 1
                if poll_count % 10 == 0:
                    stats = state_manager.get_stats()
                    logger.info("轮询汇总 #%d: 完成=%d 网格计算中=%d 求解计算中=%d 错误=%d",
                                poll_count, stats["completed"],
                                stats.get("computing_meshing", 0),
                                stats.get("computing_solving", 0),
                                stats["error"])

                # 分片 sleep，每 2 秒检查一次 Ctrl+C / 关闭请求
                _deadline = time.time() + REMOTE_CONFIG.get("poll_interval", 30)
                while time.time() < _deadline:
                    if _shutdown_requested:
                        break
                    time.sleep(min(2.0, _deadline - time.time()))

        except Exception as e:
            logger.critical("流水线工作线程发生未处理异常: %s", e)
            logger.critical(traceback.format_exc())
        finally:
            executor._cleanup_remote()
            pipeline_complete.set()

    # =====================================================================
    # 启动执行（TUI 在主线程渲染，流水线在 Worker 线程）
    # =====================================================================
    pipeline_complete = threading.Event()

    try:
        if tui:
            # TUI Live 在主线程中渲染
            with Live(tui._render_loop(), console=tui.console,
                      refresh_per_second=tui.refresh_rate, screen=True) as live:
                tui._live = live
                tui._running = True

                # 启动流水线工作线程
                worker = threading.Thread(target=pipeline_worker, daemon=False)
                worker.start()

                # 主线程负责 TUI 刷新 + 交互 Shell
                while not pipeline_complete.is_set() and not _shutdown_requested:
                    live.update(tui._render_loop())
                    time.sleep(0.15)

                tui._running = False

            worker.join(timeout=10.0)
            if worker.is_alive():
                logger.error(
                    "工作线程在 10 秒内未退出（可能卡在 COM/子进程调用中），"
                    "强制终止。远程任务不受影响——它们通过 Start-Process 独立运行。"
                )
                os._exit(1)

            # 流水线结束后进入交互 Shell
            if not _shutdown_requested:
                print("\n")  # 从 TUI 全屏模式退出后留空行
                shell.start_interactive_mode()

        else:
            # 无 TUI 模式：直接在主线程同步运行
            pipeline_worker()

            # 流水线结束后进入交互 Shell
            if not _shutdown_requested:
                shell.start_interactive_mode()

    except KeyboardInterrupt:
        logger.info("用户中断 (Ctrl+C)，正在保存状态...")
        _shutdown_requested = True
    except Exception as e:
        logger.critical("总控制器发生未处理异常: %s", e)
        logger.critical(traceback.format_exc())
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
        logger.info("  网格计算中: %d", stats.get("computing_meshing", 0))
        logger.info("  求解计算中: %d", stats.get("computing_solving", 0))
        logger.info("  错误: %d", stats["error"])
        logger.info("=" * 60)


def _restore_computing_status(state_manager: StateManager):
    """
    断点续传恢复：检查之前标记为 Computing 的任务是否远程已完成。

    v2.1: 合并单次 SSH 连接，统一检查 mesh + solver Computing 状态。
    """
    mesh_computing = state_manager.get_configs_by_status("meshing_status", Status.COMPUTING)
    sol_computing = state_manager.get_configs_by_status("solving_status", Status.COMPUTING)

    if not mesh_computing and not sol_computing:
        logger.info("无需恢复（无 Computing 状态的任务）")
        return

    remote = None
    try:
        from modules.remote_scheduler import RemoteScheduler
        remote = RemoteScheduler()
        if not remote.connect():
            logger.warning("断点续传恢复: SSH 连接失败，跳过远程状态检查")
            return

        # --- 网格 Computing 恢复 ---
        if mesh_computing:
            logger.info("检测到 %d 个网格 Computing 状态的任务，尝试恢复...", len(mesh_computing))
            for config_id in mesh_computing:
                if remote.check_substage_done(config_id, SUBSTAGE_MESHING):
                    state_manager.update_meshing_status(config_id, Status.DONE)
                    logger.info("断点续传恢复: 构型 %s 网格 → Done", config_id)
                elif remote.check_substage_error(config_id, SUBSTAGE_MESHING):
                    state_manager.update_meshing_status(config_id, Status.ERROR, "上次运行出错")
                    logger.info("断点续传恢复: 构型 %s 网格 → Error", config_id)

        # --- 求解 Computing 恢复 ---
        if sol_computing:
            logger.info("检测到 %d 个求解 Computing 状态的任务，尝试恢复...", len(sol_computing))
            for config_id in sol_computing:
                if remote.check_substage_done(config_id, SUBSTAGE_SOLVING):
                    state_manager.update_solving_status(config_id, Status.COMPLETED)
                    logger.info("断点续传恢复: 构型 %s 求解 → Completed", config_id)
                elif remote.check_substage_error(config_id, SUBSTAGE_SOLVING):
                    state_manager.update_solving_status(config_id, Status.ERROR, "上次运行出错")
                    logger.info("断点续传恢复: 构型 %s 求解 → Error", config_id)

    except Exception as e:
        logger.warning("断点续传检查异常: %s", e)
    finally:
        if remote is not None:
            try:
                remote.cleanup()
            except Exception:
                pass


def _run_poll_only(state_manager: StateManager):
    """纯轮询模式：仅检查远程任务状态"""
    logger.info("进入纯轮询模式...")
    executor = PipelineExecutor(state_manager, tui=None)

    try:
        remote = executor.connect_remote()
        if remote is None:
            logger.error("轮询模式: 无法连接到远程工作站，退出")
            return

        while not _shutdown_requested:
            executor.poll_all_remote()

            # 网格完成后启动求解
            for config in CONFIG_COMBINATIONS:
                st = state_manager.get_state(config.id)
                if st and st.get("meshing_status") == Status.DONE:
                    executor.launch_solving_if_ready(config, st)

            if executor.all_remote_done():
                logger.info("所有远程任务已完成，退出轮询模式")
                # 尝试自动启动尚未启动的求解
                for config in CONFIG_COMBINATIONS:
                    st = state_manager.get_state(config.id)
                    if st and st.get("solving_status") == Status.PENDING and st.get("meshing_status") == Status.DONE:
                        logger.info("自动启动求解: %s", config.id)
                        executor.launch_solving_if_ready(config, st)

                if executor.all_remote_done():
                    break

            time.sleep(REMOTE_CONFIG.get("poll_interval", 30))

    except KeyboardInterrupt:
        logger.info("轮询模式被用户中断")
    finally:
        executor.disconnect_remote()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    main()