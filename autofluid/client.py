"""TUI 客户端。"""

from __future__ import annotations

import shlex
import threading
import time
from typing import Dict, List, Optional

from rich.console import Console
from rich.live import Live
from rich.prompt import Prompt
from rich.table import Table

from .config import CONFIG
from .constants import COMMAND_STEP_ALIASES, PIPELINE_STEPS, STEP_LABELS, StepName
from .ipc import IPCClient
from .state_store import StateStore


class PipelineTUI:
    """前端 TUI 客户端。"""

    def __init__(self, store: StateStore, ipc_client: IPCClient) -> None:
        self.store = store
        self.ipc = ipc_client
        self.console = Console()
        self._stop_event = threading.Event()

    def run(self) -> None:
        render_thread = threading.Thread(target=self._render_loop, daemon=True)
        render_thread.start()

        while not self._stop_event.is_set():
            command_line = Prompt.ask("命令")
            if not command_line:
                continue
            self._handle_command(command_line.strip())

        self._stop_event.set()
        render_thread.join(timeout=2)

    def _render_loop(self) -> None:
        with Live(self._build_table(), console=self.console, refresh_per_second=1) as live:
            while not self._stop_event.is_set():
                live.update(self._build_table())
                time.sleep(1)

    def _build_table(self) -> Table:
        table = Table(title="仿真流水线状态")
        table.add_column("构型", style="cyan", no_wrap=True)
        for step in PIPELINE_STEPS:
            table.add_column(STEP_LABELS[step], justify="center")
        statuses = self.store.get_all_statuses()
        for config in self.store.get_configs():
            row: List[str] = [config.name]
            status_map = statuses.get(config.name, {})
            for step in PIPELINE_STEPS:
                status = status_map.get(step)
                row.append(status.value if status else "")
            table.add_row(*row)
        return table

    def _handle_command(self, command_line: str) -> None:
        parts = shlex.split(command_line)
        if not parts:
            return
        command = parts[0].lower()

        if command == "quit":
            self.console.print("[yellow]界面已退出，后台流水线仍在运行[/yellow]")
            self._stop_event.set()
            return

        if command == "full_quit":
            if not self._confirm("确认同时退出后台引擎？"):
                return
            response = self.ipc.send_command("full_quit")
            self.console.print(response.get("message", "后台已退出"))
            self._stop_event.set()
            return

        if command == "start":
            self.console.print(self._send("start"))
            return

        if command == "pause":
            self.console.print(self._send("pause"))
            return

        if command == "check":
            self.console.print(self._send("check"))
            return

        if command == "reset":
            self._handle_reset(parts[1:])
            return

        if command == "clean":
            self._handle_clean(parts[1:])
            return

        self.console.print(f"[red]未知命令: {command}[/red]")

    def _handle_reset(self, args: List[str]) -> None:
        if not args:
            self.console.print("[red]reset 需要参数[/red]")
            return
        if args[0].lower() == "all":
            if not self._confirm("确认重置全部构型？"):
                return
            self.console.print(self._send("reset", {"all": True}))
            return
        if len(args) < 2:
            self.console.print("[red]reset <config> <step>[/red]")
            return
        name, step_alias = args[0], args[1].lower()
        step = COMMAND_STEP_ALIASES.get(step_alias)
        if not step:
            self.console.print("[red]未知步骤[/red]")
            return
        self.console.print(self._send("reset", {"name": name, "step": step.value}))

    def _handle_clean(self, args: List[str]) -> None:
        if not args:
            self.console.print("[red]clean 需要参数[/red]")
            return
        if args[0].lower() == "all":
            if not self._confirm("确认清理全部产物？"):
                return
            self.console.print(self._send("clean", {"all": True}))
            return
        step_alias = args[0].lower()
        step = COMMAND_STEP_ALIASES.get(step_alias)
        if not step:
            self.console.print("[red]未知步骤[/red]")
            return
        self.console.print(self._send("clean", {"step": step.value}))

    def _send(self, command: str, args: Optional[Dict[str, object]] = None) -> Dict[str, object]:
        try:
            return self.ipc.send_command(command, args)
        except OSError as exc:
            return {"ok": False, "message": f"IPC 连接失败: {exc}"}

    def _confirm(self, message: str) -> bool:
        answer = Prompt.ask(f"{message} (yes/no)", default="no")
        return answer.strip().lower() == "yes"


def run_client() -> None:
    store = StateStore(CONFIG.db_path)
    ipc = IPCClient(CONFIG.ipc_host, CONFIG.ipc_port)
    PipelineTUI(store, ipc).run()


if __name__ == "__main__":
    run_client()
