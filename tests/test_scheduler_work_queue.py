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


def test_has_claim_detects_pending_and_in_flight() -> None:
    """has_claim 应在排队和执行期间返回 True，释放后返回 False。"""
    work_queue = UniqueWorkQueue[int](key=lambda item: item)

    # 提交前：无 claim
    assert work_queue.has_claim(1) is False

    # 提交后、出队前：有 claim（排队中）
    assert work_queue.submit(1) is True
    assert work_queue.has_claim(1) is True

    # 出队后、完成前：有 claim（执行中）
    item = work_queue.get_nowait()
    assert item == 1
    assert work_queue.has_claim(1) is True

    # 完成后：无 claim
    work_queue.complete(1)
    assert work_queue.has_claim(1) is False

    # requeue 后仍保持 claim，需重新 get 后再 complete
    assert work_queue.submit(2) is True
    _item = work_queue.get_nowait()
    work_queue.requeue(2)
    assert work_queue.has_claim(2) is True
    # 重新获取 requeue 的任务
    _item2 = work_queue.get_nowait()
    work_queue.complete(2)
    assert work_queue.has_claim(2) is False

    # clear 后释放 claim
    work_queue.submit(3)
    work_queue.clear()
    assert work_queue.has_claim(3) is False


def test_has_claim_with_custom_key_function() -> None:
    """has_claim 应正确使用自定义 key 函数。"""
    work_queue = UniqueWorkQueue[tuple[int, str]](key=lambda item: item[0])

    assert work_queue.submit((1, "path_a")) is True
    assert work_queue.has_claim(1) is True
    # 同一 key 不同 value 应被视为同一 claim
    assert work_queue.submit((1, "path_b")) is False

    item = work_queue.get_nowait()
    work_queue.complete(item)
    assert work_queue.has_claim(1) is False
