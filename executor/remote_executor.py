"""
===============================================================================
远程任务执行器 (Remote Executor)

负责 SCDOC 文件传输（本地→远程）以及远程工作站上的网格划分和仿真求解。

从 engine/task_runner.py 中提取。
===============================================================================
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import threading
from contextlib import AbstractContextManager
from typing import Callable, NoReturn, TYPE_CHECKING

if TYPE_CHECKING:
    from utils.ssh_client import RemoteWorkstation
    from engine.state_manager import StateManager

from engine.config import (
    DEFAULT_WORKSTATION_ID, LOCAL_PATHS, REMOTE_CONFIG, ENGINE_CONFIG,
    OPERATION_TIMEOUTS,
    STATUS_ERROR, get_step_filename, STEP_FILE_PATTERNS,
    get_workstation_config, is_server_mode,
)
from engine.scheduler.utils import wait_unless_paused_or_stopped
from executor.postprocess_paths import resolve_postprocess_paths
from utils.infrastructure import InfrastructureUnavailableError
from utils.logger import setup_logger

logger = setup_logger(__name__)


def _cmd_arg(value: object, *, force_quote: bool = False) -> str:
    """Return a cmd.exe-safe argument for the generated remote batch script."""
    text = str(value)
    if any(ch in text for ch in ('"', "\r", "\n")):
        raise ValueError(f"远程命令参数包含非法字符: {text!r}")
    escaped = text.replace("%", "%%")
    needs_quote = force_quote or not escaped or any(
        ch.isspace() or ch in "&()[]{}^=;!'+,`~|<>"
        for ch in escaped
    )
    return f'"{escaped}"' if needs_quote else escaped


def _raise_infra_on_ssh_error(exc: Exception) -> "NoReturn":
    """Wrap SSH/SFTP connection errors as infrastructure exceptions.

    Call this in except blocks that catch ``(OSError, ConnectionError)`` to
    convert them into :class:`InfrastructureUnavailableError` so the scheduler
    knows the failure is not a business-logic error and should not consume the
    config retry budget.

    Raises:
        InfrastructureUnavailableError: always
    """
    raise InfrastructureUnavailableError(str(exc)) from exc


# 远程脚本文件列表（部署到 scripts_dir）
REMOTE_SCRIPT_FILES = [
    "batch_meshing_gen4.py",
    "batch_solver_gen4.py",
    "batch_postprocess_gen4.py",
    "meshing_gen4.wft",
    "meshing_gen4.jou",
    "solver_gen4.jou",
    "solver_gen4.set",
    "solver_post_gen4.jou",
    "postprocess_extra_gen4.jou",
    "postprocess_metrics_gen4.py",
    "compute_metrics_gen4.py",
    "metrics_export_gen4.jou",
]

# 远程仿真引用文件列表（部署到 ref_files_dir）
REMOTE_REF_FILES = [
    "chemkin-import_chem.inp",
    "chemkin-import_therm.dat",
    "model_gen4.fla",
    "model_gen4.pdf",
]


class RemoteExecutor:
    """远程任务执行器。

    负责 Transfer（SFTP 上传）、Meshing（远程网格）、Solver（远程求解）。
    共享同一个 SSH 连接。
    """

    def __init__(
        self,
        state_manager: StateManager,
        ssh_getter: Callable[..., "RemoteWorkstation"],
        ssh_lock: threading.RLock,
        ssh_locks: dict[str, threading.RLock] | None = None,
        ssh_locks_guard: threading.Lock | None = None,
    ):
        """初始化远程执行器。

        Args:
            state_manager: StateManager 实例
            ssh_getter: 可调用对象，返回 RemoteWorkstation 实例
            ssh_lock: SSH 连接的线程锁
            ssh_locks: 可选的按工作站锁池，与 TaskRunner/Cleaner 共享
            ssh_locks_guard: 保护共享锁池的锁
        """
        self.state = state_manager
        self._get_ssh = ssh_getter
        self._ssh_lock = ssh_lock
        self._ssh_locks = ssh_locks if ssh_locks is not None else {}
        self._ssh_locks.setdefault(DEFAULT_WORKSTATION_ID, ssh_lock)
        self._ssh_locks_guard = ssh_locks_guard or threading.Lock()
        self._paused_event: threading.Event | None = None
        self._stopped_event: threading.Event | None = None
        # 跟踪远程后台任务名称（用于超时后终止）
        self._remote_tasks: dict[object, str] = {}
        self._remote_tasks_lock = threading.RLock()
        self._sync_cache_lock = threading.Lock()
        self._last_sync_paths_lock = threading.RLock()
        self._last_successful_sync_signature: tuple[object, ...] | None = None
        self._last_successful_sync_signatures: dict[str, tuple[object, ...]] = {}
        self.last_meshing_error = ""

    # 步骤名 → 日志前缀映射（项目规范：中文消息 + 英文标签前缀）
    _STEP_LOG_PREFIX: dict[str, str] = {
        "transfer": "[Transfer]",
        "meshing": "[Meshing]",
        "solver": "[Solver]",
        "postprocess": "[PostProcess]",
    }

    @classmethod
    def _log_prefix(cls, step_name: str) -> str:
        """将步骤名转换为规范的日志前缀。"""
        return cls._STEP_LOG_PREFIX.get(step_name, f"[{step_name}]")

    @staticmethod
    def _remote_task_artifacts(task_name: str, flag_file: str) -> dict[str, str | None]:
        """根据计划任务名和 flag 路径推导 wrapper 产物路径。"""
        task_hash = (
            task_name.removeprefix("AutoFluid_")
            if task_name.startswith("AutoFluid_")
            else ""
        )
        if not task_hash:
            return {"log_file": None, "pid_file": None, "script_file": None}
        flag_dir = flag_file.rsplit("/", 1)[0] if "/" in flag_file else "."
        return {
            "log_file": f"{flag_dir}/autofluid_bg_{task_hash}.log",
            "pid_file": f"{flag_dir}/autofluid_bg_{task_hash}.pid",
            "script_file": f"{flag_dir}/autofluid_bg_{task_hash}.cmd",
        }

    @staticmethod
    def _local_scdoc_dir() -> str:
        """Return the daemon-local SCDOC source directory for Transfer."""
        explicit_scdoc_dir = os.environ.get("AUTOFLUID_SCDOC_DIR")
        if explicit_scdoc_dir:
            return explicit_scdoc_dir
        if is_server_mode():
            return os.path.join(str(LOCAL_PATHS["data_dir"]), "scdoc")
        return str(LOCAL_PATHS["scdoc_dir"])

    @staticmethod
    def _remote_task_key(
        config_name: int,
        step_name: str,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> tuple[str, int, str]:
        """Build the in-memory remote-task key."""
        return (workstation_id, config_name, step_name)

    def _get_ssh_for_workstation(
        self,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> "RemoteWorkstation":
        """Call the SSH getter with workstation support while preserving legacy default callers."""
        try:
            return self._get_ssh(workstation_id)
        except TypeError:
            if workstation_id != DEFAULT_WORKSTATION_ID:
                raise
            return self._get_ssh()

    def _ssh_guard(
        self,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> AbstractContextManager[object]:
        """Return the SSH lock that serializes operations for one workstation."""
        with self._ssh_locks_guard:
            lock = self._ssh_locks.get(workstation_id)
            if lock is None:
                lock = threading.RLock()
                self._ssh_locks[workstation_id] = lock
            return lock

    @staticmethod
    def _remote_config_for_workstation(
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> dict[str, object]:
        """Return remote paths/settings for one workstation."""
        if workstation_id == DEFAULT_WORKSTATION_ID:
            return dict(REMOTE_CONFIG)
        return dict(get_workstation_config(workstation_id))

    def _save_remote_task(
        self,
        *,
        workstation_id: str,
        config_name: int,
        step_name: str,
        task_name: str,
        flag_file: str,
        error_flag_file: str,
        log_file: str | None,
        pid_file: str | None,
        script_file: str | None,
        started_at: float,
    ) -> None:
        """Persist remote-task metadata with legacy StateManager compatibility."""
        try:
            self.state.save_remote_task(
                workstation_id=workstation_id,
                config_name=config_name,
                step_name=step_name,
                task_name=task_name,
                flag_file=flag_file,
                error_flag_file=error_flag_file,
                log_file=log_file,
                pid_file=pid_file,
                script_file=script_file,
                started_at=started_at,
            )
        except TypeError:
            if workstation_id != DEFAULT_WORKSTATION_ID:
                raise
            self.state.save_remote_task(
                config_name=config_name,
                step_name=step_name,
                task_name=task_name,
                flag_file=flag_file,
                error_flag_file=error_flag_file,
                log_file=log_file,
                pid_file=pid_file,
                script_file=script_file,
                started_at=started_at,
            )

    def _get_remote_task_from_state(
        self,
        config_name: int,
        step_name: str,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> dict[str, object] | None:
        """Read persisted remote-task metadata with legacy StateManager compatibility."""
        try:
            return self.state.get_remote_task(config_name, step_name, workstation_id)
        except TypeError:
            if workstation_id != DEFAULT_WORKSTATION_ID:
                raise
            return self.state.get_remote_task(config_name, step_name)

    def _delete_remote_task_from_state(
        self,
        config_name: int,
        step_name: str,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> None:
        """Delete persisted remote-task metadata with legacy StateManager compatibility."""
        try:
            self.state.delete_remote_task(config_name, step_name, workstation_id)
        except TypeError:
            if workstation_id != DEFAULT_WORKSTATION_ID:
                raise
            self.state.delete_remote_task(config_name, step_name)

    def _remember_remote_task(
        self,
        config_name: int,
        step_name: str,
        task_name: str,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> None:
        """Remember a remote scheduled task in memory."""
        key = self._remote_task_key(config_name, step_name, workstation_id)
        with self._remote_tasks_lock:
            self._remote_tasks[key] = task_name

    def _pop_remote_task(
        self,
        config_name: int,
        step_name: str,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> str | None:
        """Remove one remembered remote scheduled task, accepting legacy int keys."""
        with self._remote_tasks_lock:
            task_name = self._remote_tasks.pop(
                self._remote_task_key(config_name, step_name, workstation_id),
                None,
            )
            if task_name is None and workstation_id == DEFAULT_WORKSTATION_ID:
                task_name = self._remote_tasks.pop(config_name, None)
        return task_name

    def _persist_remote_task(
        self,
        *,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
        config_name: int,
        step_name: str,
        task_name: str,
        flag_file: str,
    ) -> None:
        """保存远程计划任务元数据，供 daemon 重启后恢复。"""
        artifacts = self._remote_task_artifacts(task_name, flag_file)
        self._save_remote_task(
            workstation_id=workstation_id,
            config_name=config_name,
            step_name=step_name,
            task_name=task_name,
            flag_file=flag_file,
            error_flag_file=f"{flag_file}.error",
            log_file=artifacts["log_file"],
            pid_file=artifacts["pid_file"],
            script_file=artifacts["script_file"],
            started_at=time.time(),
        )

    def restore_remote_tasks_from_db(self) -> None:
        """Daemon 重启后从数据库恢复工作站/构型/步骤 → 计划任务名映射。"""
        with self._remote_tasks_lock:
            self._remote_tasks.clear()
            for task in self.state.get_all_remote_tasks():
                workstation_id = str(task.get("workstation_id", DEFAULT_WORKSTATION_ID))
                config_name = int(str(task["config_name"]))
                step_name = str(task["step_name"])
                task_name = str(task["task_name"])
                self._remote_tasks[
                    self._remote_task_key(config_name, step_name, workstation_id)
                ] = task_name

    def forget_remote_task(
        self,
        config_name: int,
        step_name: str,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> None:
        """远程任务已在恢复扫描中判定终态时，清理本地和 DB 跟踪记录。"""
        self._pop_remote_task(config_name, step_name, workstation_id)
        self._delete_remote_task_from_state(config_name, step_name, workstation_id)

    def register_postprocess_from_solver(
        self,
        config_name: int,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> bool:
        """Persist PostProcess tracking for the same remote Solver task."""
        return self._register_postprocess_from_solver(config_name, workstation_id)

    def query_remote_task_status(
        self,
        config_name: int,
        step_name: str,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> str:
        """查询持久化远程任务当前状态。

        Returns:
            completed | failed | running | lost | unknown
        """
        task = self._get_remote_task_from_state(config_name, step_name, workstation_id)
        if task is None:
            return "lost"
        flag_file = str(task["flag_file"])
        error_flag_file = str(task["error_flag_file"])
        task_name = str(task["task_name"])

        with self._ssh_guard(workstation_id):
            try:
                ssh = self._get_ssh_for_workstation(workstation_id)
                if ssh.check_remote_file(flag_file):
                    return "completed"
                if ssh.check_remote_file(error_flag_file):
                    return "failed"
            except (OSError, ConnectionError) as e:
                _raise_infra_on_ssh_error(e)

            try:
                query_cmd = f'schtasks /Query /TN "{task_name}" /FO CSV /NH'
                _, _, exit_code = ssh.exec_command(query_cmd, timeout=30)
                if exit_code == 0:
                    pid_running = self._remote_task_pid_running(ssh, task)
                    if pid_running is False:
                        return "lost"
                    return "running"
                return "lost"
            except (OSError, ConnectionError) as e:
                _raise_infra_on_ssh_error(e)

    def _remote_task_pid_running(
        self,
        ssh: "RemoteWorkstation",
        task: dict[str, object],
    ) -> bool | None:
        """用持久化 PID 文件进一步校验远程进程是否仍存活。"""
        pid_file = task.get("pid_file")
        if not pid_file:
            return None

        read_pid = getattr(ssh, "read_remote_pid_file", None)
        if not callable(read_pid):
            return None

        pid = read_pid(str(pid_file))
        if pid is None:
            return None

        tasklist_cmd = f'tasklist /FI "PID eq {pid}" /FO CSV /NH'
        out, _, exit_code = ssh.exec_command(tasklist_cmd, timeout=30)
        if exit_code != 0:
            return None
        return re.search(rf"\b{pid}\b", out) is not None

    def _remote_task_start_time(
        self,
        config_name: int,
        step_name: str,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> float:
        """返回远程任务持久化启动时间；无记录时使用当前时间。"""
        task = self._get_remote_task_from_state(config_name, step_name, workstation_id)
        if task is None:
            return time.time()
        try:
            return float(str(task["started_at"]))
        except (KeyError, TypeError, ValueError):
            logger.warning(f"{self._log_prefix(step_name)} 构型{config_name} 远程任务 started_at 无效")
            return time.time()

    def set_control_events(
        self,
        paused_event: threading.Event,
        stopped_event: threading.Event,
    ) -> None:
        """注入调度器暂停/停止事件，使 Transfer 上传可响应控制指令。"""
        self._paused_event = paused_event
        self._stopped_event = stopped_event

    def get_ssh_connection(self) -> "RemoteWorkstation":
        """获取 SSH 连接实例（公共接口，供外部模块查询远程文件状态）。"""
        return self._get_ssh()

    # ------------------------------------------------------------------
    # 同步状态持久化
    # ------------------------------------------------------------------

    @property
    def _sync_state_path(self) -> str:
        """同步状态文件路径（记录上次同步的远程目录）。"""
        return os.path.join(str(LOCAL_PATHS["data_dir"]), "last_sync_paths.json")

    def _load_last_sync_paths(self) -> dict[str, object]:
        """加载上次同步时的远程目录路径。

        Returns:
            字典，包含 scripts_dir 和 ref_files_dir 的上次值；
            文件不存在或解析失败返回空字典
        """
        state_file = self._sync_state_path
        if not os.path.exists(state_file):
            return {}
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            return {str(key): value for key, value in data.items() if isinstance(key, str)}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"[Sync] 加载同步状态失败: {e}")
            return {}

    def _last_sync_paths_for_workstation(
        self,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> dict[str, str]:
        """Return last synced paths for one workstation."""
        with self._last_sync_paths_lock:
            data = self._load_last_sync_paths()
            nested = data.get("workstations")
            if isinstance(nested, dict):
                workstation_paths = nested.get(workstation_id)
                if isinstance(workstation_paths, dict):
                    return {
                        str(key): str(value)
                        for key, value in workstation_paths.items()
                        if isinstance(key, str) and isinstance(value, str)
                    }
            if workstation_id == DEFAULT_WORKSTATION_ID:
                return {
                    str(key): str(value)
                    for key, value in data.items()
                    if key in {"scripts_dir", "ref_files_dir"} and isinstance(value, str)
                }
            return {}

    def _save_last_sync_paths(
        self,
        remote_config: dict[str, object] | None = None,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> None:
        """将当前远程目录配置保存为下次同步的比对基准。"""
        config = remote_config or self._remote_config_for_workstation(workstation_id)
        paths = {
            "scripts_dir": str(config["scripts_dir"]),
            "ref_files_dir": str(config["ref_files_dir"]),
        }
        with self._last_sync_paths_lock:
            state = self._load_last_sync_paths()
            nested = state.get("workstations")
            if not isinstance(nested, dict):
                nested = {}
            nested[workstation_id] = paths
            state["workstations"] = nested
            if workstation_id == DEFAULT_WORKSTATION_ID:
                state.update(paths)
            try:
                with open(self._sync_state_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
                logger.debug(f"[Sync] 已保存同步状态: {state}")
            except OSError as e:
                logger.error(f"[Sync] 保存同步状态失败: {e}")

    def _cleanup_remote_files(
        self,
        remote_dir: str,
        filenames: list[str],
        label: str,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> None:
        """清理旧远程目录中我们上传过的已知文件。

        逐个删除文件，忽略不存在或删除失败的情况。
        不删除目录本身，避免误伤共享目录中的其他文件。

        Args:
            remote_dir: 旧的远程目录路径
            filenames: 需要清理的文件名列表
            label: 日志标签（如 "脚本"、"引用文件"）
        """
        logger.info(f"[Sync] 检测到{label}远程目录变更，清理旧路径: {remote_dir}")
        with self._ssh_guard(workstation_id):
            try:
                ssh = self._get_ssh_for_workstation(workstation_id)
                for filename in filenames:
                    remote_path = f"{remote_dir}/{filename}".replace("\\", "/")
                    try:
                        ssh.delete_remote_file(remote_path)
                    except Exception as e:
                        logger.debug(f"[Sync] 清理旧文件跳过 {remote_path}: {e}")
            except (OSError, ConnectionError) as e:
                logger.warning(f"[Sync] 清理旧路径文件异常（不影响后续上传）: {e}")

    # ------------------------------------------------------------------
    # 文件传输
    # ------------------------------------------------------------------

    def execute_transfer(
        self,
        config_name: int,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> bool:
        """通过 SFTP 将 SCDOC 文件上传到远程工作站。

        内部先检查远程文件是否已存在且大小与本地文件一致，若一致则直接
        返回成功（断点续传场景），避免重复上传。
        所有 SSH/SFTP 操作均在 _ssh_lock 保护下执行，保证线程安全。
        """
        _scdoc_name = get_step_filename("sc", config_name)
        if not _scdoc_name:
            logger.error("[Transfer] 无法生成 SCDOC 文件名：STEP_FILE_PATTERNS['sc'] 未配置或格式错误")
            self.state.set_step_status(config_name, "transfer", STATUS_ERROR, "SCDOC 文件名配置错误")
            return False
        local_file = os.path.join(
            self._local_scdoc_dir(),
            _scdoc_name,
        )
        remote_config = self._remote_config_for_workstation(workstation_id)
        remote_file = os.path.join(
            str(remote_config["scdoc_dir"]),
            _scdoc_name,
        ).replace("\\", "/")

        if not os.path.exists(local_file):
            logger.error(f"[Transfer] 本地 SCDOC 文件不存在: {local_file}")
            self.state.set_step_status(config_name, "transfer", STATUS_ERROR, "本地文件不存在")
            return False
        local_size = os.path.getsize(local_file)
        if local_size <= 0:
            logger.error(f"[Transfer] 本地 SCDOC 文件为空: {local_file}")
            self.state.set_step_status(config_name, "transfer", STATUS_ERROR, "本地 SCDOC 文件为空")
            return False

        if self._stopped_event is not None and self._stopped_event.is_set():
            logger.info(f"[Transfer] 构型{config_name} 因停止取消（未开始上传）")
            return False
        if self._paused_event is not None and self._paused_event.is_set():
            logger.info(f"[Transfer] 构型{config_name} 因暂停暂缓（未开始上传）")
            return False

        transfer_timeout = float(ENGINE_CONFIG["transfer_timeout"])
        transfer_deadline = time.monotonic() + transfer_timeout

        def _remaining_transfer_timeout() -> float:
            return max(0.0, transfer_deadline - time.monotonic())

        def _fail_transfer_timeout() -> bool:
            logger.error(f"[Transfer] 构型{config_name} 文件传输超时 ({transfer_timeout:g}s)")
            self.state.set_step_status(config_name, "transfer", STATUS_ERROR, "文件传输超时")
            return False

        with self._ssh_guard(workstation_id):
            try:
                ssh = self._get_ssh_for_workstation(workstation_id)

                remote_check_timeout = _remaining_transfer_timeout()
                if remote_check_timeout <= 0:
                    return _fail_transfer_timeout()

                # ★ 远程文件存在性检查（断点续传）：在锁内执行，保证线程安全。
                #    原检查位于 worker_pool._process_transfer_step() 中且未持有
                #    _ssh_lock，与 upload_file() 并发操作同一 SFTP 通道导致死锁。
                try:
                    try:
                        remote_size = ssh.get_remote_file_size(
                            remote_file,
                            timeout=remote_check_timeout,
                        )
                    except TypeError:
                        remote_size = ssh.get_remote_file_size(remote_file)
                    if remote_size == local_size:
                        logger.info(
                            f"[Transfer] 远程 SCDOC 已存在 ({remote_size} bytes)，"
                            f"构型{config_name} 跳过上传"
                        )
                        return True
                    if remote_size is not None and remote_size > 0:
                        logger.info(
                            f"[Transfer] 远程 SCDOC 大小不一致 "
                            f"(remote={remote_size}, local={local_size})，"
                            f"构型{config_name} 将重新上传"
                        )
                except Exception as e:
                    # 远程检查失败不影响后续上传流程（可能是临时网络问题）
                    logger.debug(
                        f"[Transfer] 构型{config_name} 远程文件检查异常"
                        f"（将继续上传）: {e}"
                    )

                upload_timeout = _remaining_transfer_timeout()
                if upload_timeout <= 0:
                    return _fail_transfer_timeout()

                upload_max_retries = self._upload_max_retries()
                success = ssh.upload_file(
                    local_file,
                    remote_file,
                    max_retries=upload_max_retries,
                    timeout=upload_timeout,
                    paused_event=self._paused_event,
                    stopped_event=self._stopped_event,
                )
                if success:
                    logger.info(f"[Transfer] 文件传输完成: 构型{config_name}")
                    return True
                else:
                    ssh.delete_remote_file(remote_file)
                    if self._stopped_event is not None and self._stopped_event.is_set():
                        logger.info(f"[Transfer] 构型{config_name} 上传因停止中断")
                        return False
                    if self._paused_event is not None and self._paused_event.is_set():
                        logger.info(f"[Transfer] 构型{config_name} 上传因暂停中断")
                        return False
                    raise InfrastructureUnavailableError(
                        f"SFTP 上传失败（构型{config_name}）"
                    )
            except InfrastructureUnavailableError:
                raise
            except (OSError, ConnectionError) as e:
                _raise_infra_on_ssh_error(e)

    # ------------------------------------------------------------------
    # 网格划分
    # ------------------------------------------------------------------

    def _upload_max_retries(self) -> int:
        """返回 SFTP 上传重试次数，配置异常时回退到默认值。"""
        default_retries = 3
        raw_retries = OPERATION_TIMEOUTS.get("ssh_upload_max_retries", default_retries)
        try:
            retries = int(raw_retries)
        except (TypeError, ValueError):
            logger.warning(
                f"[Transfer] ssh_upload_max_retries 配置无效: {raw_retries!r}，"
                f"回退为 {default_retries}"
            )
            return default_retries

        if retries < 1:
            logger.warning(
                f"[Transfer] ssh_upload_max_retries 必须 >= 1，当前为 {retries}，"
                f"回退为 {default_retries}"
            )
            return default_retries
        return retries

    def _meshing_processor_count(self) -> int:
        """返回 Fluent Meshing 启动核心数，配置异常时回退到保守默认值。"""
        default_count = 8
        raw_count = ENGINE_CONFIG.get("meshing_processor_count", default_count)
        try:
            processor_count = int(raw_count)
        except (TypeError, ValueError):
            logger.warning(
                f"[Meshing] meshing_processor_count 配置无效: {raw_count!r}，"
                f"使用默认值 {default_count}"
            )
            return default_count
        if processor_count < 1:
            logger.warning(
                f"[Meshing] meshing_processor_count 必须大于 0，"
                f"当前为 {processor_count}，使用默认值 {default_count}"
            )
            return default_count
        return processor_count

    @staticmethod
    def _validate_config_name(config_name: int) -> None:
        """校验构型编号，防止绕过 public API 后拼接进远程命令。"""
        if type(config_name) is not int:
            raise ValueError(f"构型名称必须是整数，当前为 {type(config_name).__name__}")

    def _meshing_flag_file(
        self,
        config_name: int,
        remote_config: dict[str, object] | None = None,
    ) -> str:
        """返回 Meshing 完成标志文件路径。"""
        self._validate_config_name(config_name)
        config = remote_config or self._remote_config_for_workstation()
        return f"{config['flag_dir']}/meshing_done_{config_name}.txt".replace("\\", "/")

    def _meshing_mesh_file(
        self,
        config_name: int,
        remote_config: dict[str, object] | None = None,
    ) -> str | None:
        """返回 Meshing 输出网格文件路径。"""
        self._validate_config_name(config_name)
        mesh_name = get_step_filename("meshing", config_name)
        if not mesh_name:
            return None
        config = remote_config or self._remote_config_for_workstation()
        msh_dir = str(config["msh_dir"]).replace("\\", "/")
        return f"{msh_dir}/{mesh_name}"

    def _solver_flag_file(
        self,
        config_name: int,
        remote_config: dict[str, object] | None = None,
    ) -> str:
        """返回 Solver 完成标志文件路径。"""
        self._validate_config_name(config_name)
        config = remote_config or self._remote_config_for_workstation()
        return f"{config['flag_dir']}/solver_done_{config_name}.txt".replace("\\", "/")

    def _solver_progress_file(
        self,
        config_name: int,
        remote_config: dict[str, object] | None = None,
    ) -> str:
        """返回 Solver 剩余时间进度文件路径。"""
        self._validate_config_name(config_name)
        config = remote_config or self._remote_config_for_workstation()
        return f"{config['flag_dir']}/solver_progress_{config_name}.json".replace("\\", "/")

    def _postprocess_flag_file(
        self,
        config_name: int,
        remote_config: dict[str, object] | None = None,
    ) -> str:
        """返回 PostProcess 完成标志文件路径。"""
        self._validate_config_name(config_name)
        config = remote_config or self._remote_config_for_workstation()
        pattern = STEP_FILE_PATTERNS.get("postprocess", "postprocess_done_{config}.txt")
        filename = str(pattern).format(config=config_name)
        return f"{config['flag_dir']}/{filename}".replace("\\", "/")

    def _build_meshing_command(
        self,
        config_name: int,
        remote_config: dict[str, object] | None = None,
    ) -> tuple[str, str]:
        """构建远程网格划分命令和标志文件路径。

        Args:
            config_name: 构型名称

        Returns:
            (command, flag_file) 元组
        """
        config = remote_config or self._remote_config_for_workstation()
        flag_file = self._meshing_flag_file(config_name, config)
        conda_env = config["conda_env"]
        conda_exe = config["conda_exe"]
        scripts_dir = config["scripts_dir"]
        scdoc_name = get_step_filename("sc", config_name)
        if not scdoc_name:
            raise ValueError("无法生成 SCDOC 文件名")
        processor_count = self._meshing_processor_count()

        # 构建参数化命令（所有路径均为必需参数，无默认值）
        command = " ".join([
            _cmd_arg(conda_exe, force_quote=True),
            "run",
            "--no-capture-output",
            "-n",
            _cmd_arg(conda_env),
            "python",
            "-u",
            _cmd_arg(f"{scripts_dir}/batch_meshing_gen4.py", force_quote=True),
            str(config_name),
            "--mpi-bin-dir",
            _cmd_arg(config["mpi_bin_dir"], force_quote=True),
            "--fluent-path",
            _cmd_arg(config["fluent_path"], force_quote=True),
            "--workflow-path",
            _cmd_arg(f"{scripts_dir}/meshing_gen4.wft", force_quote=True),
            "--journal-path",
            _cmd_arg(f"{scripts_dir}/meshing_gen4.jou", force_quote=True),
            "--scdoc-dir",
            _cmd_arg(config["scdoc_dir"], force_quote=True),
            "--scdoc-name",
            _cmd_arg(scdoc_name, force_quote=True),
            "--output-dir",
            _cmd_arg(config["msh_dir"], force_quote=True),
            "--working-dir",
            _cmd_arg(config["working_dir"], force_quote=True),
            "--processor-count",
            str(processor_count),
        ])
        return command, flag_file

    def _run_meshing_command(
        self,
        config_name: int,
        log_prefix: str = "[Meshing]",
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> bool:
        """启动远程网格划分后台任务（内部方法）。

        统一处理参数校验、脚本同步、命令构建和 SSH 执行。
        由 execute_meshing() 和 start_meshing() 委托调用。

        Args:
            config_name: 构型名称
            log_prefix: 日志前缀（如 "[MeshingMonitor]"）

        Returns:
            True 表示后台任务启动成功
        """
        remote_config = self._remote_config_for_workstation(workstation_id)
        if not self.sync_scripts(workstation_id=workstation_id):
            logger.error(f"{log_prefix} 远程脚本同步失败，无法启动网格划分")
            return False

        try:
            command, flag_file = self._build_meshing_command(config_name, remote_config)
        except ValueError as e:
            logger.error(f"{log_prefix} 远程网格划分命令构建失败: {e}")
            return False

        logger.info(f"{log_prefix} 启动远程网格划分: 构型{config_name}")
        logger.debug(f"{log_prefix} 远程命令: {command}")

        with self._ssh_guard(workstation_id):
            try:
                ssh = self._get_ssh_for_workstation(workstation_id)
                success, task_name = ssh.exec_background(
                    command, flag_file,
                    working_dir=str(remote_config["working_dir"]),
                    interactive=True,
                )
                if success:
                    self._remember_remote_task(
                        config_name,
                        "meshing",
                        task_name,
                        workstation_id,
                    )
                    self._persist_remote_task(
                        workstation_id=workstation_id,
                        config_name=config_name,
                        step_name="meshing",
                        task_name=task_name,
                        flag_file=flag_file,
                    )
                    logger.info(f"{log_prefix} 网格划分后台任务已启动: 构型{config_name}")
                else:
                    logger.error(f"{log_prefix} 网格划分远程任务启动失败: 构型{config_name}")
                return success
            except InfrastructureUnavailableError:
                raise
            except (OSError, ConnectionError) as e:
                _raise_infra_on_ssh_error(e)

    def execute_meshing(
        self,
        config_name: int,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> bool:
        """在远程工作站启动网格划分后台任务。

        注意：本方法仅返回成功/失败，不设置步骤状态。
        状态由调用方（RetryManager / MeshingMonitor）统一管理。
        """
        if workstation_id == DEFAULT_WORKSTATION_ID:
            return self.start_meshing(config_name)
        return self.start_meshing(config_name, workstation_id=workstation_id)

    def start_meshing(
        self,
        config_name: int,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> bool:
        """启动远程网格划分后台任务（不设置状态错误，由调用方处理）。

        用于 MeshingMonitor，启动失败时返回 False 由调用方决定重试策略。
        """
        try:
            self._validate_config_name(config_name)
        except ValueError:
            logger.error(f"[Meshing] 无效的构型名称类型: {type(config_name).__name__}")
            return False
        if workstation_id == DEFAULT_WORKSTATION_ID:
            return self._run_meshing_command(config_name)
        return self._run_meshing_command(
            config_name,
            workstation_id=workstation_id,
        )

    def check_meshing_done(
        self,
        config_name: int,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> bool:
        """检查网格划分是否已完成（标志文件是否存在）。

        若标志文件存在则清理并返回 True。使用短暂 SSH 锁。
        """
        remote_config = self._remote_config_for_workstation(workstation_id)
        try:
            flag_file = self._meshing_flag_file(config_name, remote_config)
        except ValueError:
            logger.error(f"[Meshing] 无效的构型名称类型: {type(config_name).__name__}")
            return False
        try:
            with self._ssh_guard(workstation_id):
                ssh = self._get_ssh_for_workstation(workstation_id)
                if ssh.check_remote_file(flag_file):
                    logger.info(f"[Meshing] 构型{config_name} 网格划分完成（检测到标志文件）")
                    ssh.delete_remote_file(flag_file)
                    self._cleanup_completed_remote_task(
                        config_name,
                        "meshing",
                        ssh,
                        workstation_id,
                    )
                    return True
            return False
        except InfrastructureUnavailableError:
            raise
        except (OSError, ConnectionError) as e:
            _raise_infra_on_ssh_error(e)

    def check_meshing_outputs_exist(
        self,
        config_name: int,
        *,
        timeout: float | None = None,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> bool:
        """在 SSH 锁内检查 Meshing 网格文件是否已存在。"""
        remote_config = self._remote_config_for_workstation(workstation_id)
        try:
            mesh_file = self._meshing_mesh_file(config_name, remote_config)
        except ValueError:
            logger.error(f"[Meshing] 无效的构型名称类型: {type(config_name).__name__}")
            return False

        def _check_remote_file(ssh: "RemoteWorkstation", remote_path: str) -> bool:
            if timeout is None:
                return bool(ssh.check_remote_file(remote_path))
            try:
                return bool(ssh.check_remote_file(remote_path, timeout=timeout))
            except TypeError:
                return bool(ssh.check_remote_file(remote_path))

        try:
            with self._ssh_guard(workstation_id):
                ssh = self._get_ssh_for_workstation(workstation_id)
                return mesh_file is not None and _check_remote_file(ssh, mesh_file)
        except InfrastructureUnavailableError:
            raise
        except (OSError, ConnectionError) as e:
            _raise_infra_on_ssh_error(e)

    def wait_meshing_completion(
        self, config_name: int,
        paused_event: threading.Event | None = None,
        stopped_event: threading.Event | None = None,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> bool:
        """轮询等待网格划分完成（逐次短暂持 SSH 锁，不在整个等待期间持锁）。

        暂停期间冻结超时计时器，防止恢复运行后立即触发超时。
        """
        remote_config = self._remote_config_for_workstation(workstation_id)
        try:
            flag_file = self._meshing_flag_file(config_name, remote_config)
            mesh_file = self._meshing_mesh_file(config_name, remote_config)
        except ValueError:
            logger.error(f"[Meshing] 无效的构型名称类型: {type(config_name).__name__}")
            return False
        error_flag = f"{flag_file}.error"
        timeout = ENGINE_CONFIG["meshing_timeout"]
        poll_interval = 10
        file_grace_period = 60
        done_flag_seen_time: float | None = None
        start_time = self._remote_task_start_time(
            config_name,
            "meshing",
            workstation_id,
        )
        self.last_meshing_error = ""

        logger.info(f"[Meshing] 开始轮询构型{config_name} 网格划分状态 (超时: {timeout}s)")

        while time.time() - start_time < timeout:
            # ★ 使用统一的暂停等待函数，替代手写内联循环
            if paused_event is not None:
                pause_start = time.time()
                if not wait_unless_paused_or_stopped(paused_event, stopped_event or threading.Event()):
                    logger.info(f"[Meshing] 等待构型{config_name} 期间收到停止指令")
                    return False
                # 暂停补偿：将超时计时器向后推移暂停时长
                pause_duration = time.time() - pause_start
                if pause_duration > 0:
                    start_time += pause_duration
            if stopped_event is not None and stopped_event.is_set():
                logger.info(f"[Meshing] 等待构型{config_name} 期间收到停止指令")
                return False

            try:
                with self._ssh_guard(workstation_id):
                    ssh = self._get_ssh_for_workstation(workstation_id)
                    if ssh.check_remote_file(error_flag):
                        error_summary = self._read_remote_task_error_summary(
                            ssh,
                            config_name,
                            "meshing",
                            workstation_id,
                            error_flag,
                        )
                        self.last_meshing_error = error_summary
                        logger.error(
                            f"[Meshing] 构型{config_name} 网格划分远程任务执行失败: "
                            f"{error_summary}"
                        )
                        ssh.delete_remote_file(error_flag)
                        self._cleanup_completed_remote_task(
                            config_name,
                            "meshing",
                            ssh,
                            workstation_id,
                        )
                        return False
                    if ssh.check_remote_file(flag_file):
                        mesh_exists = (
                            mesh_file is not None
                            and ssh.check_remote_file(mesh_file)
                        )
                        if mesh_exists:
                            logger.info(f"[Meshing] 构型{config_name} 网格划分完成")
                            ssh.delete_remote_file(flag_file)
                            self._cleanup_completed_remote_task(
                                config_name,
                                "meshing",
                                ssh,
                                workstation_id,
                            )
                            return True

                        if done_flag_seen_time is None:
                            done_flag_seen_time = time.time()
                            logger.warning(
                                f"[Meshing] 构型{config_name}: 标志文件已存在但缺少 "
                                f"{mesh_file or '网格文件'}，等待 {file_grace_period}s"
                            )

                        if time.time() - done_flag_seen_time > file_grace_period:
                            logger.error(
                                f"[Meshing] 构型{config_name}: 网格文件 "
                                f"{file_grace_period}s 内未生成，判定为导出错误"
                            )
                            ssh.delete_remote_file(flag_file)
                            self._cleanup_completed_remote_task(
                                config_name,
                                "meshing",
                                ssh,
                                workstation_id,
                            )
                            return False
            except InfrastructureUnavailableError:
                raise
            except (OSError, ConnectionError) as e:
                _raise_infra_on_ssh_error(e)

            time.sleep(poll_interval)

        logger.error(f"[Meshing] 构型{config_name} 网格划分超时 ({timeout}s)")
        self.last_meshing_error = f"网格划分超时 ({timeout}s)"
        # 超时后终止远程进程，防止资源泄漏和重试冲突
        self._kill_remote_task_for_config(config_name, "meshing", workstation_id)
        return False

    def _read_remote_task_error_summary(
        self,
        ssh: "RemoteWorkstation",
        config_name: int,
        step_name: str,
        workstation_id: str,
        error_flag: str,
    ) -> str:
        """Return a compact remote task error summary before cleanup removes markers."""
        read_text = getattr(ssh, "read_remote_text_file", None)
        if not callable(read_text):
            return "远程任务失败（当前 SSH 客户端不支持读取错误详情）"

        parts: list[str] = []
        try:
            error_text = read_text(error_flag, timeout=5)
            if error_text:
                parts.append(error_text.strip())
        except (OSError, ConnectionError) as exc:
            parts.append(f"读取错误标志失败: {exc}")

        task = self._get_remote_task_from_state(config_name, step_name, workstation_id)
        log_file = str((task or {}).get("log_file") or "")
        if log_file:
            try:
                log_text = read_text(log_file, timeout=5)
                if log_text:
                    parts.append(log_text.strip())
            except (OSError, ConnectionError) as exc:
                parts.append(f"读取远程任务日志失败: {exc}")

        summary = "\n".join(part for part in parts if part).strip()
        if not summary:
            return "远程任务失败（未读取到错误详情）"
        lines = [line.strip() for line in summary.splitlines() if line.strip()]
        tail = "\n".join(lines[-8:])
        return tail[-1200:]

    # ------------------------------------------------------------------
    # 仿真求解
    # ------------------------------------------------------------------

    def _solver_processor_count(self) -> int:
        """返回 Fluent Solver 启动核心数，配置异常时回退到默认值。"""
        default_count = 128
        raw_count = ENGINE_CONFIG.get("solver_processor_count", default_count)
        try:
            processor_count = int(raw_count)
        except (TypeError, ValueError):
            logger.warning(
                f"[Solver] solver_processor_count 配置无效: {raw_count!r}，"
                f"使用默认值 {default_count}"
            )
            return default_count
        if processor_count < 1:
            logger.warning(
                f"[Solver] solver_processor_count 必须大于 0，"
                f"当前为 {processor_count}，使用默认值 {default_count}"
            )
            return default_count
        return processor_count

    @staticmethod
    def _postprocess_path_config(
        config: dict[str, object],
        *,
        allow_config_override: bool = True,
    ) -> dict[str, str]:
        """Resolve workstation-local postprocess export directories."""
        paths = resolve_postprocess_paths(
            config,
            allow_config_override=allow_config_override,
            metrics_fallback_to_output_subdir=True,
        )
        return {
            "output_dir": paths["output_dir"].replace("/", "\\"),
            "animation_dir": paths["animation_dir"].replace("/", "\\"),
            "metrics_dir": paths["metrics_dir"].replace("/", "\\"),
        }

    @staticmethod
    def _postprocess_metric_constants() -> dict[str, str]:
        return {
            "exit_to_throat_area_ratio": str(
                ENGINE_CONFIG.get("postprocess_exit_to_throat_area_ratio", 7.427276607)
            ),
            "cstar_reference": str(
                ENGINE_CONFIG.get("postprocess_cstar_reference", 1830.4)
            ),
        }

    def _build_solver_command(
        self,
        config_name: int,
        remote_config: dict[str, object] | None = None,
    ) -> tuple[str, str]:
        """构建远程仿真求解命令和标志文件路径。

        Args:
            config_name: 构型名称

        Returns:
            (command, flag_file) 元组
        """
        config = remote_config or self._remote_config_for_workstation()
        flag_file = self._solver_flag_file(config_name, config)
        postprocess_flag_file = self._postprocess_flag_file(config_name, config)
        progress_file = self._solver_progress_file(config_name, config)
        conda_env = config["conda_env"]
        conda_exe = config["conda_exe"]
        scripts_dir = config["scripts_dir"]
        processor_count = self._solver_processor_count()
        iteration_count = ENGINE_CONFIG["solver_iteration_count"]
        postprocess_paths = self._postprocess_path_config(
            config,
            allow_config_override=remote_config is not None,
        )
        metric_constants = self._postprocess_metric_constants()

        # 构建参数化命令（所有路径均为必需参数，无默认值）
        anim_dir = postprocess_paths["animation_dir"]
        command = " ".join([
            _cmd_arg(conda_exe, force_quote=True),
            "run",
            "--no-capture-output",
            "-n",
            _cmd_arg(conda_env),
            "python",
            "-u",
            _cmd_arg(f"{scripts_dir}/batch_solver_gen4.py", force_quote=True),
            str(config_name),
            "--mpi-bin-dir",
            _cmd_arg(config["mpi_bin_dir"], force_quote=True),
            "--fluent-path",
            _cmd_arg(config["fluent_path"], force_quote=True),
            "--journal-path",
            _cmd_arg(f"{scripts_dir}/solver_gen4.jou", force_quote=True),
            "--msh-dir",
            _cmd_arg(config["msh_dir"], force_quote=True),
            "--output-dir",
            _cmd_arg(config["result_dir"], force_quote=True),
            "--postprocess-output-dir",
            _cmd_arg(postprocess_paths["output_dir"], force_quote=True),
            "--anim-dir",
            _cmd_arg(anim_dir, force_quote=True),
            "--working-dir",
            _cmd_arg(config["working_dir"], force_quote=True),
            "--working-dir-t",
            _cmd_arg(f"{config['working_dir']}/animation-t", force_quote=True),
            "--working-dir-v",
            _cmd_arg(f"{config['working_dir']}/animation-v", force_quote=True),
            "--processor-count",
            str(processor_count),
            "--iterate-count",
            str(iteration_count),
            "--progress-file",
            _cmd_arg(progress_file, force_quote=True),
            "--solver-flag-file",
            _cmd_arg(flag_file),
            "--post-journal-path",
            _cmd_arg(f"{scripts_dir}/solver_post_gen4.jou", force_quote=True),
            "--extra-post-journal-path",
            _cmd_arg(f"{scripts_dir}/postprocess_extra_gen4.jou", force_quote=True),
            "--metrics-script",
            _cmd_arg(f"{scripts_dir}/postprocess_metrics_gen4.py", force_quote=True),
            "--compute-metrics-script",
            _cmd_arg(f"{scripts_dir}/compute_metrics_gen4.py", force_quote=True),
            "--metrics-output-dir",
            _cmd_arg(postprocess_paths["metrics_dir"], force_quote=True),
            "--metrics-exit-to-throat-area-ratio",
            metric_constants["exit_to_throat_area_ratio"],
            "--metrics-cstar-reference",
            metric_constants["cstar_reference"],
            "--postprocess-flag-file",
            _cmd_arg(postprocess_flag_file),
        ])
        return command, flag_file

    def _build_postprocess_command(
        self,
        config_name: int,
        remote_config: dict[str, object] | None = None,
    ) -> tuple[str, str]:
        """构建远程后处理命令和标志文件路径。"""
        config = remote_config or self._remote_config_for_workstation()
        flag_file = self._postprocess_flag_file(config_name, config)
        conda_env = config["conda_env"]
        conda_exe = config["conda_exe"]
        scripts_dir = config["scripts_dir"]
        postprocess_paths = self._postprocess_path_config(
            config,
            allow_config_override=remote_config is not None,
        )
        metric_constants = self._postprocess_metric_constants()
        command = " ".join([
            _cmd_arg(conda_exe, force_quote=True),
            "run",
            "--no-capture-output",
            "-n",
            _cmd_arg(conda_env),
            "python",
            "-u",
            _cmd_arg(f"{scripts_dir}/batch_postprocess_gen4.py", force_quote=True),
            str(config_name),
            "--fluent-path",
            _cmd_arg(config["fluent_path"], force_quote=True),
            "--case-dir",
            _cmd_arg(config["result_dir"], force_quote=True),
            "--post-journal-path",
            _cmd_arg(f"{scripts_dir}/solver_post_gen4.jou", force_quote=True),
            "--extra-post-journal-path",
            _cmd_arg(f"{scripts_dir}/postprocess_extra_gen4.jou", force_quote=True),
            "--postprocess-output-dir",
            _cmd_arg(postprocess_paths["output_dir"], force_quote=True),
            "--metrics-script",
            _cmd_arg(f"{scripts_dir}/postprocess_metrics_gen4.py", force_quote=True),
            "--compute-metrics-script",
            _cmd_arg(f"{scripts_dir}/compute_metrics_gen4.py", force_quote=True),
            "--metrics-output-dir",
            _cmd_arg(postprocess_paths["metrics_dir"], force_quote=True),
            "--metrics-exit-to-throat-area-ratio",
            metric_constants["exit_to_throat_area_ratio"],
            "--metrics-cstar-reference",
            metric_constants["cstar_reference"],
            "--flag-file",
            _cmd_arg(flag_file),
            "--anim-dir",
            _cmd_arg(postprocess_paths["animation_dir"], force_quote=True),
            "--working-dir",
            _cmd_arg(config["working_dir"], force_quote=True),
            "--working-dir-t",
            _cmd_arg(f"{config['working_dir']}/animation-t", force_quote=True),
            "--working-dir-v",
            _cmd_arg(f"{config['working_dir']}/animation-v", force_quote=True),
        ])
        return command, flag_file

    def cleanup_solver_runtime_flag_artifacts(
        self,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> dict[str, int]:
        """清理全部 Solver 成功后 flags 中残留的后台 wrapper 脚本和日志。"""
        remote_config = self._remote_config_for_workstation(workstation_id)
        flag_dir = str(remote_config["flag_dir"])
        deleted = 0
        failed = 0

        with self._ssh_guard(workstation_id):
            try:
                ssh = self._get_ssh_for_workstation(workstation_id)
                list_remote_directory = getattr(ssh, "list_remote_directory", None)
                if not callable(list_remote_directory):
                    logger.warning("[Solver] SSH 客户端不支持列举 flags 目录，跳过运行产物清理")
                    return {"deleted": deleted, "failed": 1}

                flag_dir_base = flag_dir.rstrip("/\\")
                for filename in list_remote_directory(flag_dir):
                    lower = filename.lower()
                    if not (
                        lower.startswith("autofluid_bg_")
                        and lower.endswith((".cmd", ".log"))
                    ):
                        continue
                    remote_path = f"{flag_dir_base}/{filename}".replace("\\", "/")
                    try:
                        if ssh.delete_remote_file(remote_path):
                            deleted += 1
                        else:
                            failed += 1
                    except (OSError, ConnectionError) as e:
                        failed += 1
                        logger.debug(f"[Solver] 清理 flags 运行产物失败: {remote_path}: {e}")
            except (OSError, ConnectionError) as e:
                logger.warning(f"[Solver] 清理 flags 运行产物异常: {e}")
                failed += 1

        logger.info(f"[Solver] flags 运行产物清理完成: deleted={deleted}, failed={failed}")
        return {"deleted": deleted, "failed": failed}

    def execute_solver(
        self,
        config_name: int,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> bool:
        """在远程工作站启动仿真求解后台任务（全局屏障后调用）。

        注意：本方法仅返回成功/失败，不设置步骤状态。
        状态由调用方（RetryManager / BarrierCoordinator）统一管理。
        """
        # 安全校验：config_name 必须为整数（来自 Excel 构型号），防止命令注入
        try:
            self._validate_config_name(config_name)
        except ValueError:
            logger.error(f"[Solver] 无效的构型名称类型: {type(config_name).__name__}")
            return False

        remote_config = self._remote_config_for_workstation(workstation_id)

        # 同步远程脚本（仅在文件变更时上传）
        if not self.sync_scripts(workstation_id=workstation_id):
            logger.error("[Solver] 远程脚本同步失败，无法启动仿真求解")
            return False

        try:
            command, flag_file = self._build_solver_command(config_name, remote_config)
        except ValueError as e:
            logger.error(f"[Solver] 远程求解命令构建失败: {e}")
            return False

        logger.info(f"[Solver] 启动远程仿真求解: 构型{config_name}")
        logger.debug(f"[Solver] 远程命令: {command}")

        with self._ssh_guard(workstation_id):
            try:
                ssh = self._get_ssh_for_workstation(workstation_id)
                success, task_name = ssh.exec_background(
                    command, flag_file,
                    working_dir=str(remote_config["working_dir"]),
                    interactive=True,
                )
                if success:
                    self._remember_remote_task(
                        config_name,
                        "solver",
                        task_name,
                        workstation_id,
                    )
                    self._persist_remote_task(
                        workstation_id=workstation_id,
                        config_name=config_name,
                        step_name="solver",
                        task_name=task_name,
                        flag_file=flag_file,
                    )
                    logger.info(f"[Solver] 仿真求解后台任务已启动: 构型{config_name}")
                    return True
                else:
                    logger.error(f"[Solver] 远程求解启动失败: 构型{config_name}")
                    return False
            except InfrastructureUnavailableError:
                raise
            except (OSError, ConnectionError) as e:
                _raise_infra_on_ssh_error(e)

    def execute_postprocess(
        self,
        config_name: int,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> bool:
        """接管同一 Fluent 会话中的后处理阶段。

        Solver 远程脚本在同一个 Fluent session 内继续执行 PostProcess。
        优先把正在运行的 solver 后台任务登记为 postprocess 任务，
        让后续 wait_postprocess_completion() 轮询 postprocess_done flag；
        若 solver 任务记录已清理，则启动独立 PostProcess 任务用于恢复。
        """
        try:
            self._validate_config_name(config_name)
        except ValueError:
            logger.error(f"[PostProcess] 无效的构型名称类型: {type(config_name).__name__}")
            return False
        if self._register_postprocess_from_solver(config_name, workstation_id):
            return True
        return self._start_standalone_postprocess(config_name, workstation_id)

    def _start_standalone_postprocess(
        self,
        config_name: int,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> bool:
        """启动独立 PostProcess 后台任务，用于 solver task 已清理后的恢复。"""
        try:
            self._validate_config_name(config_name)
        except ValueError:
            logger.error(f"[PostProcess] 无效的构型名称类型: {type(config_name).__name__}")
            return False

        remote_config = self._remote_config_for_workstation(workstation_id)
        if not self.sync_scripts(workstation_id=workstation_id):
            logger.error("[PostProcess] 远程脚本同步失败，无法启动独立后处理")
            return False

        try:
            command, flag_file = self._build_postprocess_command(config_name, remote_config)
        except ValueError as e:
            logger.error(f"[PostProcess] 远程后处理命令构建失败: {e}")
            return False

        logger.info(f"[PostProcess] 启动独立远程后处理: 构型{config_name}")
        logger.debug(f"[PostProcess] 远程命令: {command}")

        with self._ssh_guard(workstation_id):
            try:
                ssh = self._get_ssh_for_workstation(workstation_id)
                success, task_name = ssh.exec_background(
                    command,
                    flag_file,
                    working_dir=str(remote_config["working_dir"]),
                    interactive=True,
                )
                if success:
                    self._remember_remote_task(
                        config_name,
                        "postprocess",
                        task_name,
                        workstation_id,
                    )
                    self._persist_remote_task(
                        workstation_id=workstation_id,
                        config_name=config_name,
                        step_name="postprocess",
                        task_name=task_name,
                        flag_file=flag_file,
                    )
                    logger.info(f"[PostProcess] 独立后处理后台任务已启动: 构型{config_name}")
                    return True
                logger.error(f"[PostProcess] 独立后处理远程任务启动失败: 构型{config_name}")
                return False
            except InfrastructureUnavailableError:
                raise
            except (OSError, ConnectionError) as e:
                _raise_infra_on_ssh_error(e)

    def _register_postprocess_from_solver(
        self,
        config_name: int,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> bool:
        try:
            self._validate_config_name(config_name)
        except ValueError:
            logger.error(f"[PostProcess] 无效的构型名称类型: {type(config_name).__name__}")
            return False

        remote_config = self._remote_config_for_workstation(workstation_id)
        try:
            flag_file = self._postprocess_flag_file(config_name, remote_config)
        except ValueError as e:
            logger.error(f"[PostProcess] 远程后处理 flag 构建失败: {e}")
            return False

        with self._remote_tasks_lock:
            solver_task_name = self._remote_tasks.get(
                self._remote_task_key(config_name, "solver", workstation_id),
            )
        if solver_task_name is None:
            solver_task = self._get_remote_task_from_state(
                config_name,
                "solver",
                workstation_id,
            )
            if solver_task is not None:
                solver_task_name = str(solver_task["task_name"])
        if not solver_task_name:
            logger.warning(
                f"[PostProcess] 构型{config_name} 无可接管的 Solver 远程任务，"
                "将尝试启动独立后处理"
            )
            return False

        self._remember_remote_task(
            config_name,
            "postprocess",
            solver_task_name,
            workstation_id,
        )
        self._persist_remote_task(
            workstation_id=workstation_id,
            config_name=config_name,
            step_name="postprocess",
            task_name=solver_task_name,
            flag_file=flag_file,
        )
        logger.info(
            f"[PostProcess] 构型{config_name} 已接管 Solver Fluent 会话，等待后处理完成"
        )
        return True

    def _read_solver_progress(
        self,
        ssh: "RemoteWorkstation",
        progress_file: str,
        config_name: int,
    ) -> None:
        """读取远程 Solver progress JSON 并写入状态。"""
        read_remote_text_file = getattr(ssh, "read_remote_text_file", None)
        if not callable(read_remote_text_file):
            return
        raw = read_remote_text_file(progress_file, timeout=5)
        if not raw:
            return
        try:
            progress = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug(f"[Solver] progress JSON 暂不可读，忽略本轮: {progress_file}")
            return
        if not isinstance(progress, dict):
            return
        if progress.get("config_name") != config_name:
            logger.debug(
                f"[Solver] progress 构型不匹配，忽略: {progress.get('config_name')} != {config_name}"
            )
            return
        sanitized = {
            key: progress[key]
            for key in (
                "config_name",
                "current_iter",
                "total_iter",
                "remaining_sec",
                "updated_at",
            )
            if key in progress
        }
        set_solver_progress = getattr(self.state, "set_solver_progress", None)
        if callable(set_solver_progress):
            set_solver_progress(sanitized)

    def _clear_solver_progress(
        self,
        progress_file: str,
        ssh: RemoteWorkstation | None = None,
    ) -> None:
        """清理本地与远程 Solver progress。"""
        clear_solver_progress = getattr(self.state, "clear_solver_progress", None)
        if callable(clear_solver_progress):
            clear_solver_progress()
        if ssh is None:
            return
        delete_remote_file = getattr(ssh, "delete_remote_file", None)
        if callable(delete_remote_file):
            try:
                delete_remote_file(progress_file)
            except (OSError, ConnectionError) as e:
                logger.debug(f"[Solver] 清理远程 progress 文件失败: {progress_file}: {e}")

    def wait_solver_completion(
        self, config_name: int,
        paused_event: threading.Event | None = None,
        stopped_event: threading.Event | None = None,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> bool:
        """轮询等待仿真求解完成（逐次短暂持 SSH 锁）。

        检测到标志文件后，额外验证 .cas.h5 和 .dat.h5 是否都存在。
        若仅存在一个文件，宽限 60s 等待另一个；超时则清理部分文件并返回错误。
        """
        remote_config = self._remote_config_for_workstation(workstation_id)
        try:
            flag_file = self._solver_flag_file(config_name, remote_config)
            progress_file = self._solver_progress_file(config_name, remote_config)
        except ValueError:
            logger.error(f"[Solver] 无效的构型名称类型: {type(config_name).__name__}")
            return False
        error_flag = f"{flag_file}.error"
        result_dir = str(remote_config["result_dir"]).replace('\\', '/')
        cas_name = get_step_filename("solver", config_name)
        dat_name = get_step_filename("solverdata", config_name)
        cas_file = f"{result_dir}/{cas_name}" if cas_name else None
        dat_file = f"{result_dir}/{dat_name}" if dat_name else None

        timeout = ENGINE_CONFIG["solver_timeout"]
        poll_interval = 30
        start_time = self._remote_task_start_time(
            config_name,
            "solver",
            workstation_id,
        )
        file_grace_period = 60
        first_file_seen_time: float | None = None

        logger.info(f"[Solver] 开始轮询构型{config_name} 仿真求解状态 (超时: {timeout}s)")

        while time.time() - start_time < timeout:
            # ---- 暂停/停止响应（统一使用 wait_unless_paused_or_stopped） ----
            if paused_event is not None:
                pause_start = time.time()
                if not wait_unless_paused_or_stopped(paused_event, stopped_event or threading.Event()):
                    self._clear_solver_progress(progress_file)
                    return False
                # 暂停补偿：将超时计时器和文件宽限计时器向后推移暂停时长
                pause_duration = time.time() - pause_start
                if pause_duration > 0:
                    start_time += pause_duration
                    if first_file_seen_time is not None:
                        first_file_seen_time += pause_duration
            if stopped_event is not None and stopped_event.is_set():
                self._clear_solver_progress(progress_file)
                return False

            try:
                with self._ssh_guard(workstation_id):
                    ssh = self._get_ssh_for_workstation(workstation_id)
                    if ssh.check_remote_file(error_flag):
                        logger.error(
                            f"[Solver] 构型{config_name} 仿真求解远程任务执行失败"
                        )
                        ssh.delete_remote_file(error_flag)
                        self._clear_solver_progress(progress_file, ssh)
                        self._cleanup_completed_remote_task(
                            config_name,
                            "solver",
                            ssh,
                            workstation_id,
                        )
                        return False
                    self._read_solver_progress(ssh, progress_file, config_name)
                    if ssh.check_remote_file(flag_file):
                        # 标志文件存在，验证输出文件
                        cas_exists = cas_file is not None and ssh.check_remote_file(cas_file)
                        dat_exists = dat_file is not None and ssh.check_remote_file(dat_file)

                        if cas_exists and dat_exists:
                            ssh.delete_remote_file(flag_file)
                            self._clear_solver_progress(progress_file, ssh)
                            logger.info(f"[Solver] 构型{config_name} 仿真求解完成（cas+dat 均已保存）")
                            return True

                        # 部分文件缺失
                        if first_file_seen_time is None:
                            first_file_seen_time = time.time()
                            missing = []
                            if not cas_exists:
                                missing.append("cas.h5")
                            if not dat_exists:
                                missing.append("dat.h5")
                            logger.warning(
                                f"[Solver] 构型{config_name}: 标志文件已存在但缺少 "
                                f"{', '.join(missing)}，等待 {file_grace_period}s"
                            )

                        if (first_file_seen_time is not None
                                and time.time() - first_file_seen_time > file_grace_period):
                            missing = []
                            if not cas_exists:
                                missing.append("cas.h5")
                            if not dat_exists:
                                missing.append("dat.h5")
                            logger.error(
                                f"[Solver] 构型{config_name}: {', '.join(missing)} "
                                f"{file_grace_period}s 内未生成，判定为导出错误"
                            )
                            # 清理部分文件 + 标志文件，确保 retry 从干净状态开始
                            if cas_exists and cas_file:
                                ssh.delete_remote_file(cas_file)
                                logger.info(f"[Solver] 构型{config_name}: 已清理部分文件 {cas_file}")
                            if dat_exists and dat_file:
                                ssh.delete_remote_file(dat_file)
                                logger.info(f"[Solver] 构型{config_name}: 已清理部分文件 {dat_file}")
                            ssh.delete_remote_file(flag_file)
                            self._clear_solver_progress(progress_file, ssh)
                            self._cleanup_completed_remote_task(
                                config_name,
                                "solver",
                                ssh,
                                workstation_id,
                            )
                            return False
            except InfrastructureUnavailableError:
                raise
            except (OSError, ConnectionError) as e:
                _raise_infra_on_ssh_error(e)

            time.sleep(poll_interval)

        logger.error(f"[Solver] 构型{config_name} 仿真求解超时 ({timeout}s)")
        # 超时后终止远程进程，防止资源泄漏和重试冲突
        self._kill_remote_task_for_config(config_name, "solver", workstation_id)
        with self._ssh_guard(workstation_id):
            try:
                ssh = self._get_ssh_for_workstation(workstation_id)
                self._clear_solver_progress(progress_file, ssh)
            except (OSError, ConnectionError):
                self._clear_solver_progress(progress_file)
        return False

    def wait_postprocess_completion(
        self,
        config_name: int,
        paused_event: threading.Event | None = None,
        stopped_event: threading.Event | None = None,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> bool:
        """轮询等待后处理完成。

        PostProcess 的业务输出文件名尚未稳定，本阶段只以独立完成 flag
        表示工作站本地后处理结束；后续服务器上传阶段独立扫描输出目录。
        """
        remote_config = self._remote_config_for_workstation(workstation_id)
        try:
            flag_file = self._postprocess_flag_file(config_name, remote_config)
        except ValueError:
            logger.error(f"[PostProcess] 无效的构型名称类型: {type(config_name).__name__}")
            return False
        error_flag = f"{flag_file}.error"

        timeout = ENGINE_CONFIG["postprocess_timeout"]
        poll_interval = 30
        start_time = self._remote_task_start_time(
            config_name,
            "postprocess",
            workstation_id,
        )

        logger.info(f"[PostProcess] 开始轮询构型{config_name} 后处理状态 (超时: {timeout}s)")

        while time.time() - start_time < timeout:
            if paused_event is not None:
                pause_start = time.time()
                if not wait_unless_paused_or_stopped(paused_event, stopped_event or threading.Event()):
                    return False
                pause_duration = time.time() - pause_start
                if pause_duration > 0:
                    start_time += pause_duration
            if stopped_event is not None and stopped_event.is_set():
                return False

            try:
                with self._ssh_guard(workstation_id):
                    ssh = self._get_ssh_for_workstation(workstation_id)
                    if ssh.check_remote_file(error_flag):
                        logger.error(f"[PostProcess] 构型{config_name} 后处理远程任务执行失败")
                        ssh.delete_remote_file(error_flag)
                        self._cleanup_completed_remote_task(
                            config_name,
                            "postprocess",
                            ssh,
                            workstation_id,
                        )
                        return False
                    if ssh.check_remote_file(flag_file):
                        logger.info(f"[PostProcess] 构型{config_name} 后处理完成")
                        return True
            except InfrastructureUnavailableError:
                raise
            except (OSError, ConnectionError) as e:
                _raise_infra_on_ssh_error(e)

            time.sleep(poll_interval)

        logger.error(f"[PostProcess] 构型{config_name} 后处理超时 ({timeout}s)")
        self._kill_remote_task_for_config(config_name, "postprocess", workstation_id)
        return False

    def cleanup_completed_postprocess_task(
        self,
        config_name: int,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> None:
        """状态已持久化后清理 PostProcess 完成 flag 和远程任务记录。"""
        remote_config = self._remote_config_for_workstation(workstation_id)
        try:
            flag_file = self._postprocess_flag_file(config_name, remote_config)
        except ValueError:
            logger.error(f"[PostProcess] 无效的构型名称类型: {type(config_name).__name__}")
            return

        with self._ssh_guard(workstation_id):
            try:
                ssh = self._get_ssh_for_workstation(workstation_id)
                ssh.delete_remote_file(flag_file)
                ssh.delete_remote_file(f"{flag_file}.error")
                self._cleanup_completed_remote_task(
                    config_name,
                    "postprocess",
                    ssh,
                    workstation_id,
                )
            except (OSError, ConnectionError) as e:
                logger.warning(f"[PostProcess] 构型{config_name} 完成清理异常: {e}")

    # ------------------------------------------------------------------
    # 远程进程生命周期管理
    # ------------------------------------------------------------------

    def _kill_remote_task_for_config(
        self,
        config_name: int,
        step_name: str,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> None:
        """超时后终止远程后台任务。

        从 _remote_tasks 中取出任务名称，调用 SSH kill_remote_task 终止。
        无论终止是否成功，都清理跟踪记录。

        Args:
            config_name: 构型编号
            step_name: 步骤名（用于日志）
        """
        task_name = self._pop_remote_task(config_name, step_name, workstation_id)
        self._delete_remote_task_from_state(config_name, step_name, workstation_id)
        if not task_name:
            logger.debug(f"{self._log_prefix(step_name)} 构型{config_name} 无远程任务记录，跳过终止")
            return

        with self._ssh_guard(workstation_id):
            try:
                ssh = self._get_ssh_for_workstation(workstation_id)
                ssh.kill_remote_task(task_name)
                logger.info(f"{self._log_prefix(step_name)} 构型{config_name} 已请求终止远程任务: {task_name}")
            except (OSError, ConnectionError) as e:
                logger.warning(f"{self._log_prefix(step_name)} 构型{config_name} 终止远程任务异常: {e}")

    def _cleanup_completed_remote_task(
        self,
        config_name: int,
        step_name: str,
        ssh: "RemoteWorkstation",
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> None:
        """清理已结束任务的计划任务条目和本地跟踪记录。"""
        task_name = self._pop_remote_task(config_name, step_name, workstation_id)
        task = self._get_remote_task_from_state(config_name, step_name, workstation_id)
        pid_file = None
        if task is not None and task.get("pid_file"):
            pid_file = str(task["pid_file"])
        self._delete_remote_task_from_state(config_name, step_name, workstation_id)
        if not task_name:
            return

        cleanup_task = getattr(ssh, "cleanup_remote_task_entry", None)
        if callable(cleanup_task):
            cleaned = cleanup_task(task_name, pid_file)
        else:
            cleaned = ssh.kill_remote_task(task_name)

        if cleaned:
            logger.info(f"{self._log_prefix(step_name)} 构型{config_name} 已清理远程任务条目: {task_name}")
        else:
            logger.warning(f"{self._log_prefix(step_name)} 构型{config_name} 清理远程任务条目失败: {task_name}")

    # ------------------------------------------------------------------
    # 脚本同步
    # ------------------------------------------------------------------

    def sync_scripts(self, workstation_id: str = DEFAULT_WORKSTATION_ID) -> bool:
        """同步远程脚本和引用文件到工作站。

        比较本地和远程文件的 MD5 哈希值，仅在文件变更时上传。
        对于包含占位符的文件，上传时会动态替换路径。

        若检测到远程目录路径发生变更，先清理旧路径中的已知文件，
        再上传到新路径。

        Returns:
            同步成功返回 True，失败返回 False
        """
        with self._sync_cache_lock:
            return self._sync_scripts_locked(workstation_id)

    def _sync_scripts_locked(self, workstation_id: str = DEFAULT_WORKSTATION_ID) -> bool:
        """在缓存锁保护下同步脚本和引用文件。"""
        local_scripts_dir = LOCAL_PATHS.get("remote_scripts_dir")
        if not local_scripts_dir or not os.path.isdir(local_scripts_dir):
            logger.error(f"[Sync] 本地脚本目录不存在: {local_scripts_dir}")
            return False

        remote_config = self._remote_config_for_workstation(workstation_id)
        current_scripts_dir = str(remote_config["scripts_dir"])
        current_ref_dir = str(remote_config["ref_files_dir"])
        ref_local_dir = os.path.join(local_scripts_dir, "fluent_chemkin_files")

        script_hashes = self._calculate_local_hashes(
            local_scripts_dir,
            REMOTE_SCRIPT_FILES,
            remote_config,
        )
        if script_hashes is None:
            return False

        ref_hashes: dict[str, str | None] | None = None
        if os.path.isdir(ref_local_dir):
            ref_hashes = self._calculate_local_hashes(
                ref_local_dir,
                REMOTE_REF_FILES,
                remote_config,
            )
            if ref_hashes is None:
                return False

        sync_signature = self._build_sync_signature(
            current_scripts_dir,
            current_ref_dir,
            script_hashes,
            ref_hashes,
            remote_config,
        )
        if sync_signature == self._last_successful_sync_signatures.get(workstation_id):
            return True

        # ---- 路径变更检测：清理旧远程文件 ----
        last_paths = self._last_sync_paths_for_workstation(workstation_id)

        last_scripts_dir = last_paths.get("scripts_dir")
        last_ref_dir = last_paths.get("ref_files_dir")

        if last_scripts_dir and last_scripts_dir != current_scripts_dir:
            self._cleanup_remote_files(
                last_scripts_dir,
                REMOTE_SCRIPT_FILES,
                "脚本",
                workstation_id,
            )
        if last_ref_dir and last_ref_dir != current_ref_dir:
            self._cleanup_remote_files(
                last_ref_dir,
                REMOTE_REF_FILES,
                "引用文件",
                workstation_id,
            )

        # ---- 正常同步流程 ----

        # 同步脚本文件 → scripts_dir
        if not self._sync_file_group(
            local_dir=local_scripts_dir,
            remote_dir=current_scripts_dir,
            filenames=REMOTE_SCRIPT_FILES,
            label="脚本",
            local_hashes=script_hashes,
            remote_config=remote_config,
            workstation_id=workstation_id,
        ):
            return False

        # 同步引用文件 → ref_files_dir
        if ref_hashes is not None:
            if not self._sync_file_group(
                local_dir=ref_local_dir,
                remote_dir=current_ref_dir,
                filenames=REMOTE_REF_FILES,
                label="引用文件",
                local_hashes=ref_hashes,
                remote_config=remote_config,
                workstation_id=workstation_id,
            ):
                return False

        # 同步成功 → 记录当前路径供下次比对
        self._save_last_sync_paths(remote_config, workstation_id)
        self._last_successful_sync_signature = sync_signature
        self._last_successful_sync_signatures[workstation_id] = sync_signature
        return True

    def _build_sync_signature(
        self,
        scripts_dir: str,
        ref_files_dir: str,
        script_hashes: dict[str, str | None],
        ref_hashes: dict[str, str | None] | None,
        remote_config: dict[str, object] | None = None,
    ) -> tuple[object, ...]:
        """构建一次成功同步的本地内容和远程路径指纹。"""
        config = remote_config or self._remote_config_for_workstation()
        postprocess_paths = self._postprocess_path_config(config)
        placeholder_inputs = (
            str(config["scripts_dir"]),
            str(config["scdoc_dir"]),
            str(config["working_dir"]),
            str(config["ref_files_dir"]),
            str(config["msh_dir"]),
            str(config["result_dir"]),
            postprocess_paths["output_dir"],
            postprocess_paths["animation_dir"],
            postprocess_paths["metrics_dir"],
            STEP_FILE_PATTERNS.get("sc", ""),
        )
        return (
            scripts_dir,
            ref_files_dir,
            placeholder_inputs,
            tuple(sorted(script_hashes.items())),
            tuple(sorted(ref_hashes.items())) if ref_hashes is not None else None,
        )

    @staticmethod
    def _compute_combined_hash(file_hashes: dict[str, str | None]) -> str:
        """计算文件哈希字典的组合 MD5 哈希值。

        将各文件按名称排序后拼接为 "name:hash|..." 格式，
        再计算 MD5，确保本地与远程计算方式一致。

        Args:
            file_hashes: 文件名到哈希值的映射（值为 None 表示文件缺失）

        Returns:
            组合 MD5 哈希值（小写十六进制字符串）
        """
        combined = "|".join(
            f"{name}:{hash_val or 'MISSING'}"
            for name, hash_val in sorted(file_hashes.items())
        )
        return hashlib.md5(combined.encode('utf-8')).hexdigest()

    def _sync_file_group(
        self,
        local_dir: str,
        remote_dir: str,
        filenames: list[str],
        label: str,
        local_hashes: dict[str, str | None] | None = None,
        remote_config: dict[str, object] | None = None,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> bool:
        """同步一组文件到远程目录。

        采用两级哈希校验策略：
        1. 第一级：计算本地与远程文件的组合哈希，若一致则所有文件均无需同步
        2. 第二级：组合哈希不一致时，逐文件比对哈希值，仅上传变更的文件

        ★ 锁策略优化：不在整个同步期间持有 _ssh_lock。改为逐文件获取/释放锁，
          避免长时间阻塞 Transfer 等并发 SSH 操作。

        Args:
            local_dir: 本地文件目录
            remote_dir: 远程目标目录
            filenames: 文件名列表
            label: 日志标签（如 "脚本"、"引用文件"）

        Returns:
            同步成功返回 True，失败返回 False
        """
        # ---- 阶段 0: 计算本地文件哈希 ----
        if local_hashes is None:
            local_hashes = self._calculate_local_hashes(local_dir, filenames)
            if local_hashes is None:
                return False

        # ---- 阶段 1: 第一级校验 —— 组合哈希快速比对（1 次 SSH 调用） ----
        local_combined = self._compute_combined_hash(local_hashes)
        with self._ssh_guard(workstation_id):
            try:
                ssh = self._get_ssh_for_workstation(workstation_id)
                remote_combined = ssh.get_remote_combined_file_hash(
                    remote_dir, filenames
                )
            except (OSError, ConnectionError) as e:
                logger.error(f"[Sync] 获取{label}文件远程组合哈希异常: {e}")
                return False

        if remote_combined is not None and local_combined == remote_combined:
            logger.info(f"[Sync] 所有{label}文件已是最新（组合哈希一致），无需同步")
            return True

        logger.debug(
            f"[Sync] {label}组合哈希不一致"
            f" (本地: {local_combined[:8]}... 远程: {remote_combined[:8] if remote_combined else 'None'}...)，"
            f"进入逐文件比对"
        )

        # ---- 阶段 2: 第二级校验 —— 逐文件比对（N 次 SSH 调用） ----
        with self._ssh_guard(workstation_id):
            try:
                ssh = self._get_ssh_for_workstation(workstation_id)
                remote_hashes = ssh.get_remote_file_hashes(remote_dir, filenames)
            except (OSError, ConnectionError) as e:
                logger.error(f"[Sync] 获取{label}文件远程哈希异常: {e}")
                return False

        # ---- 阶段 3: 比较哈希，确定需上传的文件（无锁） ----
        files_to_upload = []
        for filename in filenames:
            local_hash = local_hashes.get(filename)
            remote_hash = remote_hashes.get(filename)
            if local_hash is None:
                logger.warning(f"[Sync] 本地{label}文件不存在: {filename}")
                continue
            if local_hash != remote_hash:
                files_to_upload.append(filename)
                logger.info(f"[Sync] {label}文件变更: {filename} (本地: {local_hash[:8]}... 远程: {remote_hash[:8] if remote_hash else '不存在'})")

        if not files_to_upload:
            logger.info(f"[Sync] 所有{label}文件已是最新，无需同步")
            return True

        logger.info(f"[Sync] 需要上传 {len(files_to_upload)} 个{label}文件: {', '.join(files_to_upload)}")

        # ---- 阶段 3: 逐文件上传（每文件独立持锁，避免长时间阻塞） ----
        path_aware_exts = {'.jou', '.set', '.wft', '.pdf'}
        for filename in files_to_upload:
            # ★ 上传前检查控制事件
            if self._stopped_event is not None and self._stopped_event.is_set():
                logger.info(f"[Sync] {label}同步因停止指令取消")
                return False
            if self._paused_event is not None and self._paused_event.is_set():
                logger.info(f"[Sync] {label}同步因暂停指令暂缓")
                return False

            local_file = os.path.join(local_dir, filename)
            remote_file = f"{remote_dir}/{filename}".replace("\\", "/")

            # ★ 逐文件获取 SSH 锁，上传完成后立即释放，
            #   允许 Transfer 等操作在文件间插入执行
            with self._ssh_guard(workstation_id):
                try:
                    ssh = self._get_ssh_for_workstation(workstation_id)
                    if os.path.splitext(filename)[1] in path_aware_exts:
                        if not self._upload_text_file_with_path_replacement(
                            ssh, local_file, remote_file,
                            remote_config=remote_config,
                            paused_event=self._paused_event,
                            stopped_event=self._stopped_event,
                        ):
                            return False
                    else:
                        if not ssh.upload_file(
                            local_file, remote_file,
                            paused_event=self._paused_event,
                            stopped_event=self._stopped_event,
                        ):
                            logger.error(f"[Sync] 上传{label}文件失败: {filename}")
                            return False
                except (OSError, ConnectionError) as e:
                    logger.error(f"[Sync] 上传{label}文件异常: {filename}: {e}")
                    return False

        logger.info(f"[Sync] {label}同步完成")
        return True

    def _calculate_local_hashes(
        self,
        local_dir: str,
        filenames: list,
        remote_config: dict[str, object] | None = None,
    ) -> dict[str, str | None] | None:
        """计算本地目录中指定文件的 MD5 哈希值。

        对于包含占位符的文件（.jou/.set/.wft/.pdf），
        计算替换后的哈希值，以便与远程文件哈希正确比较，避免每次都重新上传。

        Args:
            local_dir: 本地目录路径
            filenames: 要计算哈希的文件名列表

        Returns:
            字典，键为文件名，值为 MD5 哈希值；失败返回 None
        """
        path_aware_exts = {'.jou', '.set', '.wft', '.pdf'}
        result: dict[str, str | None] = {}
        try:
            for filename in filenames:
                filepath = os.path.join(local_dir, filename)
                if not os.path.exists(filepath):
                    logger.warning(f"[Sync] 本地文件不存在: {filepath}")
                    result[filename] = None
                    continue

                ext = os.path.splitext(filename)[1]
                if ext in path_aware_exts:
                    # 对包含占位符的文件，计算替换后内容的哈希
                    # ★ 使用 newline='' 保留原始换行符（\r\n），避免 Python
                    #    文本模式的通用换行符转换（\r\n → \n）导致本地哈希与
                    #    远程文件（通过 SFTP 二进制上传）的哈希永久不一致。
                    with open(filepath, 'r', encoding='utf-8', newline='') as f:
                        content = f.read()
                    content = self._apply_placeholders(content, remote_config)
                    file_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
                else:
                    # 普通文件直接计算原始内容哈希
                    with open(filepath, 'rb') as f:
                        file_hash = hashlib.md5(f.read()).hexdigest()
                result[filename] = file_hash

            return result

        except OSError as e:
            logger.error(f"[Sync] 计算本地文件哈希失败: {e}")
            return None

    def _apply_placeholders(
        self,
        content: str,
        remote_config: dict[str, object] | None = None,
    ) -> str:
        """将模板中的所有占位符替换为实际远程目录值。"""
        def fluent_path(path: object) -> str:
            return str(path).replace("\\", "/")

        config = remote_config or self._remote_config_for_workstation()
        scripts_dir = fluent_path(config["scripts_dir"])
        content = content.replace('{{REMOTE_ROOT}}', scripts_dir)
        content = content.replace('{{REMOTE_SCDOC_DIR}}', fluent_path(config["scdoc_dir"]))
        content = content.replace('{{REMOTE_WORKING_DIR}}', fluent_path(config["working_dir"]))
        content = content.replace('{{REMOTE_REF_FILES_DIR}}', fluent_path(config["ref_files_dir"]))
        content = content.replace('{{REMOTE_MSH_DIR}}', fluent_path(config["msh_dir"]))
        content = content.replace('{{REMOTE_RESULT_DIR}}', fluent_path(config["result_dir"]))
        postprocess_paths = self._postprocess_path_config(config)
        content = content.replace('{{REMOTE_POSTPROCESS_OUTPUT_DIR}}', fluent_path(postprocess_paths["output_dir"]))
        content = content.replace('{{REMOTE_POSTPROCESS_ANIMATION_DIR}}', fluent_path(postprocess_paths["animation_dir"]))
        content = content.replace('{{REMOTE_POSTPROCESS_METRICS_DIR}}', fluent_path(postprocess_paths["metrics_dir"]))
        sc_pattern = STEP_FILE_PATTERNS.get("sc", "")
        if sc_pattern:
            content = content.replace('{{SC_FILENAME}}', sc_pattern)
        return content

    def _upload_text_file_with_path_replacement(
        self,
        ssh: "RemoteWorkstation",
        local_file: str,
        remote_file: str,
        remote_config: dict[str, object] | None = None,
        paused_event: threading.Event | None = None,
        stopped_event: threading.Event | None = None,
    ) -> bool:
        """上传文本文件，将占位符替换为实际远程目录。

        适用于 .jou、.set、.wft、.pdf 等包含路径引用的配置文件。

        Args:
            ssh: SSH 连接实例
            local_file: 本地文件路径
            remote_file: 远程文件路径
            paused_event: 暂停事件（可选，用于中断上传）
            stopped_event: 停止事件（可选，用于中断上传）

        Returns:
            上传成功返回 True，失败返回 False
        """
        try:
            # ★ 使用 newline='' 保留原始换行符（\r\n），避免 Python
            #    文本模式的通用换行符转换破坏哈希一致性。
            with open(local_file, 'r', encoding='utf-8', newline='') as f:
                content = f.read()

            # 替换所有占位符
            content = self._apply_placeholders(content, remote_config)

            # ★ 写入临时文件时使用二进制模式，确保内容字节与
            #    _calculate_local_hashes 中 encode('utf-8') 的字节完全一致，
            #    避免文本模式的平台换行符转换（Windows 上 \n → \r\n）导致
            #    上传后的远程文件哈希与本地计算的哈希不匹配。
            temp_file = local_file + '.tmp'
            with open(temp_file, 'wb') as f:
                f.write(content.encode('utf-8'))

            # ★ 上传临时文件（传递控制事件，允许暂停/停止中断大文件上传）
            try:
                success = ssh.upload_file(
                    temp_file, remote_file,
                    paused_event=paused_event,
                    stopped_event=stopped_event,
                )
            finally:
                # 确保 temp_file 在 upload 成功或失败后都被清理
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

            if success:
                logger.info(f"[Transfer] 上传配置文件（已替换路径）: {os.path.basename(local_file)}")
            return success

        except (OSError, UnicodeDecodeError) as e:
            logger.error(f"[Transfer] 上传配置文件失败: {e}")
            return False
