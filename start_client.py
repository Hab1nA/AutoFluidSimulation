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
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.logger import init_session

session_log_dir = init_session("client")

def find_rust_tui_binary():
    project_dir = os.path.dirname(os.path.abspath(__file__))
    tui_dir = os.path.join(project_dir, "autofluid-tui")
    debug_bin = os.path.join(tui_dir, "target", "debug", "autofluid-tui.exe")
    release_bin = os.path.join(tui_dir, "target", "release", "autofluid-tui.exe")
    if os.path.exists(release_bin):
        return release_bin
    if os.path.exists(debug_bin):
        return debug_bin
    return None


def check_rust_tui_source():
    project_dir = os.path.dirname(os.path.abspath(__file__))
    tui_dir = os.path.join(project_dir, "autofluid-tui")
    cargo_toml = os.path.join(tui_dir, "Cargo.toml")
    src_dir = os.path.join(tui_dir, "src")
    main_rs = os.path.join(src_dir, "main.rs")
    return (os.path.isdir(tui_dir) and
            os.path.isfile(cargo_toml) and
            os.path.isdir(src_dir) and
            os.path.isfile(main_rs))


def print_rust_tui_not_found_help():
    print("错误: 未找到 Rust TUI 二进制文件。", file=sys.stderr)
    if check_rust_tui_source():
        print(file=sys.stderr)
        print("检测到 autofluid-tui/Cargo.toml 存在，源码完整但尚未编译。", file=sys.stderr)
        print("请执行以下任一命令进行编译：", file=sys.stderr)
        print("  1. cd autofluid-tui && cargo build --release", file=sys.stderr)
        print("  2. 运行 rebuild_tui.bat（Windows 一键构建脚本）", file=sys.stderr)
    else:
        print(file=sys.stderr)
        print("未检测到 autofluid-tui/Cargo.toml，Rust TUI 源码可能缺失。", file=sys.stderr)
        print("请确认 autofluid-tui/ 目录存在且包含完整的 Rust 项目文件。", file=sys.stderr)
        print("可通过 git 恢复: git checkout -- autofluid-tui/", file=sys.stderr)

if __name__ == "__main__":
    rust_bin = find_rust_tui_binary()
    if rust_bin:
        print("正在启动 Rust TUI 客户端...")
        print(f"  日志目录: {session_log_dir}")
        print(f"  二进制: {rust_bin}")
        print("提示: 使用 quit 命令仅退出界面，后台引擎继续运行")
        print("      使用 quit full 命令彻底停止后台引擎")
        print()
        try:
            result = subprocess.run([rust_bin], cwd=os.path.dirname(os.path.abspath(__file__)))
            sys.exit(result.returncode)
        except FileNotFoundError:
            print(f"错误: 找不到 Rust TUI 二进制文件: {rust_bin}", file=sys.stderr)
            sys.exit(1)
        except KeyboardInterrupt:
            sys.exit(0)
    else:
        print_rust_tui_not_found_help()
        sys.exit(1)
