# =============================================================================
# tui_display.py — 基于 Rich 的终端可视化监控界面（阶段5）
#
# 功能：
#   1. 使用 rich.live.Live 构建动态刷新控制台界面
#   2. 显示任务总览表格（各构型在多阶段的颜色状态）
#   3. 全局总进度条
#   4. 实时日志滚动区域
#   5. 与后台调度逻辑通过 queue.Queue 解耦，异步刷新
# =============================================================================
import queue
import threading
import time
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime

from rich.live import Live
from rich.table import Table
from rich.progress import (
    Progress,
    BarColumn,
    TextColumn,
    TaskProgressColumn,
    TimeElapsedColumn,
)
from rich.panel import Panel
from rich.layout import Layout
from rich.console import Console, Group
from rich.text import Text
from rich import box

from config import GLOBAL_CONFIG
from .state_manager import StateManager, Status

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 颜色映射（Rich 样式字符串）
# ---------------------------------------------------------------------------
STATUS_STYLES = {
    Status.PENDING:      "[dim grey54]⏳ 等待[/]",
    Status.IN_PROGRESS:  "[bold yellow]🔄 执行中[/]",
    Status.DONE:         "[bold green]✅ 完成[/]",
    Status.ERROR:        "[bold red]❌ 错误[/]",
    Status.TRANSFERRED:  "[bold cyan]📤 已传输[/]",
    Status.COMPUTING:    "[bold blue]⚡ 计算中[/]",
    Status.COMPLETED:    "[bold green]🎯 已完成[/]",
}

# 简化颜色映射（纯色块 + 状态文字，用于表格）
STATUS_COLORS = {
    Status.PENDING:      "dim",
    Status.IN_PROGRESS:  "yellow",
    Status.DONE:         "green",
    Status.ERROR:        "red",
    Status.TRANSFERRED:  "cyan",
    Status.COMPUTING:    "blue",
    Status.COMPLETED:    "green",
}


class TUIManager:
    """
    Rich TUI 管理器。

    使用 queue.Queue 接收来自工作线程的状态更新事件，
    通过 rich.live.Live 实现非阻塞的动态刷新界面。
    """

    def __init__(self, state_manager: StateManager):
        """
        初始化 TUI 管理器。

        Args:
            state_manager: 状态管理器实例（用于读取任务状态）
        """
        self.state_manager = state_manager
        self.console = Console()

        # 事件队列：工作线程通过此队列向 UI 线程推送消息
        self.event_queue: queue.Queue = queue.Queue()
        # 日志缓冲区（最近 N 条消息）
        self.log_buffer: List[str] = []
        self.max_log_lines = 6

        # 运行时统计
        self.start_time = time.time()
        self.last_event_time = time.time()

        # Live 刷新控制
        self.refresh_rate = GLOBAL_CONFIG.get("tui_refresh_rate", 4)
        self._live: Optional[Live] = None
        self._running = False

        # 阶段名称
        self.stages = ["SW建模", "SC处理", "文件传输", "Fluent求解"]

    # -----------------------------------------------------------------------
    # 事件推送（供外部线程调用）
    # -----------------------------------------------------------------------
    def push_event(self, event_type: str, config_id: str = "", message: str = ""):
        """
        向事件队列推送一条更新事件。

        Args:
            event_type: 事件类型 (status_update / log / error / progress)
            config_id: 关联的构型 ID
            message: 事件消息
        """
        try:
            self.event_queue.put_nowait({
                "type": event_type,
                "config_id": config_id,
                "message": message,
                "timestamp": datetime.now().strftime("%H:%M:%S"),
            })
        except queue.Full:
            pass  # 队列满了就丢弃（UI 刷新频率远高于事件产生频率）

    def push_log(self, config_id: str, stage: str, status: str):
        """推送一条日志（状态更新）"""
        self.push_event("status_update", config_id,
                        f"{config_id} [{stage}] → {status}")

    def push_error(self, config_id: str, msg: str):
        """推送一条错误"""
        self.push_event("error", config_id, f"ERROR: {config_id} - {msg}")

    # -----------------------------------------------------------------------
    # 日志缓冲区管理
    # -----------------------------------------------------------------------
    def _drain_events(self):
        """从事件队列中读取所有待处理事件并更新日志缓冲区"""
        while not self.event_queue.empty():
            try:
                event = self.event_queue.get_nowait()
                ts = event["timestamp"]
                msg = event["message"]
                line = f"[{ts}] {msg}"
                self.log_buffer.append(line)
                # 只保留最近 N 条
                if len(self.log_buffer) > self.max_log_lines:
                    self.log_buffer = self.log_buffer[-self.max_log_lines:]
                self.last_event_time = time.time()
            except queue.Empty:
                break

    # -----------------------------------------------------------------------
    # 构建表格：任务总览
    # -----------------------------------------------------------------------
    def _build_task_table(self) -> Table:
        """
        构建任务总览表格。

        Returns:
            Rich Table 对象
        """
        table = Table(
            title="🚀 仿真流水线任务总览",
            box=box.ROUNDED,
            highlight=True,
            title_style="bold white",
            header_style="bold cyan",
            border_style="blue",
        )

        table.add_column("构型 ID", style="bold white", width=20, no_wrap=True)
        table.add_column("SW建模", width=12, justify="center")
        table.add_column("SC处理", width=12, justify="center")
        table.add_column("文件传输", width=12, justify="center")
        table.add_column("Fluent求解", width=14, justify="center")
        table.add_column("重试", width=6, justify="center")

        # 获取所有状态
        all_states = self.state_manager.get_all_states()

        for st in all_states:
            cid = st["config_id"]
            sw_style = STATUS_COLORS.get(st["sw_status"], "dim")
            sc_style = STATUS_COLORS.get(st["sc_status"], "dim")
            tr_style = STATUS_COLORS.get(st["transfer_status"], "dim")
            fl_style = STATUS_COLORS.get(st["fluent_status"], "dim")

            # 如果 fluent 状态是 Computing 且远程已完成，自动更新
            if st["fluent_status"] == Status.COMPUTING:
                fl_display = f"[{fl_style}]⚡ 计算中[/]"
            elif st["fluent_status"] == Status.COMPLETED:
                fl_display = f"[{fl_style}]🎯 已完成[/]"
            elif st["fluent_status"] == Status.ERROR:
                fl_display = f"[{fl_style}]❌ 错误[/]"
            elif st["fluent_status"] == Status.TRANSFERRED:
                fl_display = f"[{fl_style}]📤 已传输[/]"
            elif st["fluent_status"] == Status.DONE:
                fl_display = f"[{fl_style}]✅ 完成[/]"
            elif st["fluent_status"] == Status.PENDING:
                fl_display = f"[{fl_style}]⏳ 等待[/]"
            else:
                fl_display = f"[{fl_style}]{st['fluent_status']}[/]"

            table.add_row(
                cid,
                f"[{sw_style}]{st['sw_status']}[/]",
                f"[{sc_style}]{st['sc_status']}[/]",
                f"[{tr_style}]{st['transfer_status']}[/]",
                fl_display,
                str(st["retry_count"]),
            )

        return table

    # -----------------------------------------------------------------------
    # 构建进度条与统计
    # -----------------------------------------------------------------------
    def _build_progress_section(self) -> Panel:
        """
        构建进度条 + 统计面板。

        Returns:
            Rich Panel
        """
        stats = self.state_manager.get_stats()
        total = stats["total"]
        completed = stats["completed"]
        in_progress = stats["in_progress"]
        error = stats["error"]
        pending = stats["pending"]

        # 计算百分比
        pct = (completed / total * 100) if total > 0 else 0

        # 进度文本
        progress_text = Text()
        progress_text.append("📊 批次进度: ", style="bold white")
        progress_text.append(f"{completed}/{total} 完成 ", style="bold green")
        progress_text.append(f"({pct:.1f}%)", style="bold cyan")
        progress_text.append(f" | 进行中: {in_progress}", style="yellow")
        progress_text.append(f" | 错误: {error}", style="red" if error > 0 else "dim")
        progress_text.append(f" | 等待: {pending}", style="dim")

        # Rich 进度条
        progress_bar = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=50, complete_style="green", finished_style="bright_green"),
            TaskProgressColumn(),
            TimeElapsedColumn(),
        )

        task = progress_bar.add_task("总体进度", total=total)
        progress_bar.update(task, completed=completed)

        # 运行时长
        elapsed = time.time() - self.start_time
        elapsed_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))
        progress_text.append(f"\n⏱ 运行时长: {elapsed_str}", style="dim")

        return Panel(
            Group(progress_text, progress_bar),
            title="📈 全局进度",
            border_style="green",
            padding=(1, 2),
        )

    # -----------------------------------------------------------------------
    # 构建日志面板
    # -----------------------------------------------------------------------
    def _build_log_panel(self) -> Panel:
        """
        构建实时日志面板。

        Returns:
            Rich Panel
        """
        if not self.log_buffer:
            log_content = Text("等待任务开始...", style="dim italic")
        else:
            log_content = Text("\n".join(self.log_buffer))

        return Panel(
            log_content,
            title="📜 实时日志",
            border_style="blue",
            padding=(1, 2),
            height=self.max_log_lines + 2,
        )

    # -----------------------------------------------------------------------
    # 构建完整布局
    # -----------------------------------------------------------------------
    def _build_layout(self) -> Layout:
        """
        构建完整的 Rich 布局。

        Returns:
            Rich Layout 对象
        """
        layout = Layout()

        # 顶部：标题
        layout.split(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3),
        )

        # 中间区域：表格 + 右侧面板（日志）
        layout["main"].split_row(
            Layout(name="table", ratio=3),
            Layout(name="right_panel", ratio=2),
        )

        # 右侧面板：进度条在上，日志在下
        layout["right_panel"].split(
            Layout(name="progress"),
            Layout(name="log"),
        )

        # 填充各区域
        layout["header"].update(
            Panel(
                Text("🔥 液氧甲烷火箭发动机仿真 — 自动化跑批流水线",
                     style="bold bright_yellow",
                     justify="center"),
                border_style="bright_yellow",
                padding=(0, 2),
            )
        )

        layout["table"].update(self._build_task_table())
        layout["progress"].update(self._build_progress_section())
        layout["log"].update(self._build_log_panel())

        # 底部状态栏
        stats = self.state_manager.get_stats()
        footer_text = Text()
        footer_text.append(f"总任务: {stats['total']}  ", style="dim")
        footer_text.append(f"已完成: {stats['completed']}  ", style="green")
        footer_text.append(f"进行中: {stats['in_progress']}  ", style="yellow")
        footer_text.append(f"错误: {stats['error']}  ", style="red")
        elapsed = time.time() - self.start_time
        footer_text.append(
            f"运行: {time.strftime('%H:%M:%S', time.gmtime(elapsed))}  ",
            style="dim",
        )
        footer_text.append(
            f"刷新: {datetime.now().strftime('%H:%M:%S')}",
            style="dim",
        )

        layout["footer"].update(
            Panel(footer_text, border_style="grey50", padding=(0, 2))
        )

        return layout

    # -----------------------------------------------------------------------
    # 主渲染循环
    # -----------------------------------------------------------------------
    def _render_loop(self) -> Layout:
        """渲染一帧（由 Live 自动调用）"""
        self._drain_events()
        return self._build_layout()

    def run(self):
        """启动 TUI 主循环（阻塞，在主线程中调用）"""
        self._running = True
        self._live = Live(
            self._render_loop(),
            console=self.console,
            refresh_per_second=self.refresh_rate,
            screen=True,
            transient=False,
        )

        self._live.start(refresh=True)

    def stop(self):
        """停止 TUI 刷新"""
        self._running = False
        if self._live:
            self._live.stop()

    def refresh(self):
        """手动触发一次刷新"""
        if self._live and self._running:
            try:
                self._live.update(self._render_loop())
            except Exception:
                pass

    def __enter__(self):
        """上下文管理器入口"""
        self.run()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.stop()