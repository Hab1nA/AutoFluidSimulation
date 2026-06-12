from __future__ import annotations

"""
===============================================================================
启动后台守护进程 (Daemon)
在启动 TUI 客户端之前，先运行此脚本启动后台引擎。

用法：
    python start_daemon.py

守护进程启动后会在后台持续运行，等待 TUI 客户端连接。
在本终端窗口按 Ctrl+C 可完全退出后台引擎。
TUI 客户端中 Ctrl+C 仅退出前端；如需关闭后台引擎，请使用 quit full。
===============================================================================
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- 快速预检：已有 Daemon 运行时直接退出，不创建日志目录 ----
from utils.process_utils import check_ipc_ready, read_pid_file, is_process_alive
from engine.config import IPC_CONFIG

_PID_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_DAEMON_PID_FILE = os.path.join(_PID_DIR, "daemon.pid")

_stale_pid = read_pid_file(_DAEMON_PID_FILE)
if _stale_pid is not None and is_process_alive(_stale_pid):
    if check_ipc_ready(IPC_CONFIG["host"], IPC_CONFIG["port"]):
        print(f"[Daemon] 已有 Daemon 实例运行中 (PID: {_stale_pid})，跳过启动")
        sys.exit(0)

# ---- 正式初始化日志会话 ----
from utils.logger import init_session

session_timestamp = os.environ.get("AUTOFLUID_SESSION_TIMESTAMP")
session_log_dir = init_session("daemon", session_timestamp)

from engine.daemon import main

if __name__ == "__main__":
    print("=" * 60)
    print("液氧甲烷火箭发动机仿真 - 后台调度引擎")
    print("Pipeline Daemon Engine")
    print("=" * 60)
    print()
    print(f"日志目录: {session_log_dir}")
    print("启动后将监听 IPC 连接，等待 TUI 客户端...")
    print("提示: 在此窗口按 Ctrl+C 可完全退出后台引擎")
    print()
    main()
