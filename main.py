"""
===============================================================================
总控程序入口 (Main Entry)
同时启动后台守护进程和 TUI 客户端（不推荐生产使用）。

推荐用法（两个独立终端）：
    终端1: python start_daemon.py    # 先启动后台引擎
    终端2: python start_client.py    # 再启动 TUI 界面

或者使用 --daemon 或 --client 参数分别启动。
===============================================================================
"""
import sys
import os
import argparse
import subprocess
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(
        description="液氧甲烷火箭发动机仿真总控程序"
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="仅启动后台守护进程"
    )
    parser.add_argument(
        "--client", action="store_true",
        help="仅启动 TUI 客户端"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="同时启动守护进程和客户端（守护进程在后台线程中运行）"
    )

    args = parser.parse_args()

    if args.daemon:
        from engine.daemon import main as daemon_main
        daemon_main()
    elif args.client:
        from client.tui import main as client_main
        client_main()
    elif args.all:
        print("=" * 60)
        print("  同时启动后台引擎 + TUI 界面")
        print("=" * 60)
        print()
        print("正在启动后台引擎...")

        # 在独立线程中启动守护进程
        daemon_thread = threading.Thread(
            target=lambda: __import__("engine.daemon", fromlist=["main"]).main(),
            daemon=True,
            name="DaemonThread"
        )
        daemon_thread.start()

        # 等待守护进程就绪
        time.sleep(2)

        print("正在启动 TUI 界面...")
        from client.tui import main as client_main
        client_main()
    else:
        parser.print_help()
        print()
        print("推荐使用方式:")
        print("  终端1: python start_daemon.py")
        print("  终端2: python start_client.py")


if __name__ == "__main__":
    main()
