"""流水线 pause/resume/stop 的统一并发控制。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class PipelineControl:
    """序列化控制状态变更和外部副作用启动边界。"""

    def __init__(self) -> None:
        self._paused = threading.Event()
        self._stopped = threading.Event()
        self._transition_lock = threading.RLock()

    @property
    def paused_event(self) -> threading.Event:
        """返回供子模块只读检查的暂停事件。"""
        return self._paused

    @property
    def stopped_event(self) -> threading.Event:
        """返回供子模块只读检查的停止事件。"""
        return self._stopped

    def prepare_start(self, *, clear_stopped: bool = True) -> bool:
        """准备启动；仅显式启动允许清除旧 stop。"""
        with self._transition_lock:
            if clear_stopped:
                self._stopped.clear()
            return not self._paused.is_set() and not self._stopped.is_set()

    def pause(self) -> None:
        """暂停流水线；调用返回后不再允许新的外部副作用启动。"""
        with self.pause_transition():
            pass

    @contextmanager
    def pause_transition(self) -> Iterator[None]:
        """设置 pause 并锁定状态同步窗口。"""
        with self._transition_lock:
            self._paused.set()
            yield

    def stop(self) -> None:
        """停止流水线并解除 pause 等待，使工作线程能够退出。"""
        with self._transition_lock:
            self._stopped.set()
            self._paused.clear()

    @contextmanager
    def resume_transition(self) -> Iterator[None]:
        """在恢复扫描完成后开放流水线；期间到达的 pause 会排队并最终生效。"""
        with self._transition_lock:
            self._stopped.clear()
            yield
            self._paused.clear()

    @contextmanager
    def external_start(self) -> Iterator[bool]:
        """锁定一个外部副作用启动窗口，并返回当前是否允许启动。"""
        with self._transition_lock:
            yield not self._paused.is_set() and not self._stopped.is_set()
