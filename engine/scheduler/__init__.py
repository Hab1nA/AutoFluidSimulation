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

from .main import PipelineScheduler
from .worker_pool import WorkerPoolManager
from .barrier import BarrierCoordinator
from .sw_phase import SWPhaseHandler
from .retry import RetryManager
from .utils import pause_aware_sleep

__all__ = [
    "PipelineScheduler",
    "WorkerPoolManager",
    "BarrierCoordinator",
    "SWPhaseHandler",
    "RetryManager",
    "pause_aware_sleep",
]
