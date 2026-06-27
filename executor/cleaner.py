"""
===============================================================================
文件清理与系统自检模块 (Cleaner)

负责：
- 系统自检（本地路径、SSH 连通性、远程进程状态）
- 文件清理（本地 + 远程工作站上的步骤产出文件）

从 engine/task_runner.py 中提取。
===============================================================================
"""
from __future__ import annotations

from collections.abc import Callable as CallableABC, Mapping
from contextlib import AbstractContextManager, nullcontext
import ipaddress
import os
import threading
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from utils.ssh_client import RemoteWorkstation
    from engine.state_manager import StateManager

from engine.config import (
    DEFAULT_WORKSTATION_ID, IPC_CONFIG, LOCAL_PATHS, REMOTE_CONFIG, WORKSTATIONS,
    STEP_NAMES, STEP_FILE_PATTERNS, get_workstation_config, is_server_mode,
)
from executor.postprocess_paths import resolve_postprocess_paths
from executor.remote_executor import REMOTE_SCRIPT_FILES, REMOTE_REF_FILES
from utils.logger import setup_logger

logger = setup_logger(__name__)


def _is_private_ip(host: str) -> bool:
    """Return True when host is a literal private/link-local/loopback IP."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _has_explicit_reachability(workstation: Mapping[str, object]) -> bool:
    """Return True when config declares how ocar reaches a private workstation."""
    reachable_host = str(workstation.get("reachable_host", "")).strip()
    connectivity_mode = str(workstation.get("connectivity_mode", "")).strip()
    return bool(reachable_host or connectivity_mode in {"vpn", "tailscale", "reverse_tunnel"})


def _remote_flag_paths(config: Mapping[str, object], stem: str, config_name: int) -> list[str]:
    """Return done/error flag paths for one remote step and config."""
    flag_dir = str(config.get("flag_dir", "")).replace("\\", "/")
    if not flag_dir:
        return []
    flag_file = f"{flag_dir}/{stem}_{config_name}.txt"
    paths = [flag_file, f"{flag_file}.error"]
    if stem == "solver_done":
        progress_file = f"{flag_dir}/solver_progress_{config_name}.json"
        paths.extend([progress_file, f"{progress_file}.transcript"])
    return paths


def _postprocess_cleanup_paths(config: Mapping[str, object], config_name: int) -> list[str]:
    """Return per-config postprocess result and runtime files to delete."""
    flag_dir = str(config.get("flag_dir", "")).replace("\\", "/").rstrip("/")
    postprocess_paths = resolve_postprocess_paths(config)
    output_dir = postprocess_paths["output_dir"]
    animation_dir = postprocess_paths["animation_dir"]
    metrics_dir = postprocess_paths["metrics_dir"]
    paths: list[str] = []
    if flag_dir:
        paths.extend([
            f"{flag_dir}/postprocess_done_{config_name}.txt",
            f"{flag_dir}/postprocess_done_{config_name}.txt.error",
        ])
    if output_dir:
        paths.extend([
            f"{output_dir}/model_gen4_{config_name}.csv",
            f"{output_dir}/model_gen4_{config_name}.json",
        ])
    if metrics_dir:
        paths.extend([
            f"{metrics_dir}/model_gen4_{config_name}.csv",
            f"{metrics_dir}/metrics_summary.csv",
        ])
    if animation_dir:
        paths.extend([
            f"{animation_dir}/t_gen4_{config_name}.mp4",
            f"{animation_dir}/v_gen4_{config_name}.mp4",
        ])
    if flag_dir:
        paths.extend([
            f"{flag_dir}/autofluid_bg_postprocess_{config_name}.cmd",
            f"{flag_dir}/autofluid_bg_postprocess_{config_name}.log",
            f"{flag_dir}/autofluid_bg_postprocess_{config_name}.pid",
        ])
    return paths


def _postprocess_metrics_config_dir(
    config: Mapping[str, object],
    config_name: int,
) -> str | None:
    metrics_dir = resolve_postprocess_paths(config)["metrics_dir"]
    if not metrics_dir:
        return None
    return f"{metrics_dir}/model_gen4_{config_name}"


def _postprocess_output_config_dir(
    config: Mapping[str, object],
    config_name: int,
) -> str | None:
    output_dir = resolve_postprocess_paths(config)["output_dir"]
    if not output_dir:
        return None
    return f"{output_dir}/model_gen4_{config_name}"


_CHECK_METADATA_KEYS = {"ok", "status", "summary"}


def _status_from_counts(passed: int, failed: int, warnings: int, *, empty_status: str = "skipped") -> str:
    if failed:
        return "failed"
    if warnings:
        return "warning"
    if passed:
        return "passed"
    return empty_status


def _state_from_check_item(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    severity = value.get("severity")
    if severity == "error":
        return "failed"
    if severity == "warning":
        return "warning"
    status = value.get("status")
    if status in {"passed", "failed", "warning"}:
        return str(status)
    ok = value.get("ok")
    if isinstance(ok, bool):
        return "passed" if ok else "failed"
    exists = value.get("exists")
    if isinstance(exists, bool):
        return "passed" if exists else "failed"
    return None


def _summarize_states(states: list[str]) -> dict[str, int]:
    return {
        "passed": states.count("passed"),
        "failed": states.count("failed"),
        "warnings": states.count("warning"),
    }


def _check_mapping_states(mapping: Mapping[str, object]) -> list[str]:
    states: list[str] = []
    for key, value in mapping.items():
        if key in _CHECK_METADATA_KEYS:
            continue
        state = _state_from_check_item(value)
        if state is not None:
            states.append(state)
    return states


def _apply_check_section_status(
    section: dict[str, object],
    *,
    empty_status: str = "skipped",
) -> None:
    counts = _summarize_states(_check_mapping_states(section))
    section["ok"] = counts["failed"] == 0
    section["status"] = _status_from_counts(
        counts["passed"],
        counts["failed"],
        counts["warnings"],
        empty_status=empty_status,
    )
    section["summary"] = counts


def _int_value(value: object) -> int:
    if isinstance(value, (int, float, str, bytes, bytearray)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


def _list_value(value: object) -> list[object]:
    return list(value) if isinstance(value, list | tuple) else []


def _remote_deployment_state(info: Mapping[str, object]) -> str | None:
    status = info.get("status")
    if status == "skipped":
        return None
    total = _int_value(info.get("total", 0))
    deployed = _int_value(info.get("deployed", 0))
    missing = _list_value(info.get("missing", []))
    if total == 0 and deployed == 0 and not missing:
        return None
    if missing or deployed < total:
        return "failed"
    return "passed"


def _remote_workstation_states(checks: Mapping[str, object]) -> list[str]:
    states: list[str] = []
    ssh_status = str(checks.get("ssh", ""))
    if ssh_status:
        states.append("passed" if "成功" in ssh_status else "failed")
    for key in ("conda_available", "ssh_connected"):
        value = checks.get(key)
        if isinstance(value, bool):
            states.append("passed" if value else "failed")
    python_version = checks.get("python_version")
    if isinstance(python_version, str):
        states.append("passed" if python_version else "failed")
    for collection_key in ("remote_dirs", "remote_programs"):
        for item in _list_value(checks.get(collection_key, [])):
            state = _state_from_check_item(item)
            if state is not None:
                states.append(state)
    for deployment_key in ("scripts_status", "ref_files_status"):
        deployment = checks.get(deployment_key)
        if isinstance(deployment, Mapping):
            state = _remote_deployment_state(deployment)
            if state is not None:
                states.append(state)
    return states


def _apply_remote_workstation_status(checks: dict[str, object]) -> None:
    counts = _summarize_states(_remote_workstation_states(checks))
    checks["ok"] = counts["failed"] == 0
    checks["status"] = _status_from_counts(
        counts["passed"],
        counts["failed"],
        counts["warnings"],
        empty_status="unknown",
    )
    checks["summary"] = counts


def _collect_result_summary(result: Mapping[str, object]) -> dict[str, int]:
    states: list[str] = []
    for section_name in ("local_checks", "daemon_checks"):
        section = result.get(section_name)
        if isinstance(section, Mapping):
            states.extend(_check_mapping_states(section))
    workstation_checks = result.get("workstation_checks")
    if isinstance(workstation_checks, Mapping):
        for workstation in workstation_checks.get("workstations") or []:
            state = _state_from_check_item(workstation)
            if state is not None:
                states.append(state)
    remote_checks = result.get("remote_checks")
    if isinstance(remote_checks, Mapping):
        workstations = remote_checks.get("workstations")
        if isinstance(workstations, Mapping):
            for workstation in workstations.values():
                if isinstance(workstation, Mapping):
                    states.extend(_remote_workstation_states(workstation))
    return _summarize_states(states)

class FileCleaner:
    """文件清理与系统自检器。"""

    def __init__(
        self,
        state_manager: StateManager,
        ssh_getter: Callable[..., "RemoteWorkstation"],
        ssh_lock: AbstractContextManager[object] | None = None,
        ssh_locks: dict[str, threading.RLock] | None = None,
        ssh_locks_guard: threading.Lock | None = None,
    ):
        """初始化清理器。

        Args:
            state_manager: StateManager 实例
            ssh_getter: 可调用对象，返回 RemoteWorkstation 实例
            ssh_lock: 保护共享 SSH/SFTP 客户端的上下文锁
            ssh_locks: 可选的按工作站锁池，与 TaskRunner/RemoteExecutor 共享
            ssh_locks_guard: 保护共享锁池的锁
        """
        self.state = state_manager
        self._get_ssh = ssh_getter
        self._ssh_lock = ssh_lock
        self._ssh_locks = ssh_locks
        self._ssh_locks_guard = ssh_locks_guard or threading.Lock()

    def _ssh_guard(
        self,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> AbstractContextManager[object]:
        """返回远端 SSH/SFTP 操作使用的锁上下文。"""
        if self._ssh_locks is None:
            return self._ssh_lock if self._ssh_lock is not None else nullcontext()
        with self._ssh_locks_guard:
            lock = self._ssh_locks.get(workstation_id)
            if lock is None:
                lock = threading.RLock()
                self._ssh_locks[workstation_id] = lock
            return lock

    def _get_ssh_for_workstation(
        self,
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> "RemoteWorkstation":
        """Return an SSH client for one workstation, preserving legacy getters."""
        try:
            return self._get_ssh(workstation_id)
        except TypeError:
            return self._get_ssh()

    @staticmethod
    def _remote_config_for_workstation(
        workstation_id: str = DEFAULT_WORKSTATION_ID,
    ) -> dict[str, object]:
        """Return remote paths/settings for one workstation."""
        if workstation_id == DEFAULT_WORKSTATION_ID:
            return dict(REMOTE_CONFIG)
        return dict(get_workstation_config(workstation_id))

    @staticmethod
    def _configured_workstation_ids() -> list[str]:
        ids = [
            str(workstation.get("id"))
            for workstation in WORKSTATIONS
            if workstation.get("id")
        ]
        return ids or [DEFAULT_WORKSTATION_ID]

    def _workstation_for_config(self, config_name: int) -> str:
        get_config_workstation = getattr(self.state, "get_config_workstation", None)
        if callable(get_config_workstation):
            workstation_id = get_config_workstation(config_name)
            if workstation_id:
                return str(workstation_id)
        return DEFAULT_WORKSTATION_ID

    def _workstations_for_config_cleanup(self, config_name: int) -> list[str]:
        workstation_id = self._workstation_for_config(config_name)
        configured_ids = self._configured_workstation_ids()
        if (
            workstation_id == DEFAULT_WORKSTATION_ID
            and DEFAULT_WORKSTATION_ID not in configured_ids
        ):
            return configured_ids
        return [workstation_id]

    # ------------------------------------------------------------------
    # 系统自检
    # ------------------------------------------------------------------

    def run_system_check(self) -> dict[str, object]:
        """执行系统自检：检查本地路径、SSH 连通性、远程进程状态。"""
        remote_checks: dict[str, object] = {}
        results: dict[str, object] = {
            "local_checks": {},
            "remote_checks": remote_checks,
            "daemon_checks": self._build_daemon_checks(),
            "workstation_checks": self._build_workstation_checks(),
        }

        if not is_server_mode():
            results["local_checks"] = self.run_local_system_check()

        remote_by_workstation: dict[str, dict[str, object]] = {}
        successful = 0
        failed = 0

        # ---- 远程检查 ----
        for workstation_id in self._configured_workstation_ids():
            workstation_checks: dict[str, object] = {}
            try:
                with self._ssh_guard(workstation_id):
                    remote_config = self._remote_config_for_workstation(workstation_id)
                    ssh = self._get_ssh_for_workstation(workstation_id)
                    if ssh.is_connected():
                        workstation_checks["ssh"] = "连接成功"
                        remote_info = ssh.check_system(
                            conda_exe=str(remote_config["conda_exe"]),
                            conda_env=str(remote_config["conda_env"]),
                            remote_dirs={
                                "仿真工作目录": str(remote_config["working_dir"]),
                                "脚本部署目录": str(remote_config["scripts_dir"]),
                                "引用文件目录": str(remote_config["ref_files_dir"]),
                                "SCDOC接收目录": str(remote_config["scdoc_dir"]),
                                "网格输出目录": str(remote_config["msh_dir"]),
                                "仿真输出目录": str(remote_config["result_dir"]),
                                "仿真标志目录": str(remote_config["flag_dir"]),
                            },
                            fluent_path=str(remote_config["fluent_path"]),
                            mpi_bin_dir=str(remote_config["mpi_bin_dir"]),
                            scripts_dir=str(remote_config["scripts_dir"]),
                            script_files=REMOTE_SCRIPT_FILES,
                            ref_files_dir=str(remote_config["ref_files_dir"]),
                            ref_files=REMOTE_REF_FILES,
                        )
                        if remote_info.get("ssh_connected") is False:
                            workstation_checks["ssh"] = "连接失败"
                            failed += 1
                        else:
                            successful += 1
                        workstation_checks.update(remote_info)
                    else:
                        workstation_checks.update(
                            self._default_remote_check_values(remote_config)
                        )
                        workstation_checks["ssh"] = "连接失败"
                        failed += 1
            except (OSError, ConnectionError) as e:
                logger.error(f"[SSH] 远程自检异常 ({workstation_id}): {e}")
                fallback_config = self._remote_config_for_workstation(workstation_id)
                workstation_checks.update(
                    self._default_remote_check_values(fallback_config)
                )
                workstation_checks["ssh"] = f"错误: {e}"
                failed += 1
            _apply_remote_workstation_status(workstation_checks)
            remote_by_workstation[workstation_id] = workstation_checks

        remote_checks["workstations"] = remote_by_workstation
        if successful and failed:
            remote_checks["status"] = "partial"
        elif successful:
            remote_checks["status"] = "passed"
        else:
            remote_checks["status"] = "failed"
        remote_checks["ok"] = failed == 0 and successful > 0
        remote_counts = {"passed": 0, "failed": 0, "warnings": 0}
        for workstation_checks in remote_by_workstation.values():
            summary = workstation_checks.get("summary")
            if isinstance(summary, Mapping):
                remote_counts["passed"] += int(summary.get("passed", 0) or 0)
                remote_counts["failed"] += int(summary.get("failed", 0) or 0)
                remote_counts["warnings"] += int(summary.get("warnings", 0) or 0)
        remote_checks["summary"] = remote_counts

        local_checks = results.get("local_checks")
        if isinstance(local_checks, dict):
            _apply_check_section_status(local_checks)
        daemon_checks = results.get("daemon_checks")
        if isinstance(daemon_checks, dict):
            _apply_check_section_status(daemon_checks)
        workstation_section = results.get("workstation_checks")
        if isinstance(workstation_section, dict):
            workstation_states = [
                _state_from_check_item(workstation)
                for workstation in _list_value(workstation_section.get("workstations", []))
            ]
            counts = _summarize_states([state for state in workstation_states if state is not None])
            workstation_section["ok"] = counts["failed"] == 0
            workstation_section["status"] = _status_from_counts(
                counts["passed"],
                counts["failed"],
                counts["warnings"],
            )
            workstation_section["summary"] = counts

        summary = _collect_result_summary(results)
        results["summary"] = summary
        results["overall_ok"] = summary["failed"] == 0
        results["status"] = _status_from_counts(
            summary["passed"],
            summary["failed"],
            summary["warnings"],
        )

        return results

    @staticmethod
    def _default_remote_check_values(
        remote_config: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Return remote-check fields for disconnected workstations."""
        remote_dirs: list[dict[str, object]] = []
        remote_programs: list[dict[str, object]] = []
        if remote_config is not None:
            remote_dirs.extend([
                {
                    "label": "仿真工作目录",
                    "path": str(remote_config.get("working_dir", "")),
                    "exists": None,
                },
                {
                    "label": "脚本部署目录",
                    "path": str(remote_config.get("scripts_dir", "")),
                    "exists": None,
                },
                {
                    "label": "引用文件目录",
                    "path": str(remote_config.get("ref_files_dir", "")),
                    "exists": None,
                },
                {
                    "label": "SCDOC接收目录",
                    "path": str(remote_config.get("scdoc_dir", "")),
                    "exists": None,
                },
                {
                    "label": "网格输出目录",
                    "path": str(remote_config.get("msh_dir", "")),
                    "exists": None,
                },
                {
                    "label": "仿真输出目录",
                    "path": str(remote_config.get("result_dir", "")),
                    "exists": None,
                },
                {
                    "label": "仿真标志目录",
                    "path": str(remote_config.get("flag_dir", "")),
                    "exists": None,
                },
            ])
            remote_programs.extend([
                {
                    "label": "Conda可执行文件",
                    "path": str(remote_config.get("conda_exe", "")),
                    "exists": None,
                },
                {
                    "label": "Conda环境",
                    "path": str(remote_config.get("conda_env", "")),
                    "exists": None,
                },
                {
                    "label": "Fluent可执行文件",
                    "path": str(remote_config.get("fluent_path", "")),
                    "exists": None,
                },
                {
                    "label": "MPI安装目录",
                    "path": str(remote_config.get("mpi_bin_dir", "")),
                    "exists": None,
                },
            ])
        return {
            "ssh_connected": False,
            "conda_available": False,
            "python_version": "",
            "disk_space": "",
            "background_processes": [],
            "remote_dirs": remote_dirs,
            "remote_programs": remote_programs,
            "scripts_status": {"status": "skipped", "message": "SSH 未连接，未检查"},
            "ref_files_status": {"status": "skipped", "message": "SSH 未连接，未检查"},
        }

    def _build_daemon_checks(self) -> dict[str, object]:
        """Build checks for the daemon host itself."""
        return {
            "server_mode": {"value": is_server_mode(), "ok": True},
            "ipc": {
                "host": IPC_CONFIG["host"],
                "port": IPC_CONFIG["port"],
                "auth_enabled": bool(IPC_CONFIG.get("auth_token")),
                "ok": True,
            },
            "data_dir": {
                "path": LOCAL_PATHS["data_dir"],
                "exists": os.path.isdir(LOCAL_PATHS["data_dir"]),
            },
            "scdoc_dir": {
                "path": LOCAL_PATHS["scdoc_dir"],
                "exists": os.path.isdir(LOCAL_PATHS["scdoc_dir"]),
            },
            "remote_scripts_dir": {
                "path": LOCAL_PATHS["remote_scripts_dir"],
                "exists": os.path.isdir(LOCAL_PATHS["remote_scripts_dir"]),
            },
        }

    def run_local_system_check(self) -> dict[str, dict[str, object]]:
        """Check only LocalWorker-side Windows paths."""
        local_checks: dict[str, dict[str, object]] = {}
        checks = {
            "SW可执行文件": LOCAL_PATHS["sw_exe"],
            "SW模型文件": LOCAL_PATHS["sw_model"],
            "Excel参数表": LOCAL_PATHS["excel"],
            "STEP输出目录": LOCAL_PATHS["step_dir"],
            "SC可执行文件": LOCAL_PATHS["sc_exe"],
            "SCDOC输出目录": LOCAL_PATHS["scdoc_dir"],
        }
        for name, path in checks.items():
            local_checks[name] = {"path": path, "exists": os.path.exists(path)}
        return local_checks

    def _build_workstation_checks(self) -> dict[str, object]:
        """Build deployment-oriented workstation reachability warnings."""
        server_mode = is_server_mode() or IPC_CONFIG.get("host") in {"0.0.0.0", "::"}
        workstations: list[dict[str, object]] = []
        for workstation in WORKSTATIONS:
            host = str(workstation.get("host", ""))
            reachable_host = str(workstation.get("reachable_host", "")).strip()
            connectivity_mode = str(workstation.get("connectivity_mode", "")).strip()
            effective_host = reachable_host or host
            warning = ""
            severity = "ok"
            if server_mode and _is_private_ip(host) and not _has_explicit_reachability(workstation):
                warning = (
                    "该工作站 host 是私网地址；daemon 部署在 ocar 时不能直接使用该地址，"
                    "必须配置从 ocar 可达的公网/VPN/隧道地址，并在 ocar 上验证 ssh 连通性"
                )
                severity = "error"
            workstations.append({
                "id": str(workstation.get("id", "")),
                "host": host,
                "effective_host": effective_host,
                "connectivity_mode": connectivity_mode,
                "port": int(workstation.get("port", 22)),
                "severity": severity,
                "warning": warning,
            })
        return {
            "server_mode": server_mode,
            "workstations": workstations,
        }

    # ------------------------------------------------------------------
    # 文件清理
    # ------------------------------------------------------------------

    def clean_step_files(self, step_name: str, config_name: int | str | None = None) -> None:
        """清理指定步骤产生的文件（包括本地和远程工作站上的文件）。

        Args:
            step_name: 步骤名，或 "all" 表示全部步骤
            config_name: 构型名称，若为 None 或 "all" 则清理所有构型
        """
        if config_name is None or config_name == "all":
            target_config_name = None
        else:
            target_config_name = int(config_name)

        if step_name == "all":
            failures: list[str] = []
            for s in STEP_NAMES:
                try:
                    self._clean_single_step(s, target_config_name)
                except RuntimeError as exc:
                    failures.append(str(exc))
                    logger.warning("[Cleaner] 步骤 %s 清理未完全成功: %s", s, exc)
            if failures:
                raise RuntimeError("远程文件清理未完成: " + "; ".join(failures))
        else:
            self._clean_single_step(step_name, target_config_name)

    def clean_local_step_files(
        self,
        step_name: str,
        config_name: int | str | None = None,
    ) -> None:
        """Clean only LocalWorker-side Windows step files."""
        if config_name is None or config_name == "all":
            target_config_name = None
        else:
            target_config_name = int(config_name)

        if step_name == "all":
            for s in STEP_NAMES:
                self._clean_local_single_step(s, target_config_name)
        else:
            self._clean_local_single_step(step_name, target_config_name)

    def clean_all_cache(self) -> None:
        """清空远程工作站上运行产生的临时缓存目录内容。"""
        try:
            total_deleted = 0
            total_failed = 0
            for workstation_id in self._configured_workstation_ids():
                with self._ssh_guard(workstation_id):
                    ssh = self._get_ssh_for_workstation(workstation_id)
                    if not ssh.is_connected():
                        logger.warning(
                            f"[Cleaner] SSH 未连接，跳过远程缓存清理: {workstation_id}"
                        )
                        total_failed += 1
                        continue

                    remote_config = self._remote_config_for_workstation(workstation_id)
                    for label, remote_dir in self._remote_cache_dirs(remote_config):
                        if not self._is_safe_remote_cache_dir(remote_dir):
                            logger.error(
                                f"[Cleaner] 拒绝清理不安全的远程目录 "
                                f"({workstation_id}/{label}): {remote_dir}"
                            )
                            total_failed += 1
                            continue

                        deleted_count, failed_count = ssh.clear_remote_directory(remote_dir)
                        total_deleted += deleted_count
                        total_failed += failed_count
                        logger.info(
                            f"[Cleaner] {workstation_id}/{label} 清理完成："
                            f"已删除 {deleted_count} 项，失败 {failed_count} 项 ({remote_dir})"
                        )

            logger.info(
                "[Cleaner] 远程缓存清理完成："
                f"已删除 {total_deleted} 项，失败 {total_failed} 项"
            )
            if total_failed:
                raise RuntimeError(
                    f"远程缓存清理未完成：已删除 {total_deleted} 项，失败 {total_failed} 项"
                )
        except (OSError, ConnectionError) as e:
            logger.error(f"[Cleaner] 远程缓存清理异常: {e}")
            from utils.infrastructure import InfrastructureUnavailableError
            raise InfrastructureUnavailableError(f"远程缓存清理未完成: {e}") from e

    def _clean_single_step(self, step_name: str, config_name: int | None = None) -> None:
        """清理单个步骤的文件（内部方法）。"""
        self._clean_local_single_step(step_name, config_name)
        self._clean_remote_single_step(step_name, config_name)
        logger.info(f"[Cleaner] 步骤 {step_name} 文件清理完成")

    def _clean_local_single_step(self, step_name: str, config_name: int | None = None) -> None:
        """清理单个步骤的本地文件。"""
        local_patterns = {
            "sw":       ("step_dir",  [STEP_FILE_PATTERNS["sw"]]),
            "sc":       ("scdoc_dir", [STEP_FILE_PATTERNS["sc"]]),
            "transfer": None,
            "meshing":  None,
            "solver":   None,
        }
        configs = [config_name] if config_name is not None else self.state.get_all_configs()
        local_info = local_patterns.get(step_name)
        if local_info is not None:
            dir_key, file_templates = local_info
            target_dir = str(LOCAL_PATHS.get(dir_key, ""))
            deleted_count = 0
            missing_count = 0
            for cn in configs:
                for file_template in file_templates:
                    filename = str(file_template).format(config=cn)
                    filepath = os.path.join(target_dir, filename)
                    try:
                        os.remove(filepath)
                        deleted_count += 1
                        logger.info(
                            f"[Cleaner] 已删除本地文件: {filepath}",
                            extra={"broadcast": False},
                        )
                    except FileNotFoundError:
                        missing_count += 1
                    except OSError as e:
                        logger.warning(f"[Cleaner] 删除本地文件失败: {filepath}: {e}")
            logger.info(
                f"[Cleaner] 步骤 {step_name} 本地文件清理完成："
                f"已删除 {deleted_count} 个，未找到 {missing_count} 个"
            )

    def _clean_remote_single_step(self, step_name: str, config_name: int | None = None) -> None:
        """清理单个步骤的远程文件。"""
        remote_patterns: dict[
            str,
            tuple[
                str | None,
                list[str],
                CallableABC[[int, Mapping[str, object]], list[str]] | None,
            ] | None,
        ] = {
            "sw":       None,
            "sc":       None,
            "transfer": ("scdoc_dir",  [STEP_FILE_PATTERNS["sc"]], None),
            "meshing":  (
                "msh_dir",
                [STEP_FILE_PATTERNS["meshing"]],
                lambda cn, cfg: _remote_flag_paths(cfg, "meshing_done", cn),
            ),
            "solver":   (
                "result_dir",
                [STEP_FILE_PATTERNS["solver"], STEP_FILE_PATTERNS["solverdata"]],
                lambda cn, cfg: _remote_flag_paths(cfg, "solver_done", cn),
            ),
            "postprocess": (
                None,
                [],
                lambda cn, cfg: _postprocess_cleanup_paths(cfg, cn),
            ),
        }
        configs = [config_name] if config_name is not None else self.state.get_all_configs()
        remote_info = remote_patterns.get(step_name)
        if remote_info is not None:
            dir_key = remote_info[0]
            file_templates = remote_info[1]
            extra_paths_factory = remote_info[2] if len(remote_info) > 2 else None
            try:
                processed_count = 0
                failed_count = 0
                for cn in configs:
                    workstation_ids = self._workstations_for_config_cleanup(int(cn))
                    for workstation_id in workstation_ids:
                        with self._ssh_guard(workstation_id):
                            ssh = self._get_ssh_for_workstation(workstation_id)
                            if not ssh.is_connected():
                                logger.warning(
                                    f"[Cleaner] SSH 未连接，跳过远程文件清理: "
                                    f"{step_name}/构型{cn}/{workstation_id}"
                                )
                                failed_count += len(file_templates)
                                continue
                            remote_config = self._remote_config_for_workstation(workstation_id)
                            target_dir = str(remote_config.get(dir_key, "")) if dir_key else ""
                            remote_dir = target_dir.replace("\\", "/").rstrip("/")
                            remote_paths = []
                            for file_template in file_templates:
                                filename = str(file_template).format(config=cn)
                                remote_paths.append(f"{remote_dir}/{filename}")
                            if callable(extra_paths_factory):
                                remote_paths.extend(
                                    path
                                    for path in extra_paths_factory(cn, remote_config)
                                    if path
                                )
                            deduped_remote_paths = list(dict.fromkeys(remote_paths))
                            for remote_path in deduped_remote_paths:
                                if ssh.delete_remote_file(remote_path):
                                    processed_count += 1
                                    logger.info(
                                        f"[Cleaner] 已处理远程文件清理: {remote_path}",
                                        extra={"broadcast": False},
                                    )
                                else:
                                    failed_count += 1
                            if step_name == "postprocess":
                                config_dirs = [
                                    _postprocess_output_config_dir(remote_config, int(cn)),
                                    _postprocess_metrics_config_dir(remote_config, int(cn)),
                                ]
                                clear_remote_directory = getattr(
                                    ssh, "clear_remote_directory", None
                                )
                                if callable(clear_remote_directory):
                                    for config_dir in config_dirs:
                                        if not config_dir:
                                            continue
                                        _, clear_failed_count = clear_remote_directory(config_dir)
                                        if clear_failed_count:
                                            failed_count += clear_failed_count
                logger.info(
                    f"[Cleaner] 步骤 {step_name} 远程文件清理完成："
                    f"已处理 {processed_count} 个，失败 {failed_count} 个"
                )
                if failed_count:
                    raise RuntimeError(
                        f"远程文件清理未完成: step={step_name}, "
                        f"已处理 {processed_count} 个，失败 {failed_count} 个"
                    )
            except (OSError, ConnectionError) as e:
                logger.error(f"[Cleaner] 远程文件清理异常 ({step_name}): {e}")
                from utils.infrastructure import InfrastructureUnavailableError
                raise InfrastructureUnavailableError(f"远程文件清理未完成: step={step_name}: {e}") from e

    def _remote_cache_dirs(
        self,
        remote_config: Mapping[str, object] | None = None,
    ) -> list[tuple[str, str]]:
        """返回 clean all cache 覆盖的远程缓存目录。"""
        config = remote_config or REMOTE_CONFIG
        working_dir = self._normalize_remote_dir(str(config.get("working_dir", "")))
        flag_dir = self._normalize_remote_dir(str(config.get("flag_dir", "")))
        targets = [
            ("仿真工作目录", working_dir),
            ("仿真标志目录", flag_dir),
        ]

        animation_dirs = [
            ("动画临时目录 T", f"{working_dir}/animation-t" if working_dir else ""),
            ("动画临时目录 V", f"{working_dir}/animation-v" if working_dir else ""),
        ]
        for label, animation_dir in animation_dirs:
            if animation_dir and working_dir and self._is_same_or_child_dir(animation_dir, working_dir):
                logger.debug(f"[Cleaner] {label} 已包含在仿真工作目录清理范围内: {animation_dir}")
                continue
            targets.append((label, animation_dir))
        return targets

    @staticmethod
    def _normalize_remote_dir(remote_dir: str) -> str:
        return remote_dir.replace("\\", "/").rstrip("/")

    @classmethod
    def _is_safe_remote_cache_dir(cls, remote_dir: str) -> bool:
        normalized = cls._normalize_remote_dir(remote_dir)
        if not normalized or normalized in {".", "..", "/"}:
            return False
        if len(normalized) == 2 and normalized[1] == ":":
            return False
        if len(normalized) == 3 and normalized[1] == ":" and normalized[2] == "/":
            return False
        return True

    @classmethod
    def _is_same_or_child_dir(cls, child_dir: str, parent_dir: str) -> bool:
        child = cls._normalize_remote_dir(child_dir).lower()
        parent = cls._normalize_remote_dir(parent_dir).lower()
        return child == parent or child.startswith(f"{parent}/")
