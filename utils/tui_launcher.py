"""
===============================================================================
Rust TUI 二进制查找与启动工具
被 main.py 和 start_client.py 共用。
===============================================================================
"""
import os
import sys


def find_rust_tui_binary(project_dir: str) -> str | None:
    """查找已编译的 Rust TUI 二进制文件。

    Args:
        project_dir: 项目根目录绝对路径

    Returns:
        二进制文件路径，未找到返回 None
    """
    tui_dir = os.path.join(project_dir, "autofluid-tui")
    release_bin = os.path.join(tui_dir, "target", "release", "autofluid-tui.exe")
    debug_bin = os.path.join(tui_dir, "target", "debug", "autofluid-tui.exe")
    if os.path.exists(release_bin):
        return release_bin
    if os.path.exists(debug_bin):
        return debug_bin
    return None


def check_rust_tui_source(project_dir: str) -> bool:
    """检查 Rust TUI 源码是否完整存在。

    Args:
        project_dir: 项目根目录绝对路径

    Returns:
        True 表示 Cargo.toml 和 src/main.rs 均存在
    """
    tui_dir = os.path.join(project_dir, "autofluid-tui")
    cargo_toml = os.path.join(tui_dir, "Cargo.toml")
    src_dir = os.path.join(tui_dir, "src")
    main_rs = os.path.join(src_dir, "main.rs")
    return (os.path.isdir(tui_dir) and
            os.path.isfile(cargo_toml) and
            os.path.isdir(src_dir) and
            os.path.isfile(main_rs))


def print_rust_tui_not_found_help(project_dir: str):
    """打印 Rust TUI 未找到时的帮助信息。"""
    print("[错误] 未找到 Rust TUI 二进制文件。", file=sys.stderr)
    if check_rust_tui_source(project_dir):
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
