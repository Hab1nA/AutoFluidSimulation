from __future__ import annotations

"""
===============================================================================
进程管理工具模块 (Process Management Utilities)
提取重复的进程锁和 PID 文件管理代码，供 main.py 和 engine/daemon.py 共享使用。
===============================================================================
"""
import os
import logging
import shutil
import sys
import socket
import subprocess

from engine.config import LOCAL_PATHS


MIN_VALID_PID = 2
WORKER_PID_KINDS = (
    "local_worker",
    "tunnel_workstation",
    "tunnel_localworker",
    "server_ipc_tunnel",
)
TUNNEL_WATCHDOG_KINDS = ("Workstation", "LocalWorker")
logger = logging.getLogger(__name__)


def is_process_alive(pid: int) -> bool:
    """检测指定 PID 的进程是否存活（跨平台）。

    Args:
        pid: 进程 ID

    Returns:
        True 表示进程活跃，False 表示已结束或无效
    """
    if pid < MIN_VALID_PID:
        return False

    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x100000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except (OSError, AttributeError):
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False


def read_pid_file(pid_file: str) -> int | None:
    """读取 PID 文件中的进程 ID。

    Args:
        pid_file: PID 文件路径

    Returns:
        读取的 PID（整数），失败、文件不存在或 PID 不可管理时返回 None
    """
    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None
    if pid < MIN_VALID_PID:
        logger.warning("忽略保留或无效 PID 文件: %s, pid=%s", pid_file, pid)
        return None
    return pid


def write_pid_file(pid_file: str, pid: int) -> None:
    """写入 PID 到文件。

    Args:
        pid_file: PID 文件路径
        pid: 要写入的进程 ID
    """
    os.makedirs(os.path.dirname(pid_file), exist_ok=True)
    with open(pid_file, "w", encoding="utf-8") as f:
        f.write(str(pid))


def remove_pid_file(pid_file: str) -> None:
    """删除 PID 文件。

    Args:
        pid_file: PID 文件路径
    """
    try:
        os.remove(pid_file)
    except FileNotFoundError:
        pass


def run_taskkill(pid: int, timeout: int = 5) -> bool:
    """使用 taskkill 终止 Windows 进程。

    Args:
        pid: 进程 ID
        timeout: taskkill 命令超时（秒）

    Returns:
        True 表示成功，False 表示失败
    """
    if pid < MIN_VALID_PID:
        return False

    if sys.platform != "win32":
        return False

    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def get_process_command_line(pid: int, timeout: int = 3) -> str | None:
    """Return a process command line for ownership checks."""
    if pid < MIN_VALID_PID:
        return None

    try:
        if sys.platform == "win32":
            powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
            if powershell is None:
                return None
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-Command",
                    (
                        "Get-CimInstance Win32_Process -Filter "
                        f"'ProcessId = {pid}' | Select-Object -ExpandProperty CommandLine"
                    ),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        else:
            completed = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
    except (OSError, subprocess.SubprocessError):
        return None

    if completed.returncode != 0:
        return None
    command_line = completed.stdout.strip()
    return command_line or None


def worker_process_is_owned(kind: str, pid: int) -> bool:
    """Return True when a PID is recognizably owned by AutoFluid worker control."""
    command_line = get_process_command_line(pid)
    if command_line is None:
        return False
    lowered = command_line.lower()
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__))).lower()
    has_project_context = "autofluid" in lowered or project_root in lowered
    if not has_project_context:
        return False

    if kind == "local_worker":
        return (
            ("main.py" in lowered and "--worker" in lowered)
            or "engine.local_worker" in lowered
        )
    if kind == "tunnel_workstation":
        return (
            "start_workstation_reverse_tunnel.ps1" in lowered
            and "workstation" in lowered
        )
    if kind == "tunnel_localworker":
        return (
            "start_workstation_reverse_tunnel.ps1" in lowered
            and "localworker" in lowered
        )
    if kind == "server_ipc_tunnel":
        return (
            "start_server_ipc_tunnel.ps1" in lowered
            or ("ssh" in lowered and "127.0.0.1:9527" in lowered)
        )
    return False


def worker_pid_file(kind: str) -> str:
    """Return the stable PID file path for worker and SSH tunnel helpers."""
    if kind not in WORKER_PID_KINDS:
        raise ValueError(f"未知 Worker PID 类型: {kind}")
    data_dir = str(LOCAL_PATHS.get("data_dir") or "data")
    return os.path.join(data_dir, f"{kind}.pid")


def cleanup_worker_processes_from_pid_files(timeout: int = 5) -> dict[str, dict[str, int | str]]:
    """Terminate worker/tunnel processes recorded in stable PID files.

    This is intentionally PID-file based so `worker stop`, `quit full`, and
    `main.py --stop` can clean processes that were started by an earlier client
    or daemon process whose in-memory `Popen`/`Child` handles are gone.
    """
    results: dict[str, dict[str, int | str]] = {}
    for kind in WORKER_PID_KINDS:
        pid_file = worker_pid_file(kind)
        pid = read_pid_file(pid_file)
        if pid is None:
            continue
        status = "stale"
        if is_process_alive(pid):
            if worker_process_is_owned(kind, pid):
                status = "terminated" if run_taskkill(pid, timeout=timeout) else "failed"
            else:
                status = "skipped_not_owned"
        if status != "failed":
            if status != "skipped_not_owned":
                remove_pid_file(pid_file)
        results[kind] = {"pid": pid, "status": status}
    return results


def cleanup_tunnel_watchdog_tasks(project_dir: str | None = None) -> dict[str, dict[str, str] | str]:
    """Uninstall Windows Task Scheduler watchdogs for reverse SSH tunnels."""
    if sys.platform != "win32":
        return {"status": "skipped", "reason": "non_windows"}

    project_root = project_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script_path = os.path.join(project_root, "scripts", "start_workstation_reverse_tunnel.ps1")
    if not os.path.exists(script_path):
        return {"status": "skipped", "reason": "script_missing"}

    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe") or "powershell.exe"
    results: dict[str, dict[str, str] | str] = {}
    for tunnel_kind in TUNNEL_WATCHDOG_KINDS:
        try:
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    script_path,
                    "-TunnelKind",
                    tunnel_kind,
                    "-UninstallWatchdog",
                ],
                cwd=project_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if completed.returncode == 0:
                results[tunnel_kind] = {"status": "uninstalled"}
            else:
                results[tunnel_kind] = {
                    "status": "failed",
                    "returncode": str(completed.returncode),
                    "stderr": completed.stderr.strip(),
                    "stdout": completed.stdout.strip(),
                }
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("[Worker] %s tunnel watchdog cleanup failed: %s", tunnel_kind, exc)
            results[tunnel_kind] = {"status": "failed", "error": str(exc)}
    return results


def check_ipc_ready(host: str = "127.0.0.1", port: int = 9527) -> bool:
    """检测 IPC 端口是否已被监听。

    Args:
        host: IPC 服务器地址
        port: IPC 服务器端口

    Returns:
        True 表示端口已就绪可连接，False 表示未就绪
    """
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect((connect_host, port))
            return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False
