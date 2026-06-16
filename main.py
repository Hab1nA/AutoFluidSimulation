from __future__ import annotations

"""
===============================================================================
总控程序入口 (Main Entry)
支持多种启动模式，通过进程分离确保 client 和 daemon 独立运行。

用法：
    python main.py --all          # 同时启动 daemon + client（进程分离模式）
    python main.py --daemon       # 仅启动后台守护进程
    python main.py --client       # 仅启动 TUI 客户端
    python main.py --worker       # 仅启动本地 LocalWorker
    python main.py --stop         # 终止所有运行中的仿真进程
    python main.py --status       # 查看运行状态

快捷方式：
    start.bat                     # Windows 批处理快捷启动
    start.bat stop                # 停止所有程序
    start.bat status              # 查看状态
===============================================================================
"""
import sys
import os
import argparse
import subprocess
import signal
import time
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.tui_launcher import find_rust_tui_binary, print_rust_tui_not_found_help
from utils.process_utils import (
    check_ipc_ready,
    cleanup_worker_processes_from_pid_files,
    is_process_alive,
    read_pid_file,
    remove_pid_file,
    run_taskkill,
)
from engine.config import IPC_CONFIG, PROCESS_MANAGEMENT

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PID_DIR = os.path.join(PROJECT_DIR, "data")
DAEMON_PID_FILE = os.path.join(PID_DIR, "daemon.pid")
IPC_READY_TIMEOUT = PROCESS_MANAGEMENT["ipc_ready_timeout"]




def _ensure_dirs() -> None:
    """创建必要的目录结构。"""
    os.makedirs(PID_DIR, exist_ok=True)


def _wait_for_ipc(timeout: int = IPC_READY_TIMEOUT) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if check_ipc_ready(IPC_CONFIG["host"], IPC_CONFIG["port"]):
            return True
        time.sleep(0.5)
    return False


def _setup_subprocess_logger(log_file: str) -> logging.Logger:
    logger = logging.getLogger("main.subprocess")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    return logger


def _start_daemon_subprocess(daemon_log_file: str) -> subprocess.Popen | None:
    """启动后台守护进程子进程。

    Args:
        daemon_log_file: 日志文件路径

    Returns:
        成功时返回 Popen 对象（包含日志文件句柄引用），失败时返回 None
    """
    daemon_script = os.path.join(PROJECT_DIR, "start_daemon.py")
    if not os.path.exists(daemon_script):
        print(f"[错误] 未找到启动脚本: {daemon_script}", file=sys.stderr)
        return None

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW

    env = os.environ.copy()

    log_fo = None
    try:
        # 打开日志文件
        log_fo = open(daemon_log_file, "w", encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, daemon_script],
            cwd=PROJECT_DIR,
            stdout=log_fo,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            env=env,
        )
        # 保存文件句柄引用到 Popen 对象上，确保后续能够关闭
        proc._log_file_handle = log_fo  # type: ignore[attr-defined]
        return proc
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[错误] 启动后台引擎失败: {e}", file=sys.stderr)
        if log_fo:
            try:
                log_fo.close()
            except OSError:
                pass
        return None


def _stop_daemon_subprocess() -> None:
    """终止后台守护进程。"""
    pid = read_pid_file(DAEMON_PID_FILE)
    if pid is None:
        print("后台引擎: 未运行")
        remove_pid_file(DAEMON_PID_FILE)
        return
    if not is_process_alive(pid):
        print("后台引擎: 未运行")
    else:
        stopped = False
        try:
            if sys.platform == "win32":
                if run_taskkill(pid):
                    print(f"后台引擎进程已终止 (PID: {pid})")
                    stopped = True
                else:
                    print(f"[警告] 无法终止后台引擎进程 (PID: {pid})")
            else:
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if not is_process_alive(pid):
                        stopped = True
                        break
                    time.sleep(0.2)
                if not stopped:
                    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
                    os.kill(pid, sigkill)
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        if not is_process_alive(pid):
                            stopped = True
                            break
                        time.sleep(0.2)
                if stopped:
                    print(f"后台引擎进程已终止 (PID: {pid})")
                else:
                    print(f"[警告] 无法终止后台引擎进程 (PID: {pid})")
        except (OSError, ProcessLookupError) as e:
            print(f"[警告] 终止后台引擎进程失败: {e}")
        if not stopped:
            return
    remove_pid_file(DAEMON_PID_FILE)


def _stop_all_processes() -> None:
    print("=" * 60)
    print("正在停止所有仿真进程...")
    print("=" * 60)

    if sys.platform == "win32":
        # 使用 Get-CimInstance 替代已弃用的 wmic（Windows 11 24H2+）
        for pattern, label in [
            ("start_daemon.py", "后台引擎"),
            ("start_client.py", "TUI 客户端"),
            ("main.py --all", "总控程序(--all)"),
            ("main.py --worker", "LocalWorker"),
        ]:
            try:
                ps_filter = (
                    f"Name='python.exe' AND CommandLine LIKE '%{pattern}%'"
                )
                ps_cmd = (
                    f"Get-CimInstance Win32_Process -Filter \"{ps_filter}\" "
                    f"| Select-Object -ExpandProperty ProcessId"
                )
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                for line in result.stdout.strip().splitlines():
                    pid_str = line.strip()
                    if pid_str.isdigit():
                        p = int(pid_str)
                        try:
                            if run_taskkill(p):
                                print(f"{label}进程已终止 (PID: {p})")
                            else:
                                print(f"[警告] 无法终止 {label} 进程 (PID: {p})")
                        except OSError:
                            pass
            except (subprocess.SubprocessError, OSError):
                pass

    cleanup_worker_processes_from_pid_files()
    _stop_daemon_subprocess()
    print("所有进程已停止。")


def _find_latest_session_dir(process_type: str) -> str | None:
    """查找指定进程类型的最新会话日志目录。"""
    from utils.logger import _resolve_base_log_dir
    type_dir = os.path.join(_resolve_base_log_dir(), process_type)
    if not os.path.isdir(type_dir):
        return None
    try:
        sessions = sorted(
            [d for d in os.listdir(type_dir)
             if os.path.isdir(os.path.join(type_dir, d))],
            reverse=True,
        )
        if sessions:
            return os.path.join(type_dir, sessions[0])
    except OSError:
        pass
    return None


def _show_status() -> None:
    print("=" * 60)
    print("仿真程序运行状态")
    print("=" * 60)

    daemon_pid = read_pid_file(DAEMON_PID_FILE)
    if daemon_pid is not None and is_process_alive(daemon_pid):
        print(f"后台引擎: 运行中 (PID: {daemon_pid})")
    else:
        print("后台引擎: 未运行")

    if check_ipc_ready():
        print(f"IPC 端口 {IPC_CONFIG['port']}: 已监听")
    else:
        print(f"IPC 端口 {IPC_CONFIG['port']}: 未监听")

    daemon_session = _find_latest_session_dir("daemon")
    if daemon_session and os.path.isdir(daemon_session):
        print(f"Daemon 最新日志目录: {daemon_session}")
        try:
            log_files = [f for f in os.listdir(daemon_session) if f.endswith(".log")]
            for lf in sorted(log_files):
                fp = os.path.join(daemon_session, lf)
                size = os.path.getsize(fp)
                mtime = datetime.fromtimestamp(os.path.getmtime(fp))
                print(f"{lf}  ({size} 字节, {mtime:%Y-%m-%d %H:%M:%S})")
        except OSError:
            pass

    client_session = _find_latest_session_dir("client")
    if client_session and os.path.isdir(client_session):
        print(f"Client 最新日志目录: {client_session}")
        try:
            log_files = [f for f in os.listdir(client_session) if f.endswith(".log")]
            for lf in sorted(log_files):
                fp = os.path.join(client_session, lf)
                size = os.path.getsize(fp)
                mtime = datetime.fromtimestamp(os.path.getmtime(fp))
                print(f"{lf}  ({size} 字节, {mtime:%Y-%m-%d %H:%M:%S})")
        except OSError:
            pass

    print()


def _run_all_mode() -> None:
    from utils.logger import init_session, build_session_log_dir

    _ensure_dirs()

    # ---- 0. 检测是否已有 Daemon 在运行 ----
    daemon_already_running = check_ipc_ready()

    if daemon_already_running:
        print("=" * 60)
        print("TUI 界面 (连接到已有后台引擎)")
        print("=" * 60)
        print()
        daemon_pid = read_pid_file(DAEMON_PID_FILE)
        if daemon_pid:
            print(f"后台引擎已在运行 (PID: {daemon_pid})，直接启动客户端...")
        else:
            print("后台引擎已在运行，直接启动客户端...")
        print()

        # 直接启动 TUI（在主进程中运行 Rust TUI 子进程）
        try:
            rust_bin = find_rust_tui_binary(PROJECT_DIR)
            if rust_bin:
                # 传递最新 client 日志目录给 TUI
                latest_client = _find_latest_session_dir("client")
                tui_env = os.environ.copy()
                if latest_client:
                    tui_env["AUTOFLUID_SESSION_LOG_DIR"] = latest_client
                tui_proc = subprocess.Popen([rust_bin], cwd=PROJECT_DIR, env=tui_env)
                tui_proc.wait()
            else:
                print_rust_tui_not_found_help(PROJECT_DIR)
                sys.exit(1)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            print(f"\n[错误] TUI 客户端异常退出: {e}", file=sys.stderr)
            sys.exit(1)
        return

    session_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    client_log_dir = init_session("client", session_timestamp)
    daemon_log_dir = build_session_log_dir("daemon", session_timestamp)
    os.makedirs(daemon_log_dir, exist_ok=True)

    daemon_subprocess_log = os.path.join(daemon_log_dir, "subprocess.log")

    os.environ["AUTOFLUID_SESSION_TIMESTAMP"] = session_timestamp

    print("=" * 60)
    print("同时启动后台引擎 + TUI 界面 (进程分离模式)")
    print("=" * 60)
    print()
    print(f"会话时间戳: {session_timestamp}")
    print(f"Daemon 日志目录: {daemon_log_dir}")
    print(f"Client 日志目录: {client_log_dir}")
    print()

    sp_logger = _setup_subprocess_logger(daemon_subprocess_log)

    # ---- 1. 启动 Daemon 子进程 ----
    print("[1/3] 正在启动后台引擎 (Daemon)...")
    daemon_proc = _start_daemon_subprocess(daemon_subprocess_log)
    if daemon_proc is None:
        print("[错误] 后台引擎启动失败，终止操作。", file=sys.stderr)
        sys.exit(1)
    sp_logger.info(f"Daemon 子进程已启动, PID={daemon_proc.pid}")
    print(f"后台引擎已启动 (PID: {daemon_proc.pid})")
    print(f"日志文件: {daemon_subprocess_log}")

    # ---- 2. 等待 IPC 就绪 ----
    print("[2/3] 等待后台引擎 IPC 就绪...")
    ready = _wait_for_ipc()
    if ready:
        print("后台引擎 IPC 已就绪")
        sp_logger.info("Daemon IPC 已就绪")
    else:
        print("[警告] 后台引擎未在预期时间内就绪，仍将尝试启动客户端")
        print("若客户端无法连接，请检查日志: " + daemon_subprocess_log)
        sp_logger.warning("Daemon IPC 就绪超时")

    # ---- 3. 启动 TUI Client ----
    print("[3/3] 正在启动 TUI 客户端 (Client)...")
    sp_logger.info("准备启动 TUI Client")

    # 注册退出清理
    def _cleanup_on_exit(signum=None, frame=None):
        sp_logger.info("收到退出信号，正在清理...")
        if daemon_proc is not None:
            if hasattr(daemon_proc, "_log_file_handle"):
                try:
                    daemon_proc._log_file_handle.close()
                except (OSError, AttributeError):
                    pass
            if daemon_proc.poll() is None:
                try:
                    daemon_proc.terminate()
                    try:
                        daemon_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        daemon_proc.kill()
                        daemon_proc.wait()
                    sp_logger.info(f"Daemon 子进程已终止 (PID: {daemon_proc.pid})")
                except (OSError, subprocess.SubprocessError):
                    pass
        remove_pid_file(DAEMON_PID_FILE)
        if "AUTOFLUID_SESSION_TIMESTAMP" in os.environ:
            del os.environ["AUTOFLUID_SESSION_TIMESTAMP"]

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _cleanup_on_exit)
        except (AttributeError, ValueError):
            pass

    # 启动 TUI（在主进程中运行 Rust TUI 子进程，占据当前终端）
    try:
        rust_bin = find_rust_tui_binary(PROJECT_DIR)
        if rust_bin:
            tui_env = os.environ.copy()
            tui_env["AUTOFLUID_SESSION_LOG_DIR"] = client_log_dir
            tui_proc = subprocess.Popen(
                [rust_bin],
                cwd=PROJECT_DIR,
                env=tui_env,
            )
            tui_proc.wait()
        else:
            sp_logger.error("未找到 Rust TUI 二进制文件")
            print_rust_tui_not_found_help(PROJECT_DIR)
    except KeyboardInterrupt:
        sp_logger.info("TUI Client 退出")
    except OSError as e:
        sp_logger.error(f"TUI Client 启动失败: {e}", exc_info=True)
        print(f"\n[错误] TUI 客户端启动失败: {e}", file=sys.stderr)
    except Exception as e:
        sp_logger.error(f"TUI Client 异常退出: {e}", exc_info=True)
        print(f"\n[错误] TUI 客户端异常退出: {e}", file=sys.stderr)
    finally:
        sp_logger.info("TUI Client 已退出，正在清理 Daemon 子进程...")
        _cleanup_on_exit()
        print()
        print("后台引擎已随客户端退出而终止。")
        print("如需后台引擎继续运行，请使用 start_daemon.py 单独启动。")


def main() -> None:
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
        help="同时启动守护进程和客户端（进程分离模式，Daemon 输出重定向到日志文件）"
    )
    parser.add_argument(
        "--worker", action="store_true",
        help="仅启动本地 LocalWorker（用于连接远程 ocar daemon）"
    )
    parser.add_argument(
        "--worker-once", action="store_true",
        help="LocalWorker 仅注册并发送一次心跳，用于连通性测试"
    )
    parser.add_argument(
        "--stop", action="store_true",
        help="终止所有运行中的仿真进程"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="查看当前运行状态"
    )

    args = parser.parse_args()

    if args.daemon:
        from utils.logger import init_session, get_session_log_dir
        init_session("daemon")
        print(f"日志目录: {get_session_log_dir()}")
        from engine.daemon import main as daemon_main
        daemon_main()
    elif args.client:
        from utils.logger import init_session, get_session_log_dir
        init_session("client")
        print(f"日志目录: {get_session_log_dir()}")

        rust_bin = find_rust_tui_binary(PROJECT_DIR)
        if rust_bin:
            try:
                client_log_dir = get_session_log_dir()
                tui_env = os.environ.copy()
                if client_log_dir:
                    tui_env["AUTOFLUID_SESSION_LOG_DIR"] = client_log_dir
                result = subprocess.run([rust_bin], cwd=PROJECT_DIR, env=tui_env)
                sys.exit(result.returncode)
            except FileNotFoundError:
                print(f"[错误] 找不到 Rust TUI 二进制文件: {rust_bin}", file=sys.stderr)
                sys.exit(1)
            except KeyboardInterrupt:
                sys.exit(0)
            except OSError as e:
                print(f"[错误] Rust TUI 启动失败: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            print_rust_tui_not_found_help(PROJECT_DIR)
            sys.exit(1)
    elif args.all:
        _run_all_mode()
    elif args.worker or args.worker_once:
        from engine.config import ensure_directories, reload_config_from_toml
        from engine.local_worker import LocalWorker

        reload_config_from_toml()
        ensure_directories()
        worker = LocalWorker.from_env()
        if args.worker_once:
            worker.register_once()
            worker.heartbeat_once()
        else:
            worker.run_forever()
    elif args.stop:
        _stop_all_processes()
    elif args.status:
        _show_status()
    else:
        parser.print_help()
        print()
        print("推荐使用方式:")
        print("python main.py --all       # 一键启动 (Daemon + Client)")
        print("python main.py --worker     # 启动本地 LocalWorker 连接远程 daemon")
        print("python start.bat            # Windows 快捷启动 (双窗口)")
        print("终端1: python start_daemon.py")
        print("终端2: python start_client.py")


if __name__ == "__main__":
    main()
