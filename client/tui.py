"""
===============================================================================
TUI 客户端主界面 (Textual-based Terminal UI)
基于 Textual 框架构建的交互式终端界面。

界面布局：
┌──────────────────────────────────────────────────┐
│   🚀 液氧甲烷火箭发动机仿真总控程序 v1.0          │
│  引擎状态: Running | 屏障: 未通过 | 构型: 12      │
├──────────────────────────────────────────────────┤
│  构型  │  SW  │  SC  │ 传输 │ 网格 │ 求解        │
│  ──────┼──────┼──────┼──────┼──────┼──────       │
│    1   │  ✓   │  ✓   │  ✓   │  ⏳   │  ⏳        │
│    2   │  ✓   │  ⏳   │  ⏳   │  ⏳   │  ⏳        │
│   ...  │ ...  │ ...  │ ...  │ ...  │ ...         │
├──────────────────────────────────────────────────┤
│  > _                                              │
│  [start] [pause] [check] [reset] [clean] [quit]  │
└──────────────────────────────────────────────────┘

状态图标：
  ✓ = Completed (绿色)
  ⏳ = Running (黄色闪烁)
  🔄 = Retrying (橙色)
  ⏸ = Waiting (灰色)
  ✗ = Error (红色)
===============================================================================
"""
import sys
import os
import asyncio
import subprocess
import threading
from typing import Dict

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from textual.app import App, ComposeResult
from textual.containers import Container, Vertical
from textual.widgets import (
    Static, Button, Input, DataTable,
    RichLog,
)
from textual.binding import Binding
from textual.screen import ModalScreen

from engine.config import (
    STEP_NAMES, STEP_DISPLAY,
    STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED, STATUS_RETRYING, STATUS_COMPLETED, STATUS_ERROR,
)
from client.ipc_client import IPCClient

# ============================================================================
# 状态显示映射
# ============================================================================

STATUS_ICONS = {
    STATUS_WAITING:   "⏸",   # 等待
    STATUS_RUNNING:   "⏳",   # 运行中
    STATUS_PAUSED:    "⏸",   # 已暂停（用户手动暂停）
    STATUS_RETRYING:  "🔄",   # 重试中
    STATUS_COMPLETED: "✓",   # 已完成
    STATUS_ERROR:     "✗",   # 出错
}

STATUS_COLORS = {
    STATUS_WAITING:   "dim",
    STATUS_RUNNING:   "yellow",
    STATUS_PAUSED:    "grey",
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
    """确认对话框（用于危险操作），支持多行详细警告信息。"""

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
        # 将消息按行拆分，独立渲染以保证多行显示
        lines = self.message_text.split("\n")
        message_widgets = []
        for line in lines:
            if line.strip():
                message_widgets.append(Static(line.strip(), classes="confirm-line"))
            else:
                message_widgets.append(Static(" ", classes="confirm-line"))

        yield Container(
            Vertical(*message_widgets, id="confirm-message-body"),
            Static("按 [[Y]] 确认  |  按 [[N]] 取消", id="confirm-hint"),
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

        # 远程检查项的中文显示名映射
        REMOTE_KEY_LABELS = {
            "ssh_connected": "SSH 连接",
            "ssh": "SSH 状态",
            "conda_available": "Conda 可用",
            "python_version": "Python 版本",
            "disk_space": "磁盘空间 (D:)",
            "background_processes": "后台进程",
        }

        lines = ["[bold]系统自检结果[/bold]\n"]
        lines.append("[bold]─── 本地检查 ───[/bold]")
        for name, info in results.items():
            icon = "✓" if info.get("exists") else "✗"
            color = "green" if info.get("exists") else "red"
            path = info.get("path", "")
            lines.append(f"  [{color}]{icon} {name}[/{color}]: {path}")

        lines.append("\n[bold]─── 远程检查 ───[/bold]")
        for key, val in remote.items():
            label = REMOTE_KEY_LABELS.get(key, key)
            if key == "background_processes":
                if isinstance(val, list):
                    proc_display = ", ".join(val) if val else "无"
                else:
                    proc_display = str(val)
                lines.append(f"  {label}: {proc_display}")
            else:
                lines.append(f"  {label}: {val}")

        yield Container(
            Static("\n".join(lines), id="check-content"),
            Static("\n按 [[Q]] 或 [[Esc]] 关闭", id="check-hint"),
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
        content-align: center middle;
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
        height: auto;
        margin-top: 1;
        layout: horizontal;
        overflow-x: auto;
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
        width: 66;
        height: auto;
        max-height: 30;
        margin: 3 8;
        padding: 2 3;
        background: #16213e;
        border: thick #e94560;
    }

    #confirm-message-body {
        width: 100%;
        height: auto;
        padding-bottom: 1;
    }

    .confirm-line {
        color: #ffaa00;
        text-style: bold;
        text-align: center;
        width: 100%;
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
    .status-paused { color: #888; }
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
        self._refresh_timer = None  # Textual Timer object
        self._status_data: Dict[int, Dict[str, str]] = {}
        self._configs: list[int] = []
        self._engine_info: dict = {}
        self._refresh_counter: int = 0
        self._data_lock = threading.Lock()  # 保护共享状态数据
        self._daemon_process: subprocess.Popen = None  # 由 TUI 启动的 daemon 子进程句柄
        self._column_keys: Dict[str, str] = {}  # STEP_NAMES → DataTable column key 映射
        self._table_row_keys: set[str] = set()  # 当前表格中已存在的行键（自维护，兼容 Textual 8.x）

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
        yield RichLog(id="log-panel", highlight=False, markup=True, max_lines=50)

        # 底部命令区域
        yield Container(
            Input(placeholder="输入命令 (help 查看帮助)...", id="cmd-input"),
            Container(
                Button("▶ Start", id="btn-start", variant="success"),
                Button("⏸ Pause", id="btn-pause", variant="warning"),
                Button("🔧 Check", id="btn-check", variant="primary"),
                Button("📊 Status", id="btn-status", variant="primary"),
                Button("🔧 Daemon Start", id="btn-daemon", variant="primary"),
                Button("⏸ Daemon Stop", id="btn-dstop", variant="warning"),
                Button("🚪 Quit", id="btn-quit", variant="error"),
                Button("⏹ Quit Full", id="btn-fullquit", variant="error"),
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
            self._refresh_timer.stop()
        self.ipc.disconnect()
        # 不终止 daemon 子进程——quit 时 daemon 继续运行

    # ------------------------------------------------------------------
    # 表格初始化
    # ------------------------------------------------------------------

    def _init_table(self):
        """初始化状态表格的列，同时记录列 key 映射供增量更新使用。

        Textual 8.x 中 add_column 若不指定 key 参数会自动生成唯一 ID；
        必须显式传入 key=display 以确保后续 update_cell 可以通过字符串匹配。
        """
        table = self.query_one("#status-table", DataTable)
        table.add_column("构型", width=6, key="构型")
        for step in STEP_NAMES:
            display = STEP_DISPLAY.get(step, step)
            table.add_column(display, width=16, key=display)
            self._column_keys[step] = display  # display 同时作为列 key
        table.show_header = True
        table.cursor_type = "row"

    # ------------------------------------------------------------------
    # 状态刷新
    # ------------------------------------------------------------------

    async def _refresh_status(self) -> None:
        """定时从 Daemon 刷新状态数据并更新表格（异步，不阻塞事件循环）。"""
        if not self.ipc.is_connected():
            return

        # 将阻塞 IPC 调用放到线程池，避免卡住 Textual 事件循环
        ok, data, msg = await asyncio.to_thread(self.ipc.get_all_status)
        if not ok:
            return

        # 在锁内完成所有数据快照复制，然后释放锁再进行 UI 更新
        with self._data_lock:
            self._status_data = data or {}
            # 引擎状态变化较慢，每5次刷新（5秒）更新一次
            self._refresh_counter += 1
            should_refresh_engine = self._refresh_counter >= 5
            if should_refresh_engine:
                self._refresh_counter = 0
            # 快照引擎信息以避免在锁外访问
            eng_snapshot = dict(self._engine_info) if self._engine_info else {}

        self._update_table()

        if should_refresh_engine:
            ok2, eng_data, _ = await asyncio.to_thread(self.ipc.get_engine_status)
            if ok2 and eng_data:
                with self._data_lock:
                    self._engine_info = eng_data
                    eng_snapshot = dict(eng_data)
        self._update_info_bar(eng_snapshot)

    def _update_table(self):
        """根据最新状态数据增量更新 DataTable（保留滚动位置）。

        与旧版实现不同，此方法不再使用 table.clear() 全量重建，
        而是通过 add_row / remove_row / update_cell 进行增量操作，
        从而避免每次刷新都将滚动条重置到顶端。

        使用自维护的 _table_row_keys 集合追踪行键，
        兼容 Textual 8.x 中 ordered_rows 返回不可哈希 Row 对象的问题。
        """
        table = self.query_one("#status-table", DataTable)

        with self._data_lock:
            if not self._status_data:
                return
            # 获取排序后的构型列表（按数值排序，避免字符串排序导致的 1→10→2 问题）
            # 防御：仅保留纯数字 key，过滤掉因 IPC 数据错乱混入的非构型字段（如 engine_status）
            configs = sorted(
                [k for k in self._status_data.keys() if isinstance(k, str) and k.isdigit()],
                key=lambda x: int(x)
            )
            self._configs = configs
            # 复制数据以避免在锁外迭代
            status_data = dict(self._status_data)

        # 新数据中的行键集合
        new_keys = {str(cn) for cn in configs}

        # 1) 移除已不存在的构型行
        removed_keys = self._table_row_keys - new_keys
        for key in removed_keys:
            try:
                table.remove_row(key)
            except KeyError:
                pass  # 行可能已被移除（竞态窗口），忽略
        self._table_row_keys -= removed_keys

        # 2) 添加新的构型行
        added_keys = new_keys - self._table_row_keys
        for cn in configs:
            key = str(cn)
            if key in added_keys:
                steps = status_data.get(cn)
                if steps is None:
                    continue  # 并发场景下该构型数据已消失
                row = [key]
                for step_name in STEP_NAMES:
                    status = steps.get(step_name, STATUS_WAITING)
                    icon = STATUS_ICONS.get(status, "?")
                    row.append(f"{icon} {status}")
                table.add_row(*row, key=key)
                self._table_row_keys.add(key)

        # 3) 更新已有行的单元格（仅更新变化的列）
        for cn in configs:
            key = str(cn)
            if key not in self._table_row_keys or key in added_keys:
                continue  # 新行已在步骤 2 中创建，无需再更新
            steps = status_data.get(cn)
            if steps is None:
                continue  # 并发场景下该构型数据已消失
            for step_name in STEP_NAMES:
                status = steps.get(step_name, STATUS_WAITING)
                icon = STATUS_ICONS.get(status, "?")
                new_value = f"{icon} {status}"
                col_key = self._column_keys.get(step_name)
                if col_key is None:
                    continue
                # 仅在值变化时更新，减少不必要的重绘
                try:
                    old_value = table.get_cell(key, col_key)
                except Exception:
                    old_value = None
                if old_value != new_value:
                    table.update_cell(key, col_key, new_value)

    def _update_info_bar(self, eng_snapshot: dict = None):
        """更新顶部信息栏。可选择性传入引擎状态快照以避免锁争用。"""
        info_bar = self.query_one("#info-bar", Static)

        if self.ipc.is_connected():
            if eng_snapshot:
                engine_status = ENGINE_STATUS_DISPLAY.get(
                    eng_snapshot.get("engine_status", "stopped"), "未知"
                )
                barrier = "已通过" if eng_snapshot.get("barrier_passed") else "未通过"
            else:
                with self._data_lock:
                    if self._engine_info:
                        engine_status = ENGINE_STATUS_DISPLAY.get(
                            self._engine_info.get("engine_status", "stopped"), "未知"
                        )
                        barrier = "已通过" if self._engine_info.get("barrier_passed") else "未通过"
                    else:
                        info_bar.update("引擎: 已连接  |  等待数据...")
                        return
            info_bar.update(
                f"引擎: {engine_status}  |  "
                f"构型数: {len(self._configs)}  |  "
                f"屏障: {barrier}"
            )
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
            # 界面可能还未初始化，降级输出到 stderr
            print(f"[TUI] {message}", file=sys.stderr)

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
        elif btn_id == "btn-status":
            self._do_status()
        elif btn_id == "btn-quit":
            self._do_quit()
        elif btn_id == "btn-daemon":
            self._do_launch_daemon()
        elif btn_id == "btn-dstop":
            self._do_stop_daemon()
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
        elif cmd == "start":
            self._do_start()
        elif cmd == "pause":
            self._do_pause()
        elif cmd == "check":
            self._do_check()
        elif cmd == "reset":
            self._handle_reset_cmd(parts[1:])
        elif cmd == "clean":
            self._handle_clean_cmd(parts[1:])
        elif cmd == "quit" and len(parts) > 1 and parts[1].lower() == "full":
            self._do_full_quit()
        elif cmd == "quit":
            self._do_quit()
        elif cmd == "daemon":
            self._handle_daemon_cmd(parts[1:])
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
        """处理 reset 命令。用法: reset <构型名|all> <步骤名|all>"""
        if len(args) < 2:
            self._log("[yellow]用法: reset <构型名|all> <步骤名|all>[/yellow]")
            return

        # 解析构型名（支持整数或 "all"）
        config_name: int | str
        if args[0].lower() == "all":
            config_name = "all"
        else:
            try:
                config_name = int(args[0])
            except ValueError:
                self._log(f"[red]构型名称必须是整数或 \"all\"，收到: {args[0]}[/red]")
                return

        # 解析步骤名（支持步骤名或 "all"，必须提供）
        raw_step = args[1]
        if raw_step.lower() == "all":
            step_name = "all"
        elif raw_step in STEP_NAMES:
            step_name = raw_step
        else:
            self._log(f"[red]无效步骤名: {args[1]}，有效值: {STEP_NAMES} 或 all[/red]")
            return

        # 构建确认对话框文本
        cfg_desc = "所有构型" if config_name == "all" else f"构型{config_name}"
        if step_name == "all":
            step_desc = "所有步骤"
        else:
            step_desc = f"{step_name} 及后续步骤"

        self.push_screen(
            ConfirmDialog(
                f"确定要重置{cfg_desc}的{step_desc}吗？此操作不可逆！",
                callback=lambda: self._do_reset_step(config_name, step_name)
            )
        )

    def _do_reset_step(self, config_name, step_name: str = None):
        """执行重置步骤操作。config_name 支持 int 或 'all'，step_name 支持 str 或 'all'。"""
        if not self._check_connection():
            return
        ok, msg = self.ipc.reset_step(config_name, step_name)
        if ok:
            self._log(f"[green]✓ {msg}[/green]")
        else:
            self._log(f"[red]✗ {msg}[/red]")

    def _handle_clean_cmd(self, args: list):
        """处理 clean 命令。用法: clean <构型名|all> <步骤名|all>"""
        if len(args) < 2:
            self._log("[yellow]用法: clean <构型名|all> <步骤名|all>[/yellow]")
            return

        # 解析构型名（支持整数或 "all"）
        config_name: int | str
        if args[0].lower() == "all":
            config_name = "all"
        else:
            try:
                config_name = int(args[0])
            except ValueError:
                self._log(f"[red]构型名称必须是整数或 \"all\"，收到: {args[0]}[/red]")
                return

        # 解析步骤名（支持步骤名或 "all"，必须提供）
        raw_step = args[1]
        if raw_step.lower() == "all":
            step_name = "all"
        elif raw_step in STEP_NAMES:
            step_name = raw_step
        else:
            self._log(f"[red]无效步骤名: {args[1]}，有效值: {STEP_NAMES} 或 all[/red]")
            return

        # 对于 clean all all（全量清理），显示受影响的目录列表
        if config_name == "all" and step_name == "all":
            from engine.config import LOCAL_PATHS, REMOTE_CONFIG

            dir_lines = []
            dir_lines.append("[bold]─── 本地 PC ───[/bold]")
            local_step = LOCAL_PATHS.get("step_dir", "")
            local_scdoc = LOCAL_PATHS.get("scdoc_dir", "")
            if local_step:
                dir_lines.append(f"  • STEP 文件: {local_step}")
            if local_scdoc:
                dir_lines.append(f"  • SCDOC 文件: {local_scdoc}")

            dir_lines.append("[bold]─── 远程工作站 ({host}) ───[/bold]".format(
                host=REMOTE_CONFIG.get("host", "?")))
            remote_msh = REMOTE_CONFIG.get("msh_dir", "")
            remote_result = REMOTE_CONFIG.get("result_dir", "")
            if remote_msh:
                dir_lines.append(f"  • MSH 网格文件: {remote_msh}")
            if remote_result:
                dir_lines.append(f"  • CAS/DAT 求解结果: {remote_result}")

            dirs_text = "\n".join(dir_lines)
            detail = f"\n\n⚠ 将清空以下目录下的所有仿真中间文件：\n{dirs_text}"
        else:
            detail = ""

        # 构建确认对话框文本
        cfg_desc = "所有构型" if config_name == "all" else f"构型{config_name}"
        if step_name == "all":
            step_desc = "所有步骤"
        else:
            step_desc = f"{step_name} 步骤"

        self.push_screen(
            ConfirmDialog(
                f"确定要清理{cfg_desc}的{step_desc}产生的文件吗？此操作不可逆！{detail}",
                callback=lambda: self._do_clean_step(step_name, config_name)
            )
        )

    def _do_clean_step(self, step_name, config_name=None):
        """执行清理步骤操作。step_name 支持 str 或 'all'，config_name 支持 int、None 或 'all'。"""
        if not self._check_connection():
            return
        ok, msg = self.ipc.clean_step(step_name, config_name)
        if ok:
            self._log(f"[green]✓ {msg}[/green]")
        else:
            self._log(f"[red]✗ {msg}[/red]")

    def _do_quit(self):
        """执行 quit 命令（仅退出 TUI，后台继续运行）。"""
        self._log("[yellow]⚠ 界面已退出，后台引擎仍在运行[/yellow]")
        self._log("[yellow]  使用 start_client.py 可重新连接界面[/yellow]")
        # Textual 的 exit() 会先将待显示消息刷新到屏幕后再退出
        self.exit()

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
        self.exit()

    def _handle_daemon_cmd(self, args: list):
        """处理 daemon 子命令。"""
        if not args:
            self._log("[yellow]用法: daemon start  或 daemon stop[/yellow]")
        elif args[0].lower() == "stop":
            self._do_stop_daemon()
        elif args[0].lower() == "start":
            self._do_launch_daemon()
        else:
            self._log(f"[yellow]用法: daemon start  或 daemon stop[/yellow]")

    def _do_launch_daemon(self):
        """启动后台守护进程并自动连接。"""
        if self.ipc.is_connected():
            self._log("[yellow]⚠ 已连接到后台引擎，无需重复启动[/yellow]")
            return

        try:
            project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            daemon_script = os.path.join(project_dir, "start_daemon.py")

            if not os.path.exists(daemon_script):
                self._log(f"[red]✗ 未找到启动脚本: {daemon_script}[/red]")
                return

            # Windows 下隐藏控制台窗口
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self._daemon_process = subprocess.Popen(
                [sys.executable, daemon_script],
                cwd=project_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            self._log("[cyan]⚠ 后台引擎正在启动 (PID: {})，等待 IPC 就绪...[/cyan]".format(
                self._daemon_process.pid))

            # 启动异步轮询任务
            asyncio.create_task(self._poll_daemon_startup())
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            self._log(f"[red]✗ 启动后台引擎失败: {e}[/red]")

    def _do_stop_daemon(self):
        """停止后台守护进程（先 IPC 优雅退出，再强制终止子进程）。"""
        if not self.ipc.is_connected() and self._daemon_process is None:
            self._log("[yellow]⚠ 后台引擎未运行或非本 TUI 启动[/yellow]")
            return

        self.push_screen(
            ConfirmDialog(
                "确定要【停止后台引擎】吗？\n所有正在运行的任务将被中止！\n"
                "（TUI 界面将保持运行，可随时重新启动 daemon）",
                callback=self._execute_stop_daemon
            )
        )

    def _execute_stop_daemon(self):
        """执行停止 daemon 操作。"""
        # 1) 通过 IPC 优雅退出
        if self.ipc.is_connected():
            ok, msg = self.ipc.full_quit()
            if ok:
                self._log(f"[yellow]⏹ {msg}[/yellow]")
            else:
                self._log(f"[yellow]⚠ IPC 退出请求失败: {msg}，将强制终止进程[/yellow]")
            self.ipc.disconnect()

        # 2) 停止刷新定时器
        if self._refresh_timer:
            self._refresh_timer.stop()
            self._refresh_timer = None

        # 3) 终止 daemon 子进程（如果由本 TUI 启动）
        if self._daemon_process is not None:
            try:
                self._daemon_process.terminate()
                try:
                    self._daemon_process.wait(timeout=5)
                    self._log(f"[green]✓ 后台引擎进程已终止 (PID: {self._daemon_process.pid})[/green]")
                except subprocess.TimeoutExpired:
                    self._daemon_process.kill()
                    self._daemon_process.wait()
                    self._log(f"[yellow]⚠ 后台引擎进程被强制结束 (PID: {self._daemon_process.pid})[/yellow]")
            except (OSError, subprocess.SubprocessError) as e:
                self._log(f"[red]✗ 终止进程失败: {e}[/red]")
            self._daemon_process = None

        self._update_info_bar()

    async def _poll_daemon_startup(self):
        """异步轮询直到 Daemon IPC 就绪（最多等待 10 秒）。"""
        for i in range(20):  # 20 × 0.5s = 10s
            await asyncio.sleep(0.5)
            connected = await asyncio.to_thread(self.ipc.connect)
            if connected:
                self._log("[green]✓ 后台引擎已就绪，连接成功！[/green]")
                if not self._refresh_timer:
                    self._refresh_timer = self.set_interval(1.0, self._refresh_status)
                self._update_info_bar()
                return
        self._log("[red]✗ 后台引擎启动超时 (10s)，请手动检查 start_daemon.py 是否正常运行[/red]")

    def _show_reset_prompt(self):
        """提示用户输入 reset 参数。"""
        self._log("[yellow]请在命令输入行使用: reset <构型名|all> <步骤名|all>[/yellow]")
        self.query_one("#cmd-input", Input).focus()

    def _show_clean_prompt(self):
        """提示用户输入 clean 参数。"""
        self._log("[yellow]请在命令输入行使用: clean <构型名|all> <步骤名|all>[/yellow]")
        self.query_one("#cmd-input", Input).focus()

    def _show_help(self):
        """显示帮助信息。"""
        help_text = """
[bold]可用命令:[/bold]
  [dim]help[/dim]                        - 显示此帮助
  [green]start[/green]                       - 启动或继续流水线
  [yellow]pause[/yellow]                       - 暂停流水线
  [dim]check[/dim]                       - 系统自检
  [dim]status[/dim]                      - 显示状态摘要
  [red]reset <XX|all> <step|all>[/red]   - 重置构型步骤状态
  [red]clean <XX|all> <step|all>[/red]   - 清理构型步骤文件
  [cyan]daemon start[/cyan]                - 启动后台引擎并自动连接
  [cyan]daemon stop[/cyan]                 - 停止后台引擎（TUI 继续运行）
  [yellow]quit[/yellow]                        - 退出界面（引擎继续运行）
  [red]quit full[/red]                   - 完全退出（停止引擎 + 关闭 TUI）
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
                self._log("[green]✓ 已重新连接！[/green]")
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
