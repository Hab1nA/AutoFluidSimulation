"""
===============================================================================
进程管理工具模块 (Process Management Utilities)
提取重复的进程锁和 PID 文件管理代码，供 main.py 和 engine/daemon.py 共享使用。
===============================================================================
"""
import os
import sys
import subprocess


MIN_VALID_PID = 1


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
        读取的 PID（整数），失败或文件不存在时返回 None
    """
    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


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
            ["taskkill", "/pid", str(pid), "/f"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
