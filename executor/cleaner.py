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

import os
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from utils.ssh_client import RemoteWorkstation
    from engine.state_manager import StateManager

from engine.config import (
    LOCAL_PATHS, REMOTE_CONFIG,
    STEP_NAMES, STEP_FILE_PATTERNS,
)
from executor.remote_executor import REMOTE_SCRIPT_FILES, REMOTE_REF_FILES
from utils.logger import setup_logger

logger = setup_logger(__name__)


class FileCleaner:
    """文件清理与系统自检器。"""

    def __init__(self, state_manager: StateManager, ssh_getter: Callable[[], "RemoteWorkstation"]):
        """初始化清理器。

        Args:
            state_manager: StateManager 实例
            ssh_getter: 可调用对象，返回 RemoteWorkstation 实例
        """
        self.state = state_manager
        self._get_ssh = ssh_getter

    # ------------------------------------------------------------------
    # 系统自检
    # ------------------------------------------------------------------

    def run_system_check(self) -> dict:
        """执行系统自检：检查本地路径、SSH 连通性、远程进程状态。"""
        results: dict[str, dict[str, object]] = {
            "local_checks": {},
            "remote_checks": {},
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

    # ------------------------------------------------------------------
    # 文件清理
    # ------------------------------------------------------------------

    def clean_step_files(self, step_name, config_name=None):
        """清理指定步骤产生的文件（包括本地和远程工作站上的文件）。

        Args:
            step_name: 步骤名，或 "all" 表示全部步骤
            config_name: 构型名称，若为 None 或 "all" 则清理所有构型
        """
        if config_name == "all":
            config_name = None

        if step_name == "all":
            for s in STEP_NAMES:
                self._clean_single_step(s, config_name)
        else:
            self._clean_single_step(step_name, config_name)

    def _clean_single_step(self, step_name: str, config_name: int | None = None):
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
            for cn in configs:
                for file_template in file_templates:
                    filename = str(file_template).format(config=cn)
                    filepath = os.path.join(target_dir, filename)
                    try:
                        os.remove(filepath)
                        logger.info(f"[Cleaner] 已删除本地文件: {filepath}")
                    except FileNotFoundError:
                        pass
                    except OSError as e:
                        logger.warning(f"[Cleaner] 删除本地文件失败: {filepath}: {e}")

        # ---- 清理远程文件 ----
        remote_info = remote_patterns.get(step_name)
        if remote_info is not None:
            dir_key, file_templates = remote_info
            target_dir = str(REMOTE_CONFIG.get(dir_key, ""))
            try:
                ssh = self._get_ssh()
                if ssh.is_connected():
                    for cn in configs:
                        for file_template in file_templates:
                            filename = str(file_template).format(config=cn)
                            remote_path = f"{target_dir.replace(chr(92), '/')}/{filename}"
                            ssh.delete_remote_file(remote_path)
                            logger.info(f"[Cleaner] 已删除远程文件: {remote_path}")
                    logger.info(f"[Cleaner] 步骤 {step_name} 远程文件清理完成 ({target_dir})")
                else:
                    logger.warning(f"[Cleaner] SSH 未连接，跳过远程文件清理: {step_name}")
            except (OSError, ConnectionError) as e:
                logger.error(f"[Cleaner] 远程文件清理异常 ({step_name}): {e}")

        logger.info(f"[Cleaner] 步骤 {step_name} 文件清理完成")
