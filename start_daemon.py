"""
===============================================================================
启动后台守护进程 (Daemon)
在启动 TUI 客户端之前，先运行此脚本启动后台引擎。

用法：
    python start_daemon.py

守护进程启动后会在后台持续运行，等待 TUI 客户端连接。
可以通过 Ctrl+C 安全退出。
===============================================================================
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.logger import init_session

session_timestamp = os.environ.get("AUTOFLUID_SESSION_TIMESTAMP")
session_log_dir = init_session("daemon", session_timestamp)

from engine.daemon import main

if __name__ == "__main__":
    print("=" * 60)
    print("  液氧甲烷火箭发动机仿真 - 后台调度引擎")
    print("  Pipeline Daemon Engine v2.2.0")
    print("=" * 60)
    print()
    print(f"  日志目录: {session_log_dir}")
    print("启动后将监听 IPC 连接，等待 TUI 客户端...")
    print("按 Ctrl+C 安全退出")
    print()
    main()
