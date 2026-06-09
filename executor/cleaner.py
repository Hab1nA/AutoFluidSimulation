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

from contextlib import AbstractContextManager, nullcontext
import ipaddress
import os
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from utils.ssh_client import RemoteWorkstation
    from engine.state_manager import StateManager

from engine.config import (
    IPC_CONFIG, LOCAL_PATHS, REMOTE_CONFIG, WORKSTATIONS,
    STEP_NAMES, STEP_FILE_PATTERNS, is_server_mode,
)
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


class FileCleaner:
    """文件清理与系统自检器。"""

    def __init__(
        self,
        state_manager: StateManager,
        ssh_getter: Callable[[], "RemoteWorkstation"],
        ssh_lock: AbstractContextManager[object] | None = None,
    ):
        """初始化清理器。

        Args:
            state_manager: StateManager 实例
            ssh_getter: 可调用对象，返回 RemoteWorkstation 实例
            ssh_lock: 保护共享 SSH/SFTP 客户端的上下文锁
        """
        self.state = state_manager
        self._get_ssh = ssh_getter
        self._ssh_lock = ssh_lock

    def _ssh_guard(self) -> AbstractContextManager[object]:
        """返回远端 SSH/SFTP 操作使用的锁上下文。"""
        return self._ssh_lock if self._ssh_lock is not None else nullcontext()

    # ------------------------------------------------------------------
    # 系统自检
    # ------------------------------------------------------------------

    def run_system_check(self) -> dict[str, dict[str, object]]:
        """执行系统自检：检查本地路径、SSH 连通性、远程进程状态。"""
        results: dict[str, dict[str, object]] = {
            "local_checks": {},
            "remote_checks": {},
            "workstation_checks": self._build_workstation_checks(),
        }

        # 仅检查 settings 页面「本地文件路径」分类中展示的 6 个用户可配置路径
        checks = {
            "SW可执行文件": LOCAL_PATHS["sw_exe"],
            "SW模型文件": LOCAL_PATHS["sw_model"],
            "Excel参数表": LOCAL_PATHS["excel"],
            "STEP输出目录": LOCAL_PATHS["step_dir"],
            "SC可执行文件": LOCAL_PATHS["sc_exe"],
            "SCDOC输出目录": LOCAL_PATHS["scdoc_dir"],
        }
        for name, path in checks.items():
            exists = os.path.exists(path)
            results["local_checks"][name] = {"path": path, "exists": exists}

        # ---- 远程检查 ----
        try:
            with self._ssh_guard():
                ssh = self._get_ssh()
                if ssh.is_connected():
                    results["remote_checks"]["ssh"] = "连接成功"
                    remote_info = ssh.check_system(
                        conda_exe=REMOTE_CONFIG["conda_exe"],
                        conda_env=REMOTE_CONFIG["conda_env"],
                        remote_dirs={
                            "仿真工作目录": REMOTE_CONFIG["working_dir"],
                            "脚本部署目录": REMOTE_CONFIG["scripts_dir"],
                            "引用文件目录": REMOTE_CONFIG["ref_files_dir"],
                            "SCDOC接收目录": REMOTE_CONFIG["scdoc_dir"],
                            "网格输出目录": REMOTE_CONFIG["msh_dir"],
                            "仿真输出目录": REMOTE_CONFIG["result_dir"],
                            "仿真标志目录": REMOTE_CONFIG["flag_dir"],
                        },
                        mpi_bin_dir=REMOTE_CONFIG["mpi_bin_dir"],
                        scripts_dir=REMOTE_CONFIG["scripts_dir"],
                        script_files=REMOTE_SCRIPT_FILES,
                        ref_files_dir=REMOTE_CONFIG["ref_files_dir"],
                        ref_files=REMOTE_REF_FILES,
                    )
                    results["remote_checks"].update(remote_info)
                else:
                    results["remote_checks"]["ssh"] = "连接失败"
        except (OSError, ConnectionError) as e:
            logger.error(f"[SSH] 远程自检异常: {e}")
            results["remote_checks"]["ssh"] = f"错误: {e}"
            # 保持结构一致性：填充默认值，避免 TUI 缺失字段
            results["remote_checks"].update({
                "ssh_connected": False,
                "conda_available": False,
                "python_version": "",
                "disk_space": "",
                "background_processes": [],
                "remote_dirs": [],
                "remote_programs": [],
                "scripts_status": {"total": 0, "deployed": 0, "missing": []},
                "ref_files_status": {"total": 0, "deployed": 0, "missing": []},
            })

        return results

    def _build_workstation_checks(self) -> dict[str, object]:
        """Build deployment-oriented workstation reachability warnings."""
        server_mode = is_server_mode() or IPC_CONFIG.get("host") in {"0.0.0.0", "::"}
        workstations: list[dict[str, object]] = []
        for workstation in WORKSTATIONS:
            host = str(workstation.get("host", ""))
            warning = ""
            if server_mode and _is_private_ip(host):
                warning = (
                    "该工作站 host 是私网地址；daemon 部署在 ocar 时必须替换为 "
                    "从 ocar 可达的公网/VPN/隧道地址，并在 ocar 上验证 ssh 连通性"
                )
            workstations.append({
                "id": str(workstation.get("id", "")),
                "host": host,
                "port": int(workstation.get("port", 22)),
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
            for s in STEP_NAMES:
                self._clean_single_step(s, target_config_name)
        else:
            self._clean_single_step(step_name, target_config_name)

    def clean_all_cache(self) -> None:
        """清空远程工作站上运行产生的临时缓存目录内容。"""
        try:
            with self._ssh_guard():
                ssh = self._get_ssh()
                if not ssh.is_connected():
                    logger.warning("[Cleaner] SSH 未连接，跳过远程缓存清理")
                    return

                total_deleted = 0
                total_failed = 0
                for label, remote_dir in self._remote_cache_dirs():
                    if not self._is_safe_remote_cache_dir(remote_dir):
                        logger.error(f"[Cleaner] 拒绝清理不安全的远程目录 ({label}): {remote_dir}")
                        total_failed += 1
                        continue

                    deleted_count, failed_count = ssh.clear_remote_directory(remote_dir)
                    total_deleted += deleted_count
                    total_failed += failed_count
                    logger.info(
                        f"[Cleaner] {label} 清理完成："
                        f"已删除 {deleted_count} 项，失败 {failed_count} 项 ({remote_dir})"
                    )

                logger.info(
                    "[Cleaner] 远程缓存清理完成："
                    f"已删除 {total_deleted} 项，失败 {total_failed} 项"
                )
        except (OSError, ConnectionError) as e:
            logger.error(f"[Cleaner] 远程缓存清理异常: {e}")

    def _clean_single_step(self, step_name: str, config_name: int | None = None) -> None:
        """清理单个步骤的文件（内部方法）。"""
        local_patterns = {
            "sw":       ("step_dir",  [STEP_FILE_PATTERNS["sw"]]),
            "sc":       ("scdoc_dir", [STEP_FILE_PATTERNS["sc"]]),
            "transfer": None,
            "meshing":  None,
            "solver":   None,
        }

        remote_patterns = {
            "sw":       None,
            "sc":       ("scdoc_dir",  [STEP_FILE_PATTERNS["sc"]]),
            "transfer": None,
            "meshing":  ("msh_dir",    [STEP_FILE_PATTERNS["meshing"]]),
            "solver":   ("result_dir", [STEP_FILE_PATTERNS["solver"], STEP_FILE_PATTERNS["solverdata"]]),
        }

        configs = [config_name] if config_name is not None else self.state.get_all_configs()

        # ---- 清理本地文件 ----
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

        # ---- 清理远程文件 ----
        remote_info = remote_patterns.get(step_name)
        if remote_info is not None:
            dir_key, file_templates = remote_info
            target_dir = str(REMOTE_CONFIG.get(dir_key, ""))
            try:
                with self._ssh_guard():
                    ssh = self._get_ssh()
                    if ssh.is_connected():
                        processed_count = 0
                        failed_count = 0
                        for cn in configs:
                            for file_template in file_templates:
                                filename = str(file_template).format(config=cn)
                                remote_path = f"{target_dir.replace(chr(92), '/')}/{filename}"
                                if ssh.delete_remote_file(remote_path):
                                    processed_count += 1
                                    logger.info(
                                        f"[Cleaner] 已处理远程文件清理: {remote_path}",
                                        extra={"broadcast": False},
                                    )
                                else:
                                    failed_count += 1
                        logger.info(
                            f"[Cleaner] 步骤 {step_name} 远程文件清理完成："
                            f"已处理 {processed_count} 个，失败 {failed_count} 个 ({target_dir})"
                        )
                    else:
                        logger.warning(f"[Cleaner] SSH 未连接，跳过远程文件清理: {step_name}")
            except (OSError, ConnectionError) as e:
                logger.error(f"[Cleaner] 远程文件清理异常 ({step_name}): {e}")

        logger.info(f"[Cleaner] 步骤 {step_name} 文件清理完成")

    def _remote_cache_dirs(self) -> list[tuple[str, str]]:
        """返回 clean all cache 覆盖的远程缓存目录。"""
        working_dir = self._normalize_remote_dir(str(REMOTE_CONFIG.get("working_dir", "")))
        flag_dir = self._normalize_remote_dir(str(REMOTE_CONFIG.get("flag_dir", "")))
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
