"""
调度器通用工具函数。

本模块提供跨子模块共享的辅助函数，避免代码重复。
"""

import time
import threading


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
