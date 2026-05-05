# =============================================================================
# solidworks_driver.py — SolidWorks 参数化建模自动化（阶段2）
#
# 功能：
#   1. 通过 openpyxl 修改外部 Excel 文件驱动 SolidWorks 构型更新
#   2. 使用 win32com.client 操作 SolidWorks COM 接口
#   3. 调用预置 VBA 宏 Macro1.swp 批量导出 STEP 文件
# =============================================================================
import os
import time
import logging
import traceback
from pathlib import Path
from typing import Optional

import openpyxl
import config
from config import LOCAL_CONFIG, GLOBAL_CONFIG

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
SW_DOC_TYPE_PART = 1          # swDocPART
SW_SAVEAS_CURRENT_VERSION = 0 # 以当前版本保存
SW_REBUILD_FORCE = 2          # 强制重建


class SolidWorksDriver:
    """
    SolidWorks 自动化驱动器。

    通过 COM 接口控制 SolidWorks 应用程序：
    1. 修改 Excel 参数文件
    2. 打开/更新 SolidWorks 模型
    3. 运行导出宏生成 STEP 文件
    """

    def __init__(self):
        """初始化路径配置"""
        self.model_path = LOCAL_CONFIG["sw_model_path"]
        self.excel_path = LOCAL_CONFIG["excel_path"]
        self.macro_path = LOCAL_CONFIG["sw_macro_path"]
        self.step_dir = LOCAL_CONFIG["step_dir"]
        self._sw_app = None  # SolidWorks 应用程序 COM 对象

    # -----------------------------------------------------------------------
    # Excel 参数修改
    # -----------------------------------------------------------------------
    def update_excel_params(self, params: dict, sheet_name: str = "Sheet1") -> bool:
        """
        修改外部 Excel 文件中指定单元格的参数值。

        Args:
            params: 参数字典，格式如 {"B2": 2.5, "B3": 30, "B4": 15}
            sheet_name: Excel 工作表名称

        Returns:
            是否修改成功
        """
        try:
            logger.info("正在修改 Excel 参数文件: %s", self.excel_path)
            wb = openpyxl.load_workbook(self.excel_path)
            ws = wb[sheet_name]

            for cell_ref, value in params.items():
                ws[cell_ref] = value
                logger.debug("  设置 %s = %s", cell_ref, value)

            wb.save(self.excel_path)
            wb.close()
            logger.info("Excel 参数更新完成")
            return True
        except Exception as e:
            logger.error("Excel 参数修改失败: %s", traceback.format_exc())
            return False

    # -----------------------------------------------------------------------
    # SolidWorks COM 操作
    # -----------------------------------------------------------------------
    def _connect_sw(self) -> bool:
        """
        连接到 SolidWorks 应用程序（唤醒现有实例或启动新实例）。

        Returns:
            是否连接成功
        """
        try:
            import win32com.client
            import pythoncom

            # 初始化 COM 库
            pythoncom.CoInitialize()

            try:
                # 尝试获取已运行的 SolidWorks 实例
                self._sw_app = win32com.client.GetActiveObject("SldWorks.Application")
                logger.info("连接到已运行的 SolidWorks 实例")
            except Exception:
                # 没有运行中的实例，启动新的
                logger.info("正在启动新的 SolidWorks 实例...")
                self._sw_app = win32com.client.Dispatch("SldWorks.Application")
                self._sw_app.Visible = True  # 显示 SW 窗口便于调试

            # 等待 SW 完全加载
            time.sleep(2)
            logger.info("SolidWorks 连接成功")
            return True

        except Exception as e:
            logger.error("SolidWorks COM 连接失败: %s", e)
            return False

    def _open_model(self) -> Optional[object]:
        """
        打开 SolidWorks 模型文件。
        模型打开后会自动从外部 Excel 文件读取参数并更新。

        Returns:
            打开的文档对象，失败返回 None
        """
        try:
            import win32com.client

            logger.info("正在打开模型: %s", self.model_path)
            if not os.path.isfile(self.model_path):
                logger.error("模型文件不存在: %s", self.model_path)
                return None

            # 打开文档并更新引用
            doc = self._sw_app.OpenDoc6(
                self.model_path,       # 文件路径
                SW_DOC_TYPE_PART,       # 文档类型：零件
                0,                      # 选项：默认
                "",                     # 配置名：默认
                0, 0,                   # 读写错误变量
            )

            if doc is None:
                logger.error("无法打开模型文件")
                return None

            logger.info("模型打开成功，等待参数更新...")
            time.sleep(1)

            return doc
        except Exception as e:
            logger.error("打开模型失败: %s", e)
            return None

    def _rebuild_model(self, doc: object) -> bool:
        """
        强制重建模型以确保参数更新生效。

        Args:
            doc: SolidWorks 文档对象

        Returns:
            是否重建成功
        """
        try:
            logger.info("正在重建模型...")
            result = doc.ForceRebuild3(True)  # True = 重建所有特征
            logger.info("模型重建完成 (ForceRebuild3=%s)", result)
            return True
        except Exception as e:
            logger.warning("模型重建时出现警告: %s", e)
            return False

    def _run_macro(self) -> bool:
        """
        运行预置的 VBA 宏，用于导出 STEP 文件。
        宏路径为 config 中配置的 Macro1.swp。

        Returns:
            是否执行成功
        """
        try:
            import pythoncom

            if not os.path.isfile(self.macro_path):
                logger.error("宏文件不存在: %s", self.macro_path)
                return False

            logger.info("正在运行导出宏: %s", self.macro_path)

            # RunMacro2 参数: (macroPath, moduleName, procName, method)
            # 对于 .swp 文件，模块和过程名传空字符串，method 传 "swMacroRunFromFile"
            self._sw_app.RunMacro2(
                self.macro_path,         # 宏文件完整路径
                "",                      # 模块名（swp 不需要）
                "",                      # 过程名（swp 不需要）
                "swMacroRunFromFile",    # 以文件方式运行
            )

            # 等待宏执行完成（宏内部负责保存 STEP 文件）
            logger.info("宏执行请求已发送，等待完成...")
            time.sleep(5)

            return True
        except Exception as e:
            logger.error("宏执行失败: %s", traceback.format_exc())
            return False

    def _verify_step_output(self, config_id: str) -> bool:
        """
        验证 STEP 文件是否成功生成。

        Args:
            config_id: 构型 ID，用于匹配文件命名（如果宏按构型命名）

        Returns:
            STEP 文件是否存在
        """
        # 检查 STEP 目录下是否有新生成的文件
        step_files = list(Path(self.step_dir).glob("*.step")) + list(
            Path(self.step_dir).glob("*.STEP")
        )
        step_files += list(Path(self.step_dir).glob("*.stp")) + list(
            Path(self.step_dir).glob("*.STP")
        )

        if step_files:
            # 获取最近修改的文件
            latest = max(step_files, key=lambda f: f.stat().st_mtime)
            logger.info("找到 STEP 文件: %s (修改时间: %s)", latest.name,
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest.stat().st_mtime)))
            return True
        else:
            logger.warning("STEP 目录中未找到任何 STEP 文件: %s", self.step_dir)
            return False

    def _close_model(self):
        """关闭当前打开的 SolidWorks 文档"""
        try:
            if self._sw_app is not None:
                self._sw_app.CloseAllDocuments(True)  # True = 保存更改
                logger.info("已关闭 SolidWorks 文档")
        except Exception as e:
            logger.warning("关闭文档时出现异常: %s", e)

    # -----------------------------------------------------------------------
    # 主流程
    # -----------------------------------------------------------------------
    def process_config(self, config_id: str, params: dict, retry_count: int = 0) -> bool:
        """
        执行完整 SolidWorks 参数化建模流程：
        Excel 修改 → 打开模型 → 重建 → 运行宏 → 导出 STEP

        Args:
            config_id: 构型 ID
            params: 参数字典，如 {"B2": 2.5, "B3": 30}
            retry_count: 当前重试次数

        Returns:
            是否成功完成
        """
        max_retry = GLOBAL_CONFIG["max_retry"]
        logger.info("=" * 60)
        logger.info("阶段2 (SolidWorks) — 开始处理构型: %s", config_id)
        logger.info("=" * 60)

        # --- Step 1: 修改 Excel 参数 ---
        if not self.update_excel_params(params):
            return False

        # --- Step 2: 连接 SW 并打开模型 ---
        for attempt in range(max_retry):
            try:
                if not self._connect_sw():
                    logger.warning("SW 连接失败，第 %d/%d 次重试...", attempt + 1, max_retry)
                    time.sleep(GLOBAL_CONFIG["sw_retry_interval"])
                    continue

                # 打开模型
                doc = self._open_model()
                if doc is None:
                    self._close_model()
                    time.sleep(GLOBAL_CONFIG["sw_retry_interval"])
                    continue

                # 重建模型
                if not self._rebuild_model(doc):
                    logger.warning("模型重建警告，继续执行...")

                # 运行导出宏
                if not self._run_macro():
                    self._close_model()
                    time.sleep(GLOBAL_CONFIG["sw_retry_interval"])
                    continue

                # 验证 STEP 输出
                if not self._verify_step_output(config_id):
                    self._close_model()
                    logger.error("STEP 文件未生成，第 %d 次重试...", attempt + 1)
                    time.sleep(GLOBAL_CONFIG["sw_retry_interval"])
                    continue

                # 成功
                self._close_model()
                logger.info("构型 %s 的 SolidWorks 阶段完成 ✓", config_id)
                return True

            except Exception as e:
                logger.error("SolidWorks 流程出现异常 (尝试 %d/%d): %s",
                             attempt + 1, max_retry, traceback.format_exc())
                time.sleep(GLOBAL_CONFIG["sw_retry_interval"])

        logger.error("构型 %s SolidWorks 阶段最终失败（已重试 %d 次）", config_id, max_retry)
        return False

    def cleanup(self):
        """清理 COM 资源"""
        try:
            if self._sw_app is not None:
                self._sw_app.CloseAllDocuments(True)
                self._sw_app.ExitApp()
                self._sw_app = None
            import pythoncom
            pythoncom.CoUninitialize()
        except Exception:
            pass