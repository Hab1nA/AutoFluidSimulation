"""
===============================================================================
文件清理与系统自检模块 (Cleaner)

负责：
- 系统自检（本地路径、SSH 连通性、远程进程状态）
- 文件清理（本地 + 远程工作站上的步骤产出文件）

从 engine/task_runner.py 中提取。
===============================================================================
"""

import os
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from utils.ssh_client import RemoteWorkstation

from engine.config import (
    LOCAL_PATHS, REMOTE_CONFIG,
    STEP_NAMES, STEP_FILE_PATTERNS,
    get_step_filename,
)
from utils.logger import setup_logger

logger = setup_logger(__name__)


class FileCleaner:
    """文件清理与系统自检器。"""

    def __init__(self, state_manager: Any, ssh_getter: Callable[[], "RemoteWorkstation"]):
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

        checks = {
            "SW可执行文件": LOCAL_PATHS["sw_exe"],
            "SW模型文件": LOCAL_PATHS["sw_model"],
            "Excel参数表": LOCAL_PATHS["excel"],
            "STEP输出目录": LOCAL_PATHS["step_dir"],
            "SC可执行文件": LOCAL_PATHS["sc_exe"],
            "SC脚本文件": LOCAL_PATHS["sc_script"],
            "SCDOC输出目录": LOCAL_PATHS["scdoc_dir"],
            "日志目录": LOCAL_PATHS["log_dir"],
            "数据目录": LOCAL_PATHS["data_dir"],
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
                )
                results["remote_checks"].update(remote_info)
            else:
                results["remote_checks"]["ssh"] = "连接失败"
        except (OSError, ConnectionError) as e:
            results["remote_checks"]["ssh"] = f"错误: {e}"

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

    def _clean_single_step(self, step_name: str, config_name: Optional[int] = None):
        """清理单个步骤的文件（内部方法）。"""
        local_patterns = {
            "SW":       ("step_dir",  STEP_FILE_PATTERNS["SW"],       None),
            "SC":       ("scdoc_dir", STEP_FILE_PATTERNS["SC"],       None),
            "Transfer": None,
            "Meshing":  None,
            "Solver":   None,
        }

        remote_patterns = {
            "SW":       None,
            "SC":       ("scdoc_dir",  STEP_FILE_PATTERNS["SC"],      None),
            "Transfer": None,
            "Meshing":  ("msh_dir",    STEP_FILE_PATTERNS["Meshing"], None),
            "Solver":   ("result_dir", STEP_FILE_PATTERNS["Solver"],  None),
        }

        configs = [config_name] if config_name is not None else self.state.get_all_configs()

        # ---- 清理本地文件 ----
        local_info = local_patterns.get(step_name)
        if local_info is not None:
            dir_key, file_template, extra_suffix_pair = local_info
            target_dir = str(LOCAL_PATHS.get(dir_key, ""))
            for cn in configs:
                filename = str(file_template).format(config=cn)
                filepath = os.path.join(target_dir, filename)
                try:
                    os.remove(filepath)
                    logger.info(f"已删除本地文件: {filepath}")
                except FileNotFoundError:
                    pass
                except OSError as e:
                    logger.warning(f"删除本地文件失败: {filepath}: {e}")
                if extra_suffix_pair:
                    old_suffix, new_suffix = extra_suffix_pair
                    extra_filename = filename.rsplit(old_suffix, 1)[0] + new_suffix
                    extra_path = os.path.join(target_dir, extra_filename)
                    try:
                        os.remove(extra_path)
                        logger.info(f"已删除本地文件: {extra_path}")
                    except FileNotFoundError:
                        pass
                    except OSError as e:
                        logger.warning(f"删除本地文件失败: {extra_path}: {e}")

        # ---- 清理远程文件 ----
        remote_info = remote_patterns.get(step_name)
        if remote_info is not None:
            dir_key, file_template, _extra = remote_info
            target_dir = str(REMOTE_CONFIG.get(dir_key, ""))
            try:
                ssh = self._get_ssh()
                if ssh.is_connected():
                    for cn in configs:
                        filename = str(file_template).format(config=cn)
                        remote_path = f"{target_dir.replace(chr(92), '/')}/{filename}"
                        ssh.delete_remote_file(remote_path)
                    logger.info(f"步骤 {step_name} 远程文件清理完成 ({target_dir})")
                else:
                    logger.warning(f"SSH 未连接，跳过远程文件清理: {step_name}")
            except (OSError, ConnectionError) as e:
                logger.error(f"远程文件清理异常 ({step_name}): {e}")

        # Solver 步骤额外清理 dat.h5 文件（从 cas.h5 模板推导）
        if step_name == "Solver":
            dat_dir = str(REMOTE_CONFIG.get("result_dir", ""))
            try:
                ssh = self._get_ssh()
                if ssh.is_connected():
                    for cn in configs:
                        dat_name = get_step_filename("Solver_dat", cn)
                        if dat_name:
                            dat_path = f"{dat_dir.replace(chr(92), '/')}/{dat_name}"
                            ssh.delete_remote_file(dat_path)
                    logger.info(f"步骤 Solver(dat) 远程文件清理完成 ({dat_dir})")
            except (OSError, ConnectionError) as e:
                logger.error(f"远程 Solver(dat) 文件清理异常: {e}")

        logger.info(f"步骤 {step_name} 文件清理完成")
