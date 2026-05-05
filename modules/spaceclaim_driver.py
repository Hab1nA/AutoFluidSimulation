# =============================================================================
# spaceclaim_driver.py — SpaceClaim 几何处理与面标注自动化（阶段3）
#
# 功能：
#   1. 使用 subprocess 无头调用 SpaceClaim 命令行
#   2. 执行 .scscript 脚本处理 .step 文件
#   3. 导出 .scdoc 文件，带超时保护
# =============================================================================
import os
import subprocess
import time
import logging
import traceback
from pathlib import Path
from typing import Optional

from config import LOCAL_CONFIG, GLOBAL_CONFIG

logger = logging.getLogger(__name__)


class SpaceClaimDriver:
    """
    SpaceClaim 无头模式驱动器。

    通过命令行调用 SpaceClaim.exe 并以 /RunScript 参数
    执行预置的 .scscript 脚本完成几何处理和面标注。
    """

    def __init__(self):
        """初始化路径配置"""
        self.spaceclaim_exe = LOCAL_CONFIG["spaceclaim_exe"]
        self.sc_script = LOCAL_CONFIG["sc_script"]
        self.step_dir = LOCAL_CONFIG["step_dir"]
        self.scdoc_dir = LOCAL_CONFIG["scdoc_dir"]
        self.timeout = GLOBAL_CONFIG["sc_timeout"]

    def _verify_sc_script(self) -> bool:
        """验证 SpaceClaim 脚本文件是否存在"""
        if not os.path.isfile(self.sc_script):
            logger.error("SpaceClaim 脚本文件不存在: %s", self.sc_script)
            return False
        return True

    def _verify_spaceclaim_exe(self) -> bool:
        """验证 SpaceClaim 可执行程序是否存在"""
        if not os.path.isfile(self.spaceclaim_exe):
            logger.error("SpaceClaim 可执行程序不存在: %s", self.spaceclaim_exe)
            return False
        return True

    def _get_latest_step_file(self, config_id: Optional[str] = None) -> Optional[Path]:
        """
        获取 STEP 目录中与当前构型匹配的 STEP 文件。

        优先匹配文件名中包含 config_id 的文件（确保多构型并行时不误选）。
        若未指定 config_id 或未找到匹配文件，则降级为取最新修改时间的文件。

        Args:
            config_id: 构型 ID，用于精确匹配文件名

        Returns:
            匹配的 STEP 文件的 Path 对象，若目录为空则返回 None
        """
        step_files = (
            list(Path(self.step_dir).glob("*.step"))
            + list(Path(self.step_dir).glob("*.STEP"))
            + list(Path(self.step_dir).glob("*.stp"))
            + list(Path(self.step_dir).glob("*.STP"))
        )
        if not step_files:
            logger.warning("STEP 目录中没有找到 STEP 文件: %s", self.step_dir)
            return None

        # 优先精确匹配：文件名包含 config_id
        if config_id:
            matched = [f for f in step_files if config_id.lower() in f.name.lower()]
            if matched:
                # 多个匹配项时取最新的
                chosen = max(matched, key=lambda f: f.stat().st_mtime)
                logger.info("通过 config_id 精确匹配到 STEP 文件: %s", chosen.name)
                return chosen
            logger.warning(
                "STEP 目录中未找到包含 config_id='%s' 的文件，"
                "将回退为取最新修改时间的文件（可能误选）",
                config_id,
            )

        # 降级策略：取最新文件（向后兼容）
        return max(step_files, key=lambda f: f.stat().st_mtime)

    def _verify_scdoc_output(self) -> bool:
        """
        验证 SCDOC 文件是否成功生成。

        Returns:
            SCDOC 目录中是否存在 .scdoc 文件
        """
        scdoc_files = list(Path(self.scdoc_dir).glob("*.scdoc"))
        if scdoc_files:
            latest = max(scdoc_files, key=lambda f: f.stat().st_mtime)
            logger.info("找到 SCDOC 文件: %s (修改时间: %s)", latest.name,
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest.stat().st_mtime)))
            return True
        else:
            logger.warning("SCDOC 目录中未找到任何 .scdoc 文件: %s", self.scdoc_dir)
            return False

    # -----------------------------------------------------------------------
    # 主流程：无头调用 SpaceClaim
    # -----------------------------------------------------------------------
    def process_config(self, config_id: str) -> bool:
        """
        执行 SpaceClaim 几何处理流程。

        SpaceClaim 脚本 (spaceclaim_transit.scscript) 负责：
        1. 读取 STEP 目录中最新的 .step 文件
        2. 执行几何处理与面标注
        3. 将结果导出为 .scdoc 文件到 SCDOC 目录

        Args:
            config_id: 构型 ID（用于日志记录）

        Returns:
            是否成功完成
        """
        logger.info("=" * 60)
        logger.info("阶段3 (SpaceClaim) — 开始处理构型: %s", config_id)
        logger.info("=" * 60)

        # --- 前置检查 ---
        if not self._verify_spaceclaim_exe():
            return False
        if not self._verify_sc_script():
            return False

        step_file = self._get_latest_step_file(config_id)
        if step_file is None:
            logger.error("构型 %s: 没有可用的 STEP 文件供 SpaceClaim 处理", config_id)
            return False
        logger.info("将处理 STEP 文件: %s", step_file.name)

        # --- 构建命令行 ---
        # SpaceClaim 无头运行命令格式:
        #   SpaceClaim.exe /RunScript="script.scscript" /Headless=True /Splash=False
        cmd = [
            self.spaceclaim_exe,
            f'/RunScript="{self.sc_script}"',
            "/Headless=True",
            "/Splash=False",
        ]
        cmd_str = " ".join(cmd)
        logger.info("执行 SpaceClaim 命令:\n  %s", cmd_str)

        # --- 执行（带超时保护）---
        try:
            process = subprocess.run(
                cmd_str,
                shell=True,                 # Windows 下需要 shell 来解析引号
                capture_output=True,
                text=True,
                timeout=self.timeout,       # 超时保护（防止 SC 卡死）
                cwd=os.path.dirname(self.spaceclaim_exe),
            )

            logger.info("SpaceClaim 进程退出码: %d", process.returncode)

            if process.stdout:
                logger.debug("SC stdout:\n%s", process.stdout[:2000])
            if process.stderr:
                logger.warning("SC stderr:\n%s", process.stderr[:2000])

            if process.returncode != 0:
                logger.error("SpaceClaim 进程返回非零退出码: %d", process.returncode)
                return False

        except subprocess.TimeoutExpired:
            logger.error(
                "SpaceClaim 进程超时（%d 秒），已强制终止。构型: %s",
                self.timeout,
                config_id,
            )
            return False
        except FileNotFoundError:
            logger.error(
                "无法找到 SpaceClaim 可执行文件: %s\n请确认 ANSYS 安装路径是否正确。",
                self.spaceclaim_exe,
            )
            return False
        except Exception as e:
            logger.error("SpaceClaim 执行异常: %s", traceback.format_exc())
            return False

        # --- 验证输出 ---
        time.sleep(2)  # 等待文件系统同步
        if not self._verify_scdoc_output():
            logger.error("构型 %s: SpaceClaim 处理完成但未生成 SCDOC 文件", config_id)
            return False

        logger.info("构型 %s 的 SpaceClaim 阶段完成 ✓", config_id)
        return True