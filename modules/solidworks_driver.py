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
        self._sw_app = None          # SolidWorks 应用程序 COM 对象
        self._com_initialized = False # COM 库初始化标志
        self._macro_start_time = None # 宏执行开始时间戳（用于验证 STEP 输出）
        # 如果希望在多个构型间复用 SW 实例（性能优化），设置为 True
        self._keep_app_alive = GLOBAL_CONFIG.get("sw_keep_alive", False)

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
        wb = None
        try:
            logger.info("正在修改 Excel 参数文件: %s", self.excel_path)
            wb = openpyxl.load_workbook(self.excel_path)
            ws = wb[sheet_name]

            for cell_ref, value in params.items():
                ws[cell_ref] = value
                logger.debug("  设置 %s = %s", cell_ref, value)

            wb.save(self.excel_path)
            logger.info("Excel 参数更新完成")
            return True
        except Exception as e:
            logger.error("Excel 参数修改失败: %s", traceback.format_exc())
            return False
        finally:
            if wb is not None:
                try:
                    wb.close()
                except Exception:
                    pass

    # -----------------------------------------------------------------------
    # SolidWorks COM 操作
    # -----------------------------------------------------------------------
    def _connect_sw(self) -> bool:
        """
        连接到 SolidWorks 应用程序（唤醒现有实例或启动新实例）。

        Returns:
            是否连接成功
        """
        import win32com.client
        import pythoncom

        try:
            # 初始化 COM 库（仅一次）
            if not self._com_initialized:
                pythoncom.CoInitialize()
                self._com_initialized = True  # 紧跟在成功调用之后设置（修复竞态）

            # 尝试获取已运行的 SolidWorks 实例
            try:
                self._sw_app = win32com.client.GetActiveObject("SldWorks.Application")
                logger.info("连接到已运行的 SolidWorks 实例")
            except Exception:
                # 没有运行中的实例，启动新的（包装超时保护）
                logger.info("正在启动新的 SolidWorks 实例...")
                ok_dispatch, sw_app = self._com_call_with_timeout(
                    lambda: win32com.client.Dispatch("SldWorks.Application"),
                    timeout=120,  # 冷启动可能较慢，给 2 分钟
                    description="SW_Dispatch",
                )
                if not ok_dispatch or sw_app is None:
                    logger.error("SolidWorks Dispatch 失败或超时")
                    return False
                self._sw_app = sw_app
                self._sw_app.Visible = True  # 显示 SW 窗口便于调试

            # 轮询等待 SW 完全就绪（替换硬编码 sleep(2)，更稳健）
            logger.info("等待 SolidWorks 完全就绪...")
            self._sw_app.Visible = self._sw_app.Visible  # 触发一次属性访问探测
            deadline = time.time() + 60  # 最长等待 60 秒
            while time.time() < deadline:
                try:
                    # 访问一个轻量级属性来验证 SW 是否响应
                    _ = self._sw_app.RevisionNumber()
                    break  # 成功后退出轮询
                except Exception:
                    time.sleep(1)
            else:
                logger.warning("SolidWorks 就绪检测超时，继续尝试操作（可能不稳定）")

            logger.info("SolidWorks 连接成功")
            return True

        except Exception as e:
            logger.error("SolidWorks COM 连接失败: %s", e)
            return False

    def _com_call_with_timeout(self, func, timeout: int, description: str = "COM操作"):
        """
        在独立线程中执行 COM 调用，带超时保护。

        COM 接口调用可能会因 SolidWorks 内部状态而无限阻塞（如弹窗、
        资源争用等）。通过线程 + join(timeout) 机制，可以避免主流程
        被永久卡死。

        **重要**：子线程中必须调用 CoInitialize/CoUninitialize，
        否则 COM 公寓模型会导致跨线程调用失败或死锁。

        Args:
            func:    无参数的可调用对象，通常为 lambda 封装的 COM 方法
            timeout: 超时秒数
            description: 操作描述（用于日志）

        Returns:
            (ok: bool, result: Any) — ok=False 表示超时或异常
        """
        import threading
        import pythoncom

        result_container = {"ok": False, "result": None, "error": None}

        def _target():
            # ---- COM 线程初始化 ----
            pythoncom.CoInitialize()
            try:
                result_container["result"] = func()
                result_container["ok"] = True
            except Exception as exc:
                result_container["error"] = exc
            finally:
                # ---- COM 线程清理 ----
                pythoncom.CoUninitialize()

        t = threading.Thread(target=_target, daemon=True)
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            # daemon 线程在超时后无法强制终止；注册到跟踪列表以供外部检查
            self._track_abandoned_thread(t, description)
            suppress = GLOBAL_CONFIG.get("sw_suppress_timeout_log", False)
            if not suppress:
                logger.warning(
                    "⚠ %s 超时（%d 秒），子线程 %d 可能仍在运行 COM 操作，已记录跟踪",
                    description, timeout, t.ident or 0,
                )
            return False, None

        if not result_container["ok"]:
            logger.error("%s 抛出异常: %s", description, result_container["error"])
            return False, None

        return True, result_container["result"]

    def _track_abandoned_thread(self, thread, description: str):
        """记录超时后仍运行的 COM 线程，供 cleanup() 统一警告。"""
        if not hasattr(self, "_abandoned_threads"):
            self._abandoned_threads = []
        self._abandoned_threads.append((thread, description, time.time()))

    def _warn_abandoned_threads(self):
        """在 cleanup() 时警告仍在运行的 COM 线程。"""
        if not hasattr(self, "_abandoned_threads") or not self._abandoned_threads:
            return
        still_alive = [
            (desc, time.time() - start)
            for t, desc, start in self._abandoned_threads
            if t.is_alive()
        ]
        if still_alive:
            lines = "\n".join(
                f"  - {desc} (已运行 {elapsed:.0f}s)" for desc, elapsed in still_alive
            )
            logger.warning(
                "以下 COM 操作线程仍处于活跃状态（可能残留阻塞），请检查 SW 进程：\n%s",
                lines,
            )
        self._abandoned_threads.clear()

    def _open_model(self) -> Optional[object]:
        """
        打开 SolidWorks 模型文件（带超时保护）。
        模型打开后会自动从外部 Excel 文件读取参数并更新。

        Returns:
            打开的文档对象，失败返回 None
        """
        from config import GLOBAL_CONFIG

        timeout = GLOBAL_CONFIG.get("sw_open_timeout", 120)
        logger.info("正在打开模型: %s (超时=%ds)", self.model_path, timeout)

        if not os.path.isfile(self.model_path):
            logger.error("模型文件不存在: %s", self.model_path)
            return None

        ok, doc = self._com_call_with_timeout(
            lambda: self._sw_app.OpenDoc6(
                self.model_path,
                SW_DOC_TYPE_PART,
                0, "", 0, 0,
            ),
            timeout=timeout,
            description=f"OpenDoc6({os.path.basename(self.model_path)})",
        )

        if not ok or doc is None:
            logger.error("无法打开模型文件（超时或返回 None）")
            return None

        logger.info("模型打开成功，等待参数更新...")
        time.sleep(1)
        return doc

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
        运行预置的 VBA 宏，用于导出 STEP 文件（带超时保护）。
        宏路径为 config 中配置的 Macro1.swp。

        Returns:
            是否执行成功
        """
        from config import GLOBAL_CONFIG

        timeout = GLOBAL_CONFIG.get("sw_macro_timeout", 300)

        if not os.path.isfile(self.macro_path):
            logger.error("宏文件不存在: %s", self.macro_path)
            return False

        logger.info("正在运行导出宏: %s (超时=%ds)", self.macro_path, timeout)
        self._macro_start_time = time.time()

        # 在独立线程中执行 RunMacro2，防止 SW 内部弹窗导致永久阻塞
        ok, _ = self._com_call_with_timeout(
            lambda: self._sw_app.RunMacro2(
                self.macro_path,
                "",                      # 模块名（swp 不需要）
                "",                      # 过程名（swp 不需要）
                "swMacroRunFromFile",
            ),
            timeout=timeout,
            description=f"RunMacro2({os.path.basename(self.macro_path)})",
        )

        if not ok:
            logger.error("宏执行失败或超时")
            return False

        # 宏线程返回后，追加一个短轮询确认 SW 真正空闲
        logger.info("宏执行请求已返回，等待 SW 完成导出...")
        max_wait = 60
        elapsed = 0
        while elapsed < max_wait:
            time.sleep(0.5)
            elapsed += 0.5
            try:
                self._sw_app.GetDocumentCount()
                if elapsed >= 3:
                    break
            except Exception:
                pass
        logger.info("宏执行完成 (等待 %.1f 秒)", elapsed)

        return True

    def _verify_step_output(self, config_id: str) -> bool:
        """
        验证 STEP 文件是否成功生成。

        策略：
          1. 优先按 config_id 精确匹配（如 R2.5_L30_A15.step）
          2. 若未找到精确匹配，则检查在本次宏执行之后新生成的 STEP 文件

        Args:
            config_id: 构型 ID，用于匹配文件命名

        Returns:
            STEP 文件是否存在
        """
        step_dir = Path(self.step_dir)
        patterns = ["*.step", "*.STEP", "*.stp", "*.STP"]

        # 策略1：精确匹配 config_id
        for pattern in patterns:
            for f in step_dir.glob(pattern):
                if f.stem == config_id or config_id in f.stem:
                    logger.info("找到匹配的 STEP 文件: %s (大小: %d bytes)", f.name, f.stat().st_size)
                    return True

        # 策略2：查找在宏执行之后新生成的文件
        if self._macro_start_time:
            all_files = []
            for pat in patterns:
                all_files.extend(step_dir.glob(pat))
            new_files = [f for f in all_files if f.stat().st_mtime >= self._macro_start_time]
            if new_files:
                latest = max(new_files, key=lambda f: f.stat().st_mtime)
                logger.info("找到新生成的 STEP 文件: %s (修改时间: %s)",
                            latest.name,
                            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest.stat().st_mtime)))
                return True

        logger.warning("STEP 目录中未找到构型 %s 的 STEP 文件: %s", config_id, self.step_dir)
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
    def process_config(self, config_id: str, params: dict) -> bool:
        """
        执行完整 SolidWorks 参数化建模流程：
        Excel 修改 → 打开模型 → 重建 → 运行宏 → 导出 STEP

        Args:
            config_id: 构型 ID
            params: 参数字典，如 {"B2": 2.5, "B3": 30}

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
        """清理 COM 资源。若 _keep_app_alive=True 则仅关闭文档，不退出 SW。"""
        try:
            if self._sw_app is not None:
                try:
                    self._sw_app.CloseAllDocuments(True)
                except Exception:
                    pass
                if not self._keep_app_alive:
                    try:
                        self._sw_app.ExitApp()
                    except Exception:
                        pass
                self._sw_app = None
        except Exception:
            pass
        finally:
            if self._com_initialized:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
                self._com_initialized = False
