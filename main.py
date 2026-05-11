"""
===============================================================================
总控程序入口 (Main Entry)
支持多种启动模式，通过进程分离确保 client 和 daemon 独立运行。

用法：
    python main.py --all          # 同时启动 daemon + client（进程分离模式）
    python main.py --daemon       # 仅启动后台守护进程
    python main.py --client       # 仅启动 TUI 客户端
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
import socket
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PID_DIR = os.path.join(PROJECT_DIR, "data")
DAEMON_PID_FILE = os.path.join(PID_DIR, "daemon.pid")
IPC_HOST = "127.0.0.1"
IPC_PORT = 9527
IPC_READY_TIMEOUT = 20
MIN_VALID_PID = 1


def _find_rust_tui_binary():
    tui_dir = os.path.join(PROJECT_DIR, "autofluid-tui")
    debug_bin = os.path.join(tui_dir, "target", "debug", "autofluid-tui.exe")
    release_bin = os.path.join(tui_dir, "target", "release", "autofluid-tui.exe")
    if os.path.exists(release_bin):
        return release_bin
    if os.path.exists(debug_bin):
        return debug_bin
    return None


def _check_rust_tui_source():
    tui_dir = os.path.join(PROJECT_DIR, "autofluid-tui")
    cargo_toml = os.path.join(tui_dir, "Cargo.toml")
    src_dir = os.path.join(tui_dir, "src")
    main_rs = os.path.join(src_dir, "main.rs")
    return (os.path.isdir(tui_dir) and
            os.path.isfile(cargo_toml) and
            os.path.isdir(src_dir) and
            os.path.isfile(main_rs))


def _print_rust_tui_not_found_help():
    print("[错误] 未找到 Rust TUI 二进制文件。", file=sys.stderr)
    if _check_rust_tui_source():
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


def _ensure_dirs():
    os.makedirs(PID_DIR, exist_ok=True)


def _write_pid(pid_file: str, pid: int):
    with open(pid_file, "w", encoding="utf-8") as f:
        f.write(str(pid))


def _read_pid(pid_file: str) -> int | None:
    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _remove_pid(pid_file: str):
    try:
        os.remove(pid_file)
    except FileNotFoundError:
        pass


def _is_process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x100000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False


def _check_ipc_ready() -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect((IPC_HOST, IPC_PORT))
        s.close()
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def _wait_for_ipc(timeout: int = IPC_READY_TIMEOUT) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if _check_ipc_ready():
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


def _run_taskkill(pid: int) -> bool:
    if pid < MIN_VALID_PID:
        return False
    try:
        result = subprocess.run(
            ["taskkill", "/pid", str(pid), "/f"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    except OSError as e:
        print(f"  [警告] taskkill 失败 (PID: {pid}): {e}")
        return False
    except subprocess.SubprocessError as e:
        print(f"  [警告] taskkill 失败 (PID: {pid}): {e}")
        return False


def _start_daemon_subprocess(daemon_log_file: str) -> subprocess.Popen | None:
    daemon_script = os.path.join(PROJECT_DIR, "start_daemon.py")
    if not os.path.exists(daemon_script):
        print(f"[错误] 未找到启动脚本: {daemon_script}", file=sys.stderr)
        return None

    log_fo = open(daemon_log_file, "w", encoding="utf-8")

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW

    env = os.environ.copy()

    try:
        proc = subprocess.Popen(
            [sys.executable, daemon_script],
            cwd=PROJECT_DIR,
            stdout=log_fo,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            env=env,
        )
        _write_pid(DAEMON_PID_FILE, proc.pid)
        return proc
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[错误] 启动后台引擎失败: {e}", file=sys.stderr)
        log_fo.close()
        return None


def _stop_daemon_subprocess():
    pid = _read_pid(DAEMON_PID_FILE)
    if pid is None:
        print("  后台引擎: 未运行")
        _remove_pid(DAEMON_PID_FILE)
        return
    if pid < MIN_VALID_PID:
        print(f"  [警告] 无效 PID (PID: {pid})，跳过终止操作")
        _remove_pid(DAEMON_PID_FILE)
        return
    if _is_process_alive(pid):
        try:
            if sys.platform == "win32":
                if _run_taskkill(pid):
                    print(f"  后台引擎进程已终止 (PID: {pid})")
                else:
                    print(f"  [警告] 无法终止后台引擎进程 (PID: {pid})")
            else:
                os.kill(pid, signal.SIGTERM)
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
                print(f"  后台引擎进程已终止 (PID: {pid})")
        except (OSError, ProcessLookupError) as e:
            print(f"  [警告] 终止后台引擎进程失败: {e}")
    else:
        print("  后台引擎: 未运行")
    _remove_pid(DAEMON_PID_FILE)


def _stop_all_processes():
    print("=" * 60)
    print("  正在停止所有仿真进程...")
    print("=" * 60)

    if sys.platform == "win32":

        for pattern, label in [
            ("start_daemon.py", "后台引擎"),
            ("start_client.py", "TUI 客户端"),
            ("main.py --all", "总控程序(--all)"),
        ]:
            try:
                result = subprocess.run(
                    ['wmic', 'process', 'where',
                     f"commandline like '%{pattern}%' and name='python.exe'",
                     'get', 'processid', '/format:csv'],
                    capture_output=True, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                pids = []
                for line in result.stdout.strip().splitlines():
                    line = line.strip()
                    if not line or line.startswith("Node"):
                        continue
                    parts = line.split(",")
                    if len(parts) >= 2 and parts[-1].strip().isdigit():
                        pids.append(int(parts[-1].strip()))
                for p in pids:
                    try:
                        if _run_taskkill(p):
                            print(f"  {label}进程已终止 (PID: {p})")
                        else:
                            print(f"  [警告] 无法终止 {label} 进程 (PID: {p})")
                    except OSError:
                        pass
            except (subprocess.SubprocessError, OSError):
                pass

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


def _show_status():
    print("=" * 60)
    print("  仿真程序运行状态")
    print("=" * 60)

    daemon_pid = _read_pid(DAEMON_PID_FILE)
    if daemon_pid is not None and _is_process_alive(daemon_pid):
        print(f"  后台引擎: 运行中 (PID: {daemon_pid})")
    else:
        print("  后台引擎: 未运行")

    if _check_ipc_ready():
        print(f"  IPC 端口 {IPC_PORT}: 已监听")
    else:
        print(f"  IPC 端口 {IPC_PORT}: 未监听")

    daemon_session = _find_latest_session_dir("daemon")
    if daemon_session and os.path.isdir(daemon_session):
        print(f"  Daemon 最新日志目录: {daemon_session}")
        try:
            log_files = [f for f in os.listdir(daemon_session) if f.endswith(".log")]
            for lf in sorted(log_files):
                fp = os.path.join(daemon_session, lf)
                size = os.path.getsize(fp)
                mtime = datetime.fromtimestamp(os.path.getmtime(fp))
                print(f"    {lf}  ({size} 字节, {mtime:%Y-%m-%d %H:%M:%S})")
        except OSError:
            pass

    client_session = _find_latest_session_dir("client")
    if client_session and os.path.isdir(client_session):
        print(f"  Client 最新日志目录: {client_session}")
        try:
            log_files = [f for f in os.listdir(client_session) if f.endswith(".log")]
            for lf in sorted(log_files):
                fp = os.path.join(client_session, lf)
                size = os.path.getsize(fp)
                mtime = datetime.fromtimestamp(os.path.getmtime(fp))
                print(f"    {lf}  ({size} 字节, {mtime:%Y-%m-%d %H:%M:%S})")
        except OSError:
            pass

    print()


def _run_all_mode():
    from utils.logger import init_session, build_session_log_dir

    _ensure_dirs()

    session_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    client_log_dir = init_session("client", session_timestamp)
    daemon_log_dir = build_session_log_dir("daemon", session_timestamp)
    os.makedirs(daemon_log_dir, exist_ok=True)

    daemon_subprocess_log = os.path.join(daemon_log_dir, "subprocess.log")

    os.environ["AUTOFLUID_SESSION_TIMESTAMP"] = session_timestamp

    print("=" * 60)
    print("  同时启动后台引擎 + TUI 界面 (进程分离模式)")
    print("=" * 60)
    print()
    print(f"  会话时间戳: {session_timestamp}")
    print(f"  Daemon 日志目录: {daemon_log_dir}")
    print(f"  Client 日志目录: {client_log_dir}")
    print()

    sp_logger = _setup_subprocess_logger(daemon_subprocess_log)

    # ---- 1. 启动 Daemon 子进程 ----
    print("[1/3] 正在启动后台引擎 (Daemon)...")
    daemon_proc = _start_daemon_subprocess(daemon_subprocess_log)
    if daemon_proc is None:
        print("[错误] 后台引擎启动失败，终止操作。", file=sys.stderr)
        sys.exit(1)
    sp_logger.info(f"Daemon 子进程已启动, PID={daemon_proc.pid}")
    print(f"      后台引擎已启动 (PID: {daemon_proc.pid})")
    print(f"      日志文件: {daemon_subprocess_log}")

    # ---- 2. 等待 IPC 就绪 ----
    print("[2/3] 等待后台引擎 IPC 就绪...")
    ready = _wait_for_ipc()
    if ready:
        print("      后台引擎 IPC 已就绪")
        sp_logger.info("Daemon IPC 已就绪")
    else:
        print("[警告] 后台引擎未在预期时间内就绪，仍将尝试启动客户端")
        print("       若客户端无法连接，请检查日志: " + daemon_subprocess_log)
        sp_logger.warning("Daemon IPC 就绪超时")

    # ---- 3. 启动 TUI Client ----
    print("[3/3] 正在启动 TUI 客户端 (Client)...")
    sp_logger.info("准备启动 TUI Client")

    # 注册退出清理
    _daemon_proc_ref = [daemon_proc]

    def _cleanup_on_exit(signum=None, frame=None):
        sp_logger.info("收到退出信号，正在清理...")
        proc = _daemon_proc_ref[0]
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                sp_logger.info(f"Daemon 子进程已终止 (PID: {proc.pid})")
            except (OSError, subprocess.SubprocessError):
                pass
        _remove_pid(DAEMON_PID_FILE)
        if "AUTOFLUID_SESSION_TIMESTAMP" in os.environ:
            del os.environ["AUTOFLUID_SESSION_TIMESTAMP"]

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _cleanup_on_exit)
        except (AttributeError, ValueError):
            pass

    # 启动 TUI（在主进程中运行 Rust TUI 子进程，占据当前终端）
    try:
        rust_bin = _find_rust_tui_binary()
        if rust_bin:
            tui_proc = subprocess.Popen(
                [rust_bin],
                cwd=PROJECT_DIR,
            )
            tui_proc.wait()
        else:
            sp_logger.error("未找到 Rust TUI 二进制文件")
            _print_rust_tui_not_found_help()
    except Exception as e:
        sp_logger.error(f"TUI Client 异常退出: {e}", exc_info=True)
        print(f"\n[错误] TUI 客户端异常退出: {e}", file=sys.stderr)
    finally:
        sp_logger.info("TUI Client 已退出，正在清理 Daemon 子进程...")
        _cleanup_on_exit()
        print()
        print("后台引擎已随客户端退出而终止。")
        print("如需后台引擎继续运行，请使用 start_daemon.py 单独启动。")


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
        help="同时启动守护进程和客户端（进程分离模式，Daemon 输出重定向到日志文件）"
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
        print(f"  日志目录: {get_session_log_dir()}")
        from engine.daemon import main as daemon_main
        daemon_main()
    elif args.client:
        from utils.logger import init_session, get_session_log_dir
        init_session("client")
        print(f"  日志目录: {get_session_log_dir()}")

        rust_bin = _find_rust_tui_binary()
        if rust_bin:
            try:
                result = subprocess.run([rust_bin], cwd=PROJECT_DIR)
                sys.exit(result.returncode)
            except FileNotFoundError:
                print(f"[错误] 找不到 Rust TUI 二进制文件: {rust_bin}", file=sys.stderr)
                sys.exit(1)
            except KeyboardInterrupt:
                sys.exit(0)
        else:
            _print_rust_tui_not_found_help()
            sys.exit(1)
    elif args.all:
        _run_all_mode()
    elif args.stop:
        _stop_all_processes()
    elif args.status:
        _show_status()
    else:
        parser.print_help()
        print()
        print("推荐使用方式:")
        print("  python main.py --all       # 一键启动 (Daemon + Client)")
        print("  python start.bat            # Windows 快捷启动 (双窗口)")
        print("  终端1: python start_daemon.py")
        print("  终端2: python start_client.py")


if __name__ == "__main__":
    main()
