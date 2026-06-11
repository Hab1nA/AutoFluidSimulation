"""
===============================================================================
任务执行器 (Task Runner)
负责协调各阶段的具体操作：

1. SW 阶段 → 委托给 executor/sw_executor.py (SWExecutor)
2. SC 阶段 → 本地 SCProcessPool 管理
3. Transfer → 委托给 executor/remote_executor.py (RemoteExecutor)
4. Meshing → 委托给 executor/remote_executor.py (RemoteExecutor)
5. Solver  → 委托给 executor/remote_executor.py (RemoteExecutor)

每个任务执行后会更新 StateManager 中的状态。
===============================================================================
"""
from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.scheduler.control import PipelineControl
    from engine.state_manager import StateManager

from engine.config import (
    DEFAULT_WORKSTATION_ID, LOCAL_PATHS,
    STATUS_ERROR, get_step_filename,
    get_workstation_config,
    is_server_mode,
)
from engine.sc_process_pool import SCProcessPool
from executor.sw_executor import SWExecutor
from executor.remote_executor import RemoteExecutor
from executor.cleaner import FileCleaner
from utils.logger import setup_logger
from utils.ssh_client import RemoteWorkstation

logger = setup_logger(__name__)


class TaskRunner:
    """
    任务执行器（协调者）。

    保持 SSH 连接和 SCProcessPool，将具体执行逻辑委托给子模块。
    """

    def __init__(self, state_manager: StateManager, local_worker_adapter=None):
        """初始化任务执行器。

        Args:
            state_manager: StateManager 实例，用于读写任务状态
        """
        self.state = state_manager
        self._ssh: RemoteWorkstation | None = None
        self._ssh_lock = threading.RLock()
        self._ssh_pool: dict[str, RemoteWorkstation] = {}
        self._ssh_locks: dict[str, threading.RLock] = {
            DEFAULT_WORKSTATION_ID: self._ssh_lock,
        }

        self._sc_pool = SCProcessPool()

        self._paused_event: threading.Event | None = None
        self._stopped_event: threading.Event | None = None
        self._pipeline_control: PipelineControl | None = None
        self._local_worker_adapter = local_worker_adapter

        # ---- 子执行器 ----
        self._sw_executor = SWExecutor(self.state)
        self._remote_executor = RemoteExecutor(
            self.state,
            ssh_getter=self.get_ssh,
            ssh_lock=self._ssh_lock,
        )
        self._remote_executor.restore_remote_tasks_from_db()
        self._cleaner = FileCleaner(
            self.state,
            ssh_getter=self.get_ssh,
            ssh_lock=self._ssh_lock,
        )

    def set_control_events(
        self,
        paused_event: threading.Event,
        stopped_event: threading.Event,
    ) -> None:
        """注入调度器的暂停/停止事件。

        Args:
            paused_event: 调度器的 _paused 事件
            stopped_event: 调度器的 _stopped 事件
        """
        self._paused_event = paused_event
        self._stopped_event = stopped_event
        # 将控制事件传递给 SW 执行器，使其逐构型循环可响应 pause/stop
        self._sw_executor.set_control_events(paused_event, stopped_event)
        self._remote_executor.set_control_events(paused_event, stopped_event)

    def set_pipeline_control(self, pipeline_control: PipelineControl) -> None:
        """注入统一控制层，用于序列化外部副作用启动边界。"""
        self._pipeline_control = pipeline_control
        self._sw_executor.set_pipeline_control(pipeline_control)

    def get_remote_executor(self) -> RemoteExecutor:
        """获取远程执行器实例（公共接口，供外部模块创建 MeshingMonitor 等）。"""
        return self._remote_executor

    # ------------------------------------------------------------------
    # SSH 连接管理
    # ------------------------------------------------------------------

    def get_ssh(
        self,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> RemoteWorkstation:
        """获取（或创建）SSH 客户端实例。线程安全。"""
        lock = self._ssh_locks.setdefault(workstation_id, threading.RLock())
        with lock:
            ssh = self._ssh_pool.get(workstation_id)
            if ssh is None:
                remote_config = get_workstation_config(workstation_id)
                ssh = RemoteWorkstation(
                    host=remote_config["host"],
                    port=remote_config["port"],
                    username=remote_config["username"],
                    password=remote_config["password"],
                )
                self._ssh_pool[workstation_id] = ssh
                if workstation_id == DEFAULT_WORKSTATION_ID:
                    self._ssh = ssh
            if not ssh.is_connected():
                if not ssh.connect():
                    logger.error("SSH 重连失败: %s", workstation_id)
            return ssh

    def disconnect_ssh(self, workstation_id: str | None = None) -> None:
        """断开 SSH 连接。"""
        ssh_pool = getattr(self, "_ssh_pool", None)
        ssh_locks = getattr(self, "_ssh_locks", None)
        if ssh_pool is None or ssh_locks is None:
            with self._ssh_lock:
                if self._ssh:
                    self._ssh.disconnect()
                    self._ssh = None
            return

        if workstation_id is not None:
            lock = ssh_locks.setdefault(workstation_id, threading.RLock())
            with lock:
                ssh = ssh_pool.pop(workstation_id, None)
                if ssh:
                    ssh.disconnect()
                if workstation_id == DEFAULT_WORKSTATION_ID:
                    self._ssh = None
            return

        with self._ssh_lock:
            legacy_ssh = self._ssh
            if legacy_ssh:
                legacy_ssh.disconnect()
                self._ssh = None
            default_pooled = ssh_pool.pop(DEFAULT_WORKSTATION_ID, None)
            if default_pooled and default_pooled is not legacy_ssh:
                default_pooled.disconnect()

        for current_id in list(ssh_pool):
            lock = ssh_locks.setdefault(current_id, threading.RLock())
            with lock:
                pooled = ssh_pool.pop(current_id, None)
                if pooled:
                    pooled.disconnect()

    def _workstation_for_config(self, config_name: int) -> str:
        """Return assigned workstation for a config, falling back to legacy default."""
        get_config_workstation = getattr(self.state, "get_config_workstation", None)
        if callable(get_config_workstation):
            workstation_id = get_config_workstation(config_name)
            if workstation_id:
                return str(workstation_id)
        return DEFAULT_WORKSTATION_ID

    # ------------------------------------------------------------------
    # 阶段 1: SolidWorks STEP 导出（委托给 SWExecutor）
    # ------------------------------------------------------------------

    def execute_sw_step(self) -> bool:
        """执行 SW 步骤（委托给 SWExecutor）。"""
        if self._should_delegate_local_steps():
            return bool(self._local_worker_adapter.execute_sw_step())
        return self._sw_executor.execute_sw_step()

    def execute_sw_per_config(self, config_name: int) -> bool:
        """执行单个构型的 SW STEP 导出（委托给 SWExecutor）。"""
        if self._should_delegate_local_steps():
            return bool(self._local_worker_adapter.execute_sw_per_config(config_name))
        return self._sw_executor.export_sw_per_config(config_name)

    def is_sw_in_flight(self, config_name: int | None = None) -> bool:
        """Return True while a delegated SW LocalWorker task is pending or running."""
        if not self._should_delegate_local_steps():
            return False
        has_active_task = getattr(self._local_worker_adapter, "has_active_task", None)
        return bool(callable(has_active_task) and has_active_task("sw", config_name))

    def shutdown_sw_processes(self) -> None:
        """全量清理 SolidWorks 进程。"""
        if self._should_delegate_local_steps():
            return
        self._sw_executor.shutdown_all()

    def do_sw_first_cleanup(self) -> None:
        """首次 SW 全体清理（进入 SW 阶段前调用）。"""
        if self._should_delegate_local_steps():
            return
        self._sw_executor.do_first_cleanup()

    def do_sw_final_cleanup(self) -> None:
        """末次 SW 全体清理（SW 阶段全部完成后调用）。"""
        if self._should_delegate_local_steps():
            return
        self._sw_executor.do_final_cleanup()

    def reset_sw_cleanup(self) -> None:
        """重置 SW 全量清理状态。"""
        if self._should_delegate_local_steps():
            return
        self._sw_executor.reset_cleanup_state()

    def disconnect_sw_cached(self) -> None:
        """清理 SW 单构型导出缓存连接。"""
        if self._should_delegate_local_steps():
            return
        self._sw_executor.disconnect_sw_cached()

    def verify_step_exports(self, step_dir: str) -> int:
        """执行 SW STEP 导出安全网校验。"""
        if self._should_delegate_local_steps():
            return sum(
                1
                for config_name in self.state.get_all_configs()
                if self.state.get_step_status(config_name, "sw") == "Completed"
            )
        return self._sw_executor._verify_step_exports(step_dir)

    # ------------------------------------------------------------------
    # 阶段 2: SpaceClaim 脚本执行（本地 SCProcessPool）
    # ------------------------------------------------------------------

    def execute_sc_step(self, config_name: int) -> bool:
        """执行 SC 步骤（SCProcessPool）。"""
        if self._should_delegate_local_steps():
            return bool(self._local_worker_adapter.execute_sc_step(config_name))
        sw_step_name = get_step_filename("sw", config_name)
        if not sw_step_name:
            logger.error("无法生成 STEP 文件名：STEP_FILE_PATTERNS['sw'] 未配置或格式错误")
            self.state.set_step_status(config_name, "sc", STATUS_ERROR, "STEP 文件名配置错误")
            return False
        scdoc_name = get_step_filename("sc", config_name)
        if not scdoc_name:
            logger.error("无法生成 SCDOC 文件名：STEP_FILE_PATTERNS['sc'] 未配置或格式错误")
            self.state.set_step_status(config_name, "sc", STATUS_ERROR, "SCDOC 文件名配置错误")
            return False

        step_dir = LOCAL_PATHS["step_dir"]
        scdoc_dir = LOCAL_PATHS["scdoc_dir"]
        step_file = os.path.join(step_dir, sw_step_name)

        os.makedirs(scdoc_dir, exist_ok=True)

        if not os.path.exists(step_file):
            logger.error(f"STEP 文件不存在: {step_file}")
            self.state.set_step_status(config_name, "sc", STATUS_ERROR, "STEP 文件不存在")
            return False

        sc_exe = LOCAL_PATHS["sc_exe"]
        sc_script = LOCAL_PATHS["sc_script"]
        if not os.path.exists(sc_exe):
            logger.error(f"SpaceClaim 可执行文件不存在: {sc_exe}")
            self.state.set_step_status(config_name, "sc", STATUS_ERROR, "SC 程序不存在")
            return False
        if not os.path.exists(sc_script):
            logger.error(f"SC 脚本文件不存在: {sc_script}")
            self.state.set_step_status(config_name, "sc", STATUS_ERROR, "SC 脚本不存在")
            return False

        return self._sc_pool.run_config(
            config_name,
            paused_event=self._paused_event,
            stopped_event=self._stopped_event,
            pipeline_control=self._pipeline_control,
        )

    # ------------------------------------------------------------------
    # 阶段 3: 文件传输（委托给 RemoteExecutor）
    # ------------------------------------------------------------------

    def execute_transfer(self, config_name: int) -> bool:
        """执行 Transfer 步骤（委托给 RemoteExecutor）。"""
        return self._remote_executor.execute_transfer(
            config_name,
            workstation_id=self._workstation_for_config(config_name),
        )

    def _should_delegate_local_steps(self) -> bool:
        """Return True when local Windows-only steps should run via LocalWorker."""
        return is_server_mode() and getattr(self, "_local_worker_adapter", None) is not None

    # ------------------------------------------------------------------
    # 阶段 4: 网格划分（委托给 RemoteExecutor）
    # ------------------------------------------------------------------

    def execute_meshing(self, config_name: int) -> bool:
        """执行 Meshing 步骤（委托给 RemoteExecutor）。"""
        return self._remote_executor.execute_meshing(
            config_name,
            workstation_id=self._workstation_for_config(config_name),
        )

    def wait_meshing_completion(
        self, config_name: int,
        paused_event: threading.Event | None = None,
        stopped_event: threading.Event | None = None,
    ) -> bool:
        """等待网格划分完成（委托给 RemoteExecutor）。"""
        return self._remote_executor.wait_meshing_completion(
            config_name,
            paused_event,
            stopped_event,
            workstation_id=self._workstation_for_config(config_name),
        )

    # ------------------------------------------------------------------
    # 阶段 5: 仿真求解（委托给 RemoteExecutor）
    # ------------------------------------------------------------------

    def execute_solver(self, config_name: int) -> bool:
        """执行 Solver 步骤（委托给 RemoteExecutor）。"""
        return self._remote_executor.execute_solver(
            config_name,
            workstation_id=self._workstation_for_config(config_name),
        )

    def wait_solver_completion(
        self, config_name: int,
        paused_event: threading.Event | None = None,
        stopped_event: threading.Event | None = None,
    ) -> bool:
        """等待求解完成（委托给 RemoteExecutor）。"""
        return self._remote_executor.wait_solver_completion(
            config_name,
            paused_event,
            stopped_event,
            workstation_id=self._workstation_for_config(config_name),
        )

    # ------------------------------------------------------------------
    # 系统自检 & 文件清理（委托给 FileCleaner）
    # ------------------------------------------------------------------

    def run_system_check(self) -> dict:
        """执行系统自检（委托给 FileCleaner）。"""
        result = self._cleaner.run_system_check()
        if self._should_delegate_local_steps():
            local_worker_result = self._local_worker_adapter.check_local_environment()
            if local_worker_result is None:
                result["local_worker_checks"] = {
                    "status": {
                        "exists": False,
                        "message": "LocalWorker 未在线或自检失败",
                    }
                }
            else:
                result["local_worker_checks"] = dict(
                    local_worker_result.get("local_checks", local_worker_result)
                )
        return result

    def run_local_system_check(self) -> dict:
        """执行 LocalWorker 本地系统自检（委托给 FileCleaner）。"""
        return self._cleaner.run_local_system_check()

    def clean_step_files(self, step_name, config_name=None):
        """清理步骤文件（委托给 FileCleaner）。"""
        if self._should_delegate_local_steps() and step_name in {"all", "sw", "sc"}:
            ok = self._local_worker_adapter.clean_local_files(step_name, config_name)
            if not ok:
                raise RuntimeError("LocalWorker 本地文件清理失败")
        self._cleaner.clean_step_files(step_name, config_name)

    def clean_local_step_files(self, step_name, config_name=None):
        """清理 LocalWorker 本地步骤文件（委托给 FileCleaner）。"""
        self._cleaner.clean_local_step_files(step_name, config_name)

    def clean_all_cache(self) -> None:
        """清理远程工作站缓存文件（委托给 FileCleaner）。"""
        self._cleaner.clean_all_cache()

    # ------------------------------------------------------------------
    # SC 进程池公共代理方法（避免外部模块直接访问 _sc_pool 私有属性）
    # ------------------------------------------------------------------

    def shutdown_sc_pool(self) -> None:
        """全量清理 SpaceClaim 进程。"""
        self._sc_pool.shutdown_all()

    def do_sc_final_cleanup(self) -> None:
        """末次 SC 全体清理（SC 阶段全部完成后调用）。"""
        self._sc_pool.do_final_cleanup()

    def reset_sc_pool(self) -> None:
        """重置 SC 进程池状态。"""
        self._sc_pool.reset()
