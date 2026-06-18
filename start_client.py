from __future__ import annotations

"""
===============================================================================
启动 TUI 客户端 (Client)
连接到后台守护进程，提供交互式终端界面。

用法：
    python start_client.py

前提条件：必须先确保 `AUTOFLUID_IPC_HOST` / `AUTOFLUID_SERVER_HOST`
指向正在运行的 daemon。
如果仅退出 TUI 而不停止后台（quit 命令），
可以随时重新运行此脚本连接回后台引擎。
===============================================================================
"""
import sys
import os
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.logger import init_session
from utils.tui_launcher import find_rust_tui_binary, print_rust_tui_not_found_help

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
session_log_dir = init_session("client")

if __name__ == "__main__":
    rust_bin = find_rust_tui_binary(PROJECT_DIR)
    if rust_bin:
        print("正在启动 Rust TUI 客户端...")
        print(f"  日志目录: {session_log_dir}")
        print(f"  二进制: {rust_bin}")
        print("  后端模式: 连接远端 daemon，不会在本地启动后台引擎")
        print("提示: 使用 quit 命令仅退出界面，后台引擎继续运行")
        print("      使用 quit full 命令彻底停止后台引擎")
        print()
        try:
            env = os.environ.copy()
            env["AUTOFLUID_SESSION_LOG_DIR"] = session_log_dir
            result = subprocess.run([rust_bin], cwd=PROJECT_DIR, env=env)
            sys.exit(result.returncode)
        except FileNotFoundError:
            print(f"错误: 找不到 Rust TUI 二进制文件: {rust_bin}", file=sys.stderr)
            sys.exit(1)
        except KeyboardInterrupt:
            sys.exit(0)
    else:
        print_rust_tui_not_found_help(PROJECT_DIR)
        sys.exit(1)
