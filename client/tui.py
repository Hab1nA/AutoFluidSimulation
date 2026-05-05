"""
===============================================================================
TUI 客户端主界面 (Textual-based Terminal UI)
基于 Textual 框架构建的交互式终端界面。

界面布局：
┌─────────────────────────────────────────────────┐
│  🚀 液氧甲烷火箭发动机仿真总控程序 v1.0          │
│  引擎状态: Running | 屏障: 未通过 | 构型: 12     │
├─────────────────────────────────────────────────┤
│  构型  │  SW  │  SC  │ 传输 │ 网格 │ 求解       │
│  ──────┼──────┼──────┼──────┼──────┼──────      │
│    1   │  ✓   │  ✓   │  ✓   │  ⏳  │  ⏸       │
│    2   │  ✓   │  ⏳   │  ⏸   │  ⏸   │  ⏸       │
│   ...  │ ...  │ ...  │ ...  │ ...  │ ...        │
├─────────────────────────────────────────────────┤
│  > _                                             │
│  [start] [pause] [check] [reset] [clean] [quit] │
└─────────────────────────────────────────────────┘

状态图标：
  ✓ = Completed (绿色)
  ⏳ = Running (黄色闪烁)
  ⟳ = Retrying (橙色)
  ⏸ = Waiting (灰色)
  ✗ = Error (红色)
===============================================================================
"""
import sys
import os
import asyncio
import threading
import time
from typing import Dict, Optional

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Header, Footer, Static, Button, Input, DataTable,
    Label, RichLog,
)
from textual.binding import Binding
from textual.screen import ModalScreen, Screen
from textual.reactive import reactive
from textual.message import Message
from textual import events

from engine.config import (
    STEP_NAMES, STEP_DISPLAY,
    STATUS_WAITING, STATUS_RUNNING, STATUS_RETRYING, STATUS_COMPLETED, STATUS_ERROR,
)
from client.ipc_client import IPCClient

# ============================================================================
# 状态显示映射
# ============================================================================

STATUS_ICONS = {
    STATUS_WAITING:   "⏸",   # 等待
    STATUS_RUNNING:   "⏳",   # 运行中
    STATUS_RETRYING:  "⟳",   # 重试中
    STATUS_COMPLETED: "✓",   # 已完成
    STATUS_ERROR:     "✗",   # 出错
}

STATUS_COLORS = {
    STATUS_WAITING:   "dim",
    STATUS_RUNNING:   "yellow",
    STATUS_RETRYING:  "orange1",
    STATUS_COMPLETED: "green",
    STATUS_ERROR:     "red",
}

ENGINE_STATUS_DISPLAY = {
    "stopped": "已停止",
    "running": "运行中",
    "paused":  "已暂停",
}


# ============================================================================
# 确认对话框
# ============================================================================

class ConfirmDialog(ModalScreen):
    """确认对话框（用于危险操作）。"""

    BINDINGS = [
        Binding("y", "confirm", "确认"),
        Binding("n", "cancel", "取消"),
        Binding("escape", "cancel", "取消"),
    ]

    def __init__(self, message: str, callback=None):
        super().__init__()
        self.message_text = message
        self.callback = callback

    def compose(self) -> ComposeResult:
        yield Container(
            Static(f"⚠️  {self.message_text}", id="confirm-message"),
            Static("按 [Y] 确认  |  按 [N] 取消", id="confirm-hint"),
            id="confirm-dialog",
        )

    def action_confirm(self):
        if self.callback:
            self.callback()
        self.dismiss()

    def action_cancel(self):
        self.dismiss()


# ============================================================================
# 自检结果显示屏幕
# ============================================================================

class CheckResultScreen(ModalScreen):
    """系统自检结果展示屏幕。"""

    BINDINGS = [
        Binding("escape", "dismiss", "关闭"),
        Binding("q", "dismiss", "关闭"),
    ]

    def __init__(self, check_data: dict):
        super().__init__()
        self.check_data = check_data

    def compose(self) -> ComposeResult:
        results = self.check_data.get("local_checks", {})
        remote = self.check_data.get("remote_checks", {})

        lines = ["[bold]系统自检结果[/bold]\n"]
        lines.append("[bold]━━ 本地检查 ━━[/bold]")
        for name, info in results.items():
            icon = "✓" if info.get("exists") else "✗"
            color = "green" if info.get("exists") else "red"
            path = info.get("path", "")
            lines.append(f"  [{color}]{icon} {name}[/{color}]: {path}")

        lines.append("\n[bold]━━ 远程检查 ━━[/bold]")
        for key, val in remote.items():
            if key == "background_processes":
                lines.append(f"  后台进程: {val}")
            else:
                lines.append(f"  {key}: {val}")

        yield Container(
            Static("\n".join(lines), id="check-content"),
            Static("\n按 [Q] 或 [Esc] 关闭", id="check-hint"),
            id="check-dialog",
        )

    def action_dismiss(self):
        self.dismiss()


# ============================================================================
# 主 TUI 应用
# ============================================================================

class PipelineTUI(App):
    """
    流水线总控 TUI 应用程序。

    使用 Textual 框架构建交互式终端界面。
    """

    CSS = """
    Screen {
        background: #1a1a2e;
    }

    #header-bar {
        dock: top;
        height: auto;
        background: #16213e;
        padding: 1 2;
        border-bottom: solid #0f3460;
    }

    #title {
        text-style: bold;
        color: #e94560;
        content-align: center;
        width: 100%;
        text-align: center;
    }

    #info-bar {
        height: 1;
        background: #0f3460;
        padding: 0 2;
        color: #a0a0a0;
    }

    #status-table {
        height: 1fr;
        margin: 1 2;
        background: #1a1a2e;
        border: solid #333;
    }

    DataTable {
        background: #1a1a2e;
        color: #e0e0e0;
    }

    DataTable > .datatable--header {
        background: #16213e;
        color: #e94560;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #0f3460;
    }

    #log-panel {
        height: 10;
        margin: 0 2;
        border: solid #333;
        background: #0d0d0d;
    }

    RichLog {
        background: #0d0d0d;
        color: #00ff88;
    }

    #command-area {
        dock: bottom;
        height: auto;
        background: #16213e;
        padding: 1 2;
        border-top: solid #0f3460;
    }

    #cmd-input {
        width: 100%;
        background: #0d0d0d;
        color: #00ff88;
        border: solid #0f3460;
    }

    #cmd-input:focus {
        border: solid #e94560;
    }

    #quick-buttons {
        height: 1;
        margin-top: 1;
    }

    Button {
        margin: 0 1;
        min-width: 14;
        background: #0f3460;
        color: #e0e0e0;
        border: none;
    }

    Button:hover {
        background: #e94560;
        color: white;
    }

    #confirm-dialog {
        width: 60;
        height: auto;
        margin: 5 10;
        padding: 2 3;
        background: #16213e;
        border: thick #e94560;
    }

    #confirm-message {
        color: #ffaa00;
        text-style: bold;
        text-align: center;
        padding-bottom: 1;
    }

    #confirm-hint {
        color: #888;
        text-align: center;
    }

    #check-dialog {
        width: 70;
        height: auto;
        margin: 2 5;
        padding: 1 2;
        background: #16213e;
        border: thick #0f3460;
    }

    #check-content {
        color: #e0e0e0;
        padding-bottom: 1;
    }

    #check-hint {
        color: #888;
        text-align: center;
    }

    .status-waiting { color: #666; }
    .status-running { color: #ffcc00; text-style: bold; }
    .status-retrying { color: #ff8800; }
    .status-completed { color: #00cc66; }
    .status-error { color: #ff4444; text-style: bold; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit_app", "退出", show=False),
        Binding("ctrl+q", "quit_app", "退出", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.ipc = IPCClient()
        self._refresh_timer: Optional[asyncio.Task] = None
        self._status_data: Dict[int, Dict[str, str]] = {}
        self._configs: list[int] = []

    # ------------------------------------------------------------------
    # 界面布局
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """构建界面组件。"""
        # 顶部标题栏
        yield Container(
            Static("🚀 液氧甲烷火箭发动机仿真总控程序 v1.0", id="title"),
            Static("引擎: 未连接  |  构型数: 0  |  屏障: --", id="info-bar"),
            id="header-bar",
        )

        # 主状态表格
        yield DataTable(id="status-table", cursor_type="row")

        # 日志面板
        yield RichLog(id="log-panel", highlight=True, markup=True, max_lines=50)

        # 底部命令区域
        yield Container(
            Input(placeholder="输入命令 (help 查看帮助)...", id="cmd-input"),
            Container(
                Button("▶ Start", id="btn-start", variant="success"),
                Button("⏸ Pause", id="btn-pause", variant="warning"),
                Button("🔍 Check", id="btn-check", variant="primary"),
                Button("🔄 Reset", id="btn-reset", variant="default"),
                Button("🧹 Clean", id="btn-clean", variant="default"),
                Button("🚪 Quit", id="btn-quit", variant="error"),
                Button("⏹ FullQuit", id="btn-fullquit", variant="error"),
                id="quick-buttons",
            ),
            id="command-area",
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        """界面挂载完成后初始化。"""
        self._init_table()
        self._log("欢迎使用仿真流水线总控程序！")
        self._log("正在连接后台引擎...")

        # 尝试连接 Daemon
        if self.ipc.connect():
            self._log("[green]✓ 已连接到后台引擎[/green]")
            self._update_info_bar()
            # 启动定时刷新
            self._refresh_timer = self.set_interval(1.0, self._refresh_status)
        else:
            self._log("[red]✗ 无法连接到后台引擎，请先启动 start_daemon.py[/red]")
            self._log("[yellow]提示: 界面将在无后台连接的情况下运行，部分功能不可用[/yellow]")

        # 设置焦点到命令输入
        self.query_one("#cmd-input", Input).focus()

    def on_unmount(self) -> None:
        """界面卸载时清理。"""
        if self._refresh_timer:
            self._refresh_timer.cancel()
        self.ipc.disconnect()

    # ------------------------------------------------------------------
    # 表格初始化
    # ------------------------------------------------------------------

    def _init_table(self):
        """初始化状态表格的列。"""
        table = self.query_one("#status-table", DataTable)
        table.add_column("构型", width=8)
        for step in STEP_NAMES:
            display = STEP_DISPLAY.get(step, step)
            table.add_column(f"{display[:4]}", width=8)
        table.show_header = True
        table.cursor_type = "row"

    # ------------------------------------------------------------------
    # 状态刷新
    # ------------------------------------------------------------------

    async def _refresh_status(self) -> None:
        """定时从 Daemon 刷新状态数据并更新表格。"""
        if not self.ipc.is_connected():
            return

        ok, data, msg = self.ipc.get_all_status()
        if not ok:
            return

        self._status_data = data or {}
        self._update_table()
        self._update_info_bar()

    def _update_table(self):
        """根据最新状态数据更新 DataTable。"""
        table = self.query_one("#status-table", DataTable)

        if not self._status_data:
            return

        # 获取排序后的构型列表
        configs = sorted(self._status_data.keys())
        self._configs = configs

        # 清除旧行
        table.clear()

        for cn in configs:
            steps = self._status_data[cn]
            row = [str(cn)]
            for step_name in STEP_NAMES:
                status = steps.get(step_name, STATUS_WAITING)
                icon = STATUS_ICONS.get(status, "?")
                row.append(f"{icon} {status}")
            table.add_row(*row)

    def _update_info_bar(self):
        """更新顶部信息栏。"""
        info_bar = self.query_one("#info-bar", Static)

        if self.ipc.is_connected():
            ok, data, msg = self.ipc.get_engine_status()
            if ok and data:
                engine_status = ENGINE_STATUS_DISPLAY.get(
                    data.get("engine_status", "stopped"), "未知"
                )
                barrier = "已通过" if data.get("barrier_passed") else "未通过"
                info_bar.update(
                    f"引擎: {engine_status}  |  "
                    f"构型数: {len(self._configs)}  |  "
                    f"屏障: {barrier}"
                )
            else:
                info_bar.update("引擎: 已连接  |  等待数据...")
        else:
            info_bar.update("引擎: 未连接  |  请先启动 Daemon")

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    def _log(self, message: str):
        """向日志面板添加一条消息。"""
        try:
            log = self.query_one("#log-panel", RichLog)
            log.write(message)
        except Exception:
            pass  # 界面可能还未初始化

    # ------------------------------------------------------------------
    # 按钮事件处理
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """处理按钮点击事件。"""
        btn_id = event.button.id

        if btn_id == "btn-start":
            self._do_start()
        elif btn_id == "btn-pause":
            self._do_pause()
        elif btn_id == "btn-check":
            self._do_check()
        elif btn_id == "btn-reset":
            self._show_reset_prompt()
        elif btn_id == "btn-clean":
            self._show_clean_prompt()
        elif btn_id == "btn-quit":
            self._do_quit()
        elif btn_id == "btn-fullquit":
            self._do_full_quit()

    # ------------------------------------------------------------------
    # 命令输入处理
    # ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """处理命令输入。"""
        cmd_line = event.value.strip()
        event.input.value = ""  # 清空输入框

        if not cmd_line:
            return

        self._log(f"[dim]> {cmd_line}[/dim]")
        parts = cmd_line.split()
        cmd = parts[0].lower()

        # 路由命令
        if cmd == "help":
            self._show_help()
        elif cmd in ("start", "continue"):
            self._do_start()
        elif cmd == "pause":
            self._do_pause()
        elif cmd == "check":
            self._do_check()
        elif cmd == "reset":
            self._handle_reset_cmd(parts[1:])
        elif cmd == "clean":
            self._handle_clean_cmd(parts[1:])
        elif cmd == "quit":
            self._do_quit()
        elif cmd == "full_quit":
            self._do_full_quit()
        elif cmd == "status":
            self._do_status()
        else:
            self._log(f"[red]未知命令: {cmd}，输入 help 查看帮助[/red]")

    # ------------------------------------------------------------------
    # 命令实现
    # ------------------------------------------------------------------

    def _do_start(self):
        """执行 start 命令。"""
        if not self._check_connection():
            return
        ok, msg = self.ipc.start_pipeline()
        if ok:
            self._log(f"[green]✓ {msg}[/green]")
        else:
            self._log(f"[red]✗ {msg}[/red]")

    def _do_pause(self):
        """执行 pause 命令。"""
        if not self._check_connection():
            return
        ok, msg = self.ipc.pause_pipeline()
        if ok:
            self._log(f"[yellow]⏸ {msg}[/yellow]")
        else:
            self._log(f"[red]✗ {msg}[/red]")

    def _do_check(self):
        """执行 check 命令。"""
        if not self._check_connection():
            return
        ok, data, msg = self.ipc.check_system()
        if ok and data:
            self._log("[green]✓ 系统自检完成[/green]")
            self.push_screen(CheckResultScreen(data))
        else:
            self._log(f"[red]✗ 系统自检失败: {msg}[/red]")

    def _do_status(self):
        """显示当前状态摘要。"""
        if not self._check_connection():
            return
        ok, data, msg = self.ipc.get_statistics()
        if ok and data:
            self._log(f"引擎状态: {data.get('engine_status', '?')}")
            self._log(f"总构型数: {data.get('total_configs', 0)}")
            for step, counts in data.get("steps", {}).items():
                self._log(f"  {step}: {counts}")
        else:
            self._log(f"[red]✗ {msg}[/red]")

    def _handle_reset_cmd(self, args: list):
        """处理 reset 命令。"""
        if not args:
            self._log("[yellow]用法: reset <构型名> [步骤名]  或 reset all[/yellow]")
            return

        if args[0].lower() == "all":
            self.push_screen(
                ConfirmDialog(
                    "确定要重置【所有构型】的所有步骤吗？此操作不可逆！",
                    callback=self._do_reset_all
                )
            )
        else:
            try:
                config_name = int(args[0])
                step_name = args[1] if len(args) > 1 else None
                if step_name and step_name not in STEP_NAMES:
                    self._log(f"[red]无效步骤名: {step_name}，有效值: {STEP_NAMES}[/red]")
                    return
                self.push_screen(
                    ConfirmDialog(
                        f"确定要重置构型{config_name}的 "
                        f"{step_name + '及后续步骤' if step_name else '所有步骤'} 吗？",
                        callback=lambda: self._do_reset_step(config_name, step_name)
                    )
                )
            except ValueError:
                self._log("[red]构型名称必须是整数[/red]")

    def _do_reset_step(self, config_name: int, step_name: str = None):
        """执行重置步骤操作。"""
        if not self._check_connection():
            return
        ok, msg = self.ipc.reset_step(config_name, step_name)
        if ok:
            self._log(f"[green]✓ {msg}[/green]")
        else:
            self._log(f"[red]✗ {msg}[/red]")

    def _do_reset_all(self):
        """执行重置全部操作。"""
        if not self._check_connection():
            return
        ok, msg = self.ipc.reset_all()
        if ok:
            self._log(f"[yellow]⚠ {msg}[/yellow]")
        else:
            self._log(f"[red]✗ {msg}[/red]")

    def _handle_clean_cmd(self, args: list):
        """处理 clean 命令。"""
        if not args:
            self._log("[yellow]用法: clean <步骤名> [构型名]  或 clean all[/yellow]")
            return

        if args[0].lower() == "all":
            self.push_screen(
                ConfirmDialog(
                    "确定要清理【所有步骤】产生的文件吗？此操作不可逆！",
                    callback=self._do_clean_all
                )
            )
        else:
            step_name = args[0]
            if step_name not in STEP_NAMES:
                self._log(f"[red]无效步骤名: {step_name}，有效值: {STEP_NAMES}[/red]")
                return
            config_name = int(args[1]) if len(args) > 1 else None
            self._do_clean_step(step_name, config_name)

    def _do_clean_step(self, step_name: str, config_name: int = None):
        """执行清理步骤操作。"""
        if not self._check_connection():
            return
        ok, msg = self.ipc.clean_step(step_name, config_name)
        if ok:
            self._log(f"[green]✓ {msg}[/green]")
        else:
            self._log(f"[red]✗ {msg}[/red]")

    def _do_clean_all(self):
        """执行清理全部操作。"""
        if not self._check_connection():
            return
        ok, msg = self.ipc.clean_all()
        if ok:
            self._log(f"[yellow]⚠ {msg}[/yellow]")
        else:
            self._log(f"[red]✗ {msg}[/red]")

    def _do_quit(self):
        """执行 quit 命令（仅退出 TUI，后台继续运行）。"""
        self._log("[yellow]⚠ 界面已退出，后台引擎仍在运行[/yellow]")
        self._log("[yellow]  使用 start_client.py 可重新连接界面[/yellow]")
        # 延迟退出以显示消息
        def delayed_exit():
            time.sleep(1)
            self.exit()
        threading.Thread(target=delayed_exit, daemon=True).start()

    def _do_full_quit(self):
        """执行 full_quit 命令。"""
        self.push_screen(
            ConfirmDialog(
                "确定要【完全退出】后台引擎和界面吗？\n所有正在运行的任务将被中止！",
                callback=self._execute_full_quit
            )
        )

    def _execute_full_quit(self):
        """执行完全退出。"""
        if self.ipc.is_connected():
            ok, msg = self.ipc.full_quit()
            if ok:
                self._log(f"[red]⏹ {msg}[/red]")
            else:
                self._log(f"[red]✗ {msg}[/red]")
        self.ipc.disconnect()
        # 延迟退出
        def delayed_exit():
            time.sleep(1.5)
            self.exit()
        threading.Thread(target=delayed_exit, daemon=True).start()

    def _show_reset_prompt(self):
        """提示用户输入 reset 参数。"""
        self._log("[yellow]请在命令输入行使用: reset <构型名> [步骤名]  或 reset all[/yellow]")
        self.query_one("#cmd-input", Input).focus()

    def _show_clean_prompt(self):
        """提示用户输入 clean 参数。"""
        self._log("[yellow]请在命令输入行使用: clean <步骤名> [构型名]  或 clean all[/yellow]")
        self.query_one("#cmd-input", Input).focus()

    def _show_help(self):
        """显示帮助信息。"""
        help_text = """
[bold]可用命令:[/bold]
  [green]start[/green] / [green]continue[/green]  - 启动或继续流水线
  [yellow]pause[/yellow]                  - 暂停流水线
  [blue]check[/blue]                   - 系统自检
  [cyan]reset <XX> <step>[/cyan]       - 重置指定构型的指定步骤
  [cyan]reset all[/cyan]               - 重置所有构型（警告！）
  [magenta]clean <step>[/magenta]            - 清理指定步骤文件
  [magenta]clean all[/magenta]              - 清理所有文件（警告！）
  [dim]quit[/dim]                    - 退出界面（后台继续运行）
  [red]full_quit[/red]               - 完全退出（停止后台引擎）
  [dim]status[/dim]                  - 显示状态摘要
  [dim]help[/dim]                    - 显示此帮助
        """
        self._log(help_text)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _check_connection(self) -> bool:
        """检查 Daemon 连接，未连接时尝试重连。"""
        if not self.ipc.is_connected():
            self._log("[yellow]未连接到后台引擎，尝试重新连接...[/yellow]")
            if self.ipc.connect():
                self._log("[green]✓ 已重新连接[/green]")
                if not self._refresh_timer:
                    self._refresh_timer = self.set_interval(1.0, self._refresh_status)
                return True
            else:
                self._log("[red]✗ 连接失败，请确保 Daemon 正在运行[/red]")
                return False
        return True

    def action_quit_app(self):
        """快捷键退出。"""
        self._do_quit()


# ============================================================================
# 主入口
# ============================================================================

def main():
    """启动 TUI 客户端。"""
    app = PipelineTUI()
    app.run()


if __name__ == "__main__":
    main()
