"""
调度器模块包。

将原先的单一 scheduler.py 拆分为多个职责单一的子模块：
- main.py: 主调度器（协调者）
- worker_pool.py: 工作线程池管理
- barrier.py: 全局屏障逻辑
- sw_phase.py: SW 阶段处理
- retry.py: 重试机制
- utils.py: 共享工具函数
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "PipelineScheduler": (".main", "PipelineScheduler"),
    "WorkerPoolManager": (".worker_pool", "WorkerPoolManager"),
    "BarrierCoordinator": (".barrier", "BarrierCoordinator"),
    "SWPhaseHandler": (".sw_phase", "SWPhaseHandler"),
    "RetryManager": (".retry", "RetryManager"),
    "MeshingMonitor": (".meshing_monitor", "MeshingMonitor"),
    "pause_aware_sleep": (".utils", "pause_aware_sleep"),
    "wait_unless_paused_or_stopped": (".utils", "wait_unless_paused_or_stopped"),
    "check_step_output_exists": (".utils", "check_step_output_exists"),
}

__all__ = [
    "PipelineScheduler",
    "WorkerPoolManager",
    "BarrierCoordinator",
    "SWPhaseHandler",
    "RetryManager",
    "MeshingMonitor",
    "pause_aware_sleep",
    "wait_unless_paused_or_stopped",
    "check_step_output_exists",
]


def __getattr__(name: str) -> Any:
    """按需加载公开符号，避免子模块导入触发调度器循环依赖。"""
    export = _EXPORTS.get(name)
    if export is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = export
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value
