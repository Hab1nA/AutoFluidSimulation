"""
===============================================================================
启动 TUI 客户端 (Client)
连接到后台守护进程，提供交互式终端界面。

用法：
    python start_client.py

前提条件：必须先启动 start_daemon.py（后台引擎）。
如果仅退出 TUI 而不停止后台（quit 命令），
可以随时重新运行此脚本连接回后台引擎。
===============================================================================
"""
import sys
import os

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client.tui import main

if __name__ == "__main__":
    print("正在启动 TUI 客户端...")
    print("提示: 使用 quit 命令仅退出界面，后台引擎继续运行")
    print("      使用 full_quit 命令彻底停止后台引擎")
    print()
    main()
