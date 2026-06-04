"""带原子去重 claim 的线程安全任务队列。"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Hashable
from typing import Generic, TypeVar, cast

T = TypeVar("T")


class UniqueWorkQueue(Generic[T]):
    """保证同一 key 在排队或执行期间最多只有一个 claim。"""

    def __init__(self, key: Callable[[T], Hashable] | None = None) -> None:
        self._queue: queue.Queue[T] = queue.Queue()
        self._key = key or cast(Callable[[T], Hashable], lambda item: item)
        self._claims: set[Hashable] = set()
        self._lock = threading.Lock()

    def submit(self, item: T) -> bool:
        """原子提交任务；key 已存在时返回 False。"""
        item_key = self._key(item)
        with self._lock:
            if item_key in self._claims:
                return False
            self._claims.add(item_key)
            self._queue.put(item)
        return True

    def get(self, timeout: float | None = None) -> T:
        """获取一个任务，claim 保持到 complete()。"""
        return self._queue.get(timeout=timeout)

    def get_nowait(self) -> T:
        """立即获取一个任务，claim 保持到 complete()。"""
        return self._queue.get_nowait()

    def complete(self, item: T) -> None:
        """完成任务并释放 claim。"""
        with self._lock:
            self._claims.discard(self._key(item))
        self._queue.task_done()

    def has_claim(self, key: Hashable) -> bool:
        """检查指定 key 是否有活跃 claim（排队中或执行中）。"""
        with self._lock:
            return key in self._claims

    def requeue(self, item: T) -> None:
        """保留 claim 并将当前任务放回队列尾部。"""
        self._queue.put(item)
        self._queue.task_done()

    def clear(self) -> None:
        """清空尚未出队的任务并释放对应 claim。"""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            with self._lock:
                self._claims.discard(self._key(item))
            self._queue.task_done()

    def qsize(self) -> int:
        """返回等待出队的任务数量。"""
        return self._queue.qsize()

    def empty(self) -> bool:
        """队列中是否没有等待出队的任务。"""
        return self._queue.empty()
