"""
调度器通用工具函数。

本模块提供跨子模块共享的辅助函数，避免代码重复。
"""

import os
import time
import threading
from typing import Optional, Any

from engine.config import get_step_filename


def wait_unless_paused_or_stopped(
    paused_event: threading.Event,
    stopped_event: threading.Event,
) -> bool:
    """
    阻塞等待直到暂停解除或收到停止信号。

    替代重复的 ``while paused and not stopped: sleep(1)`` 模式。
    在暂停期间阻塞当前线程，每秒检查一次标志。

    Args:
        paused_event: 暂停事件（set = 已暂停）
        stopped_event: 停止事件（set = 已停止）

    Returns:
        True 表示暂停已解除且未被停止，可继续执行。
        False 表示收到停止信号，调用方应立即退出。
    """
    while paused_event.is_set() and not stopped_event.is_set():
        time.sleep(1)
    return not stopped_event.is_set()


def pause_aware_sleep(
    duration: float,
    paused_event: threading.Event,
    stopped_event: threading.Event,
    check_interval: float = 1.0,
) -> bool:
    """
    可响应暂停/停止的 sleep 替代方法。

    将 sleep 切分为 check_interval 粒度的小段，每段检查
    paused_event 和 stopped_event 标志。若检测到 stopped 则立即返回。

    Args:
        duration: 总等待时长（秒）
        paused_event: 暂停事件（set = 已暂停）
        stopped_event: 停止事件（set = 已停止）
        check_interval: 每次检查的间隔（秒）

    Returns:
        True 表示 sleep 完整结束，False 表示因 stopped 提前退出
    """
    deadline = time.time() + duration
    while time.time() < deadline:
        if stopped_event.is_set():
            return False
        while paused_event.is_set() and not stopped_event.is_set():
            time.sleep(1)
        if stopped_event.is_set():
            return False
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(check_interval, remaining))
    return True


def check_step_output_exists(
    config_name: int,
    step_name: str,
    step_dir: str,
    scdoc_dir: str,
    remote_config: Any,
    ssh: Optional[Any] = None,
) -> bool:
    """
    检查某构型某步骤的输出文件是否已存在。

    统一了 prescan_downstream_outputs、_resume_paused_steps、
    worker_pool._process_single_config、meshing_monitor 中的
    输出文件存在性检查逻辑。

    Args:
        config_name: 构型名称
        step_name: 步骤名（SW/SC/Transfer/Meshing/Solver）
        step_dir: 本地 STEP 文件目录
        scdoc_dir: 本地 SCDOC 文件目录
        remote_config: 远程配置字典（需含 scdoc_dir/flag_dir/msh_dir/result_dir）
        ssh: 可选的 SSH 连接对象（需有 check_remote_file 方法）

    Returns:
        True 表示输出文件已存在且大小 > 0
    """

    # ---- 本地文件检查（SW/SC）----
    if step_name == "SW":
        filename = get_step_filename("SW", config_name)
        if not filename:
            return False
        path = os.path.join(step_dir, filename)
        return os.path.exists(path) and os.path.getsize(path) > 0

    if step_name == "SC":
        filename = get_step_filename("SC", config_name)
        if not filename:
            return False
        path = os.path.join(scdoc_dir, filename)
        return os.path.exists(path) and os.path.getsize(path) > 0

    # ---- 远程文件检查（Transfer/Meshing/Solver）----
    if ssh is None:
        return False

    try:
        if not ssh.is_connected():
            return False
    except Exception:
        return False

    if step_name == "Transfer":
        filename = get_step_filename("SC", config_name)
        if not filename:
            return False
        remote_scdoc = (
            f"{remote_config['scdoc_dir'].replace(chr(92), '/')}"
            f"/{filename}"
        )
        try:
            return bool(ssh.check_remote_file(remote_scdoc))
        except Exception:
            return False

    if step_name == "Meshing":
        flag_file = (
            f"{remote_config['flag_dir'].replace(chr(92), '/')}"
            f"/meshing_done_{config_name}.txt"
        )
        mesh_name = get_step_filename("Meshing", config_name)
        mesh_file = None
        if mesh_name:
            mesh_file = (
                f"{remote_config['msh_dir'].replace(chr(92), '/')}"
                f"/{mesh_name}"
            )
        try:
            return bool(ssh.check_remote_file(flag_file)) or (
                mesh_file is not None and bool(ssh.check_remote_file(mesh_file))
            )
        except Exception:
            return False

    if step_name == "Solver":
        flag_file = (
            f"{remote_config['flag_dir'].replace(chr(92), '/')}"
            f"/solver_done_{config_name}.txt"
        )
        result_dir = remote_config['result_dir'].replace(chr(92), '/')
        # 同时检查 cas.h5 和 dat.h5（Fluent write_case_data 同时生成两者）
        cas_name = get_step_filename("Solver", config_name)
        dat_name = get_step_filename("Solver_dat", config_name)
        cas_file = f"{result_dir}/{cas_name}" if cas_name else None
        dat_file = f"{result_dir}/{dat_name}" if dat_name else None
        try:
            if bool(ssh.check_remote_file(flag_file)):
                return True
            if cas_file is not None and bool(ssh.check_remote_file(cas_file)):
                return True
            if dat_file is not None and bool(ssh.check_remote_file(dat_file)):
                return True
            return False
        except Exception:
            return False

    return False
