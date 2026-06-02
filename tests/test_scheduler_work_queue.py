"""scheduler 唯一任务队列测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from engine.scheduler.work_queue import UniqueWorkQueue


def test_duplicate_is_rejected_until_claim_is_completed() -> None:
    """任务出队后仍保持 claim，避免执行期间被重复提交。"""
    work_queue = UniqueWorkQueue[int](key=lambda item: item)

    assert work_queue.submit(1) is True
    assert work_queue.submit(1) is False
    assert work_queue.get_nowait() == 1
    assert work_queue.submit(1) is False

    work_queue.complete(1)
    assert work_queue.submit(1) is True


def test_clear_releases_pending_claims() -> None:
    """清空待处理任务后允许重新提交相同配置。"""
    work_queue = UniqueWorkQueue[int](key=lambda item: item)

    assert work_queue.submit(1) is True
    assert work_queue.submit(2) is True

    work_queue.clear()

    assert work_queue.qsize() == 0
    assert work_queue.submit(1) is True
    assert work_queue.submit(2) is True


def test_concurrent_submit_accepts_exactly_one_claim() -> None:
    """并发提交同一任务时，检查和占用必须是原子的。"""
    work_queue = UniqueWorkQueue[int](key=lambda item: item)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(work_queue.submit, [1] * 32))

    assert results.count(True) == 1
    assert results.count(False) == 31
    assert work_queue.qsize() == 1
