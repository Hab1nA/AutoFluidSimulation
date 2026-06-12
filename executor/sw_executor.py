"""
===============================================================================
SolidWorks COM 自动化执行器 (SW Executor)

负责通过 win32com 驱动 SolidWorks 完成：
- SW 进程连接/启动（三层降级策略）
- Excel 设计表导入（多策略容错）
- 所有构型 STEP 文件导出
- COM 资源清理

从 engine/task_runner.py 中提取，职责独立后可单独测试和维护。
===============================================================================
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import gc
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from engine.scheduler.control import PipelineControl
    from engine.state_manager import StateManager

from engine.config import (
    LOCAL_PATHS, ENGINE_CONFIG,
    OPERATION_TIMEOUTS,
    STATUS_COMPLETED, STATUS_ERROR, get_step_filename,
)
from utils.logger import setup_logger

logger = setup_logger(__name__)


class SWExecutor:
    """SolidWorks COM 自动化执行器。

    封装 SolidWorks 的连接、设计表导入、STEP 导出等所有 COM 操作。
    """

    # swDocumentTypes_e
    _SW_DOC_PART = 1
    _SW_DOC_ASSEMBLY = 2
    # swOpenDocOptions_e (用于 OpenDoc6)
    _SW_OPEN_SILENT = 1       # 静默：抑制警告对话框
    _SW_OPEN_READONLY = 2     # 只读
    # swRunMacroOption_e (用于 RunMacro2)
    _SW_RUN_MACRO_DEFAULT = 0
    _SW_RUN_MACRO_UNLOAD_AFTER = 1
    # swSaveAsVersion_e / swSaveAsOptions_e (用于直接 COM 导出 STEP，替代宏文件)
    _SW_SAVE_AS_CURRENT_VERSION = 0   # swSaveAsCurrentVersion
    _SW_SAVE_AS_OPTIONS_SILENT = 1    # swSaveAsOptions_Silent

    def __init__(self, state_manager: StateManager):
        """初始化 SW 执行器。

        Args:
            state_manager: StateManager 实例
        """
        self.state = state_manager
        self._paused_event: threading.Event | None = None
        self._stopped_event: threading.Event | None = None
        self._pipeline_control: PipelineControl | None = None
        self._cached_sw_app: Any = None
        self._cached_doc: Any = None
        self._com_initialized: bool = False
        self._cleanup_lock = threading.RLock()
        self._first_cleanup_done = False
        self._final_cleanup_done = False

    def set_control_events(
        self,
        paused_event: threading.Event,
        stopped_event: threading.Event,
    ) -> None:
        """注入调度器的暂停/停止事件，使逐构型循环可响应 pause/stop。

        Args:
            paused_event: 调度器的 _paused 事件
            stopped_event: 调度器的 _stopped 事件
        """
        self._paused_event = paused_event
        self._stopped_event = stopped_event

    def set_pipeline_control(self, pipeline_control: PipelineControl) -> None:
        """注入统一控制层，用于锁定单构型 COM 操作窗口。"""
        self._pipeline_control = pipeline_control

    @contextmanager
    def _external_start(self) -> Iterator[bool]:
        """锁定一个 SW 外部操作窗口。"""
        if self._pipeline_control is not None:
            with self._pipeline_control.external_start() as allowed:
                yield allowed
            return
        paused = self._paused_event is not None and self._paused_event.is_set()
        stopped = self._stopped_event is not None and self._stopped_event.is_set()
        yield not paused and not stopped

    # ------------------------------------------------------------------
    # 单构型导出（供 RetryManager 逐构型调用）
    # ------------------------------------------------------------------

    def export_sw_per_config(self, config_name: int) -> bool:
        """在统一 gate 下执行单构型 SW 导出。"""
        with self._external_start() as allowed:
            if not allowed:
                logger.info(f"[SW] 构型{config_name}: 暂停或停止状态下跳过导出")
                return False
            return self._export_sw_per_config_admitted(config_name)

    def _export_sw_per_config_admitted(self, config_name: int) -> bool:
        """导出单个构型的 STEP 文件（供 RetryManager 调用）。

        执行流程：
        1. 首次调用时建立 SW 连接、打开模型、导入设计表（缓存在实例属性）
        2. 切换到目标构型 → 重建 → SaveAs STEP
        3. 所有构型完成后由调用方调用 _disconnect_sw_cached() 清理

        由 RetryManager 逐构型调用，
          状态管理（Running/Retrying/Error）完全由 RetryManager 负责

        Args:
            config_name: 构型编号

        Returns:
            True 表示该构型 STEP 文件导出成功
        """
        # ★ CoInitialize 仅在首次调用时执行（同一线程内幂等）
        if not self._com_initialized:
            try:
                import pythoncom
                pythoncom.CoInitialize()
                self._com_initialized = True
            except ImportError:
                logger.error("[SW] pywin32 未安装，无法初始化 COM")
                return False
            except Exception:
                pass  # 已初始化

        step_dir = LOCAL_PATHS.get("step_dir", "")
        sw_model = LOCAL_PATHS["sw_model"]
        excel_path = LOCAL_PATHS.get("excel", "")
        doc_type = self._guess_sw_doc_type(sw_model)

        # ---- 首次调用：建立 SW 连接 ----
        if self._cached_sw_app is None:
            sw_app = None
            doc = None
            cache_ready = False
            try:
                sw_app = self._connect_sw()
                if sw_app is None:
                    logger.error(f"[SW] 构型{config_name}: 无法连接 SolidWorks")
                    return False
                try:
                    sw_app.Visible = bool(ENGINE_CONFIG.get("sw_visible", True))
                except Exception:
                    pass
                doc = self._open_sw_model(sw_app, sw_model, doc_type)
                if doc is None:
                    logger.error(f"[SW] 构型{config_name}: 无法打开模型")
                    return False
                if not self._import_design_table_with_retry(
                    doc, sw_app, excel_path, sw_model
                ):
                    logger.error(f"[SW] 构型{config_name}: 设计表导入失败")
                    return False
                self._cached_sw_app = sw_app
                self._cached_doc = doc
                cache_ready = True
                logger.info("[SW] COM 连接已建立（缓存供后续构型复用）")
            except Exception as e:
                logger.error(
                    f"[SW] 构型{config_name}: SW 连接失败 "
                    f"({type(e).__name__}: {e})", exc_info=True
                )
                return False
            finally:
                if not cache_ready:
                    if sw_app is not None or doc is not None:
                        try:
                            self._disconnect_sw(sw_app, doc, sw_model)
                        except Exception as e:
                            logger.debug(f"[SW-Cleanup] 初始化失败后清理异常: {e}")
                        finally:
                            self._com_initialized = False
                    else:
                        self._uninitialize_com_if_needed()

        sw_app = self._cached_sw_app
        doc = self._cached_doc

        # ---- 校验 STEP 目录 ----
        if not step_dir:
            logger.error("[SW] 未配置 STEP 输出目录 (step_dir)")
            return False
        try:
            os.makedirs(step_dir, exist_ok=True)
        except OSError as e:
            logger.error(f"[SW] 无法创建 STEP 输出目录: {step_dir}: {e}")
            return False

        # ---- 检查文件是否已存在（断点续传 / 之前批次已成功） ----
        filename = get_step_filename("sw", config_name)
        if not filename:
            logger.error(f"[SW] 构型{config_name}: 无法生成 STEP 文件名")
            return False
        filepath = os.path.join(step_dir, filename)
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            logger.info(
                f"[SW] 构型{config_name}: STEP 文件已存在，跳过导出"
            )
            return True

        # ---- 暂停/停止检查 ----
        if self._paused_event is not None and self._paused_event.is_set():
            logger.info(f"[SW] 构型{config_name}: 暂停标志已置位，中止导出")
            return False
        if self._stopped_event is not None and self._stopped_event.is_set():
            logger.info(f"[SW] 构型{config_name}: 停止标志已置位，中止导出")
            return False

        # ---- 切换构型 → 重建 → 导出 ----
        import win32com.client

        logger.info(f"[SW] 构型{config_name}: 开始导出 STEP...")
        cn_str = str(config_name)

        try:
            doc.ShowConfiguration2(cn_str)
        except Exception as e:
            logger.error(
                f"[SW] 构型{config_name}: ShowConfiguration2 失败 "
                f"({type(e).__name__}: {e})"
            )
            return False

        # 重建
        ext = doc.Extension
        rebuild_ok = False
        try:
            ext.Rebuild(0)
            rebuild_ok = True
        except TypeError:
            try:
                rebuild_fn = ext.Rebuild
                if callable(rebuild_fn):
                    rebuild_fn(0)
                    rebuild_ok = True
            except Exception:
                pass
        except Exception:
            pass
        if not rebuild_ok:
            try:
                doc.Rebuild(0)
                rebuild_ok = True
            except Exception:
                pass

        # SaveAs STEP
        import pythoncom
        try:
            save_errors = win32com.client.VARIANT(
                pythoncom.VT_BYREF | pythoncom.VT_I4, 0
            )
            save_warnings = win32com.client.VARIANT(
                pythoncom.VT_BYREF | pythoncom.VT_I4, 0
            )
            export_data = win32com.client.VARIANT(
                pythoncom.VT_DISPATCH, None
            )
            status = doc.Extension.SaveAs(
                filepath,
                self._SW_SAVE_AS_CURRENT_VERSION,
                self._SW_SAVE_AS_OPTIONS_SILENT,
                export_data,
                save_errors,
                save_warnings,
            )
            if status and os.path.exists(filepath):
                logger.info(
                    f"[SW] 构型{config_name}: {filename} 导出成功 "
                    f"(Errors={save_errors.value}, Warnings={save_warnings.value})"
                )
                return True
            else:
                logger.warning(
                    f"[SW] 构型{config_name}: SaveAs 返回 {status}，"
                    f"文件存在={os.path.exists(filepath)} "
                    f"(Errors={save_errors.value})"
                )
                return False
        except Exception as e:
            logger.error(
                f"[SW] 构型{config_name}: SaveAs 异常 "
                f"({type(e).__name__}: {e})"
            )
            # ★ 清理缓存的 COM 连接，下次重试时重新建立
            self.disconnect_sw_cached()
            return False

    def disconnect_sw_cached(self) -> None:
        """清理通过 export_sw_per_config 缓存的 SW 连接。"""
        sw_app = self._cached_sw_app
        doc = self._cached_doc
        self._cached_sw_app = None
        self._cached_doc = None

        if sw_app is None and doc is None:
            self._uninitialize_com_if_needed()
            return

        sw_model = LOCAL_PATHS.get("sw_model", "")
        try:
            self._disconnect_sw(sw_app, doc, sw_model)
        except Exception as e:
            logger.debug(f"[SW-Cleanup] 清理缓存连接异常: {e}")
        finally:
            self._com_initialized = False

    # ------------------------------------------------------------------
    # 全量清理（首次/末次）
    # ------------------------------------------------------------------

    def shutdown_all(self) -> None:
        """全量清理 SolidWorks 进程并重置清理标志。"""
        with self._cleanup_lock:
            self._shutdown_all_internal()
            self._first_cleanup_done = False
            self._final_cleanup_done = False

    def _shutdown_all_internal(self) -> None:
        """全量清理（内部版本，调用方须已持有 _cleanup_lock）。"""
        logger.info("[SW-Cleanup] 执行全量 SolidWorks 进程清理...")
        self.disconnect_sw_cached()
        self._terminate_sw_processes()
        logger.info("[SW-Cleanup] 全量清理完成")

    def do_first_cleanup(self) -> None:
        """首次 SW 全体清理（进入 SW 阶段前调用）。"""
        with self._cleanup_lock:
            if not self._first_cleanup_done:
                logger.info("[SW-Cleanup] === 首次全体 SW 进程清理（进入 SW 阶段前）===")
                self._shutdown_all_internal()
                self._first_cleanup_done = True

    def do_final_cleanup(self) -> None:
        """末次 SW 全体清理（SW 阶段全部完成后调用）。"""
        with self._cleanup_lock:
            if not self._final_cleanup_done:
                logger.info("[SW-Cleanup] === 末次全体 SW 进程清理（SW 阶段全部完成后）===")
                self._shutdown_all_internal()
                self._final_cleanup_done = True

    def reset_cleanup_state(self) -> None:
        """重置 SW 全量清理状态。"""
        with self._cleanup_lock:
            self._first_cleanup_done = False
            self._final_cleanup_done = False
            self.disconnect_sw_cached()
            logger.info("[SW-Cleanup] 已重置全量清理状态")

    # ------------------------------------------------------------------
    # SW 连接管理
    # ------------------------------------------------------------------

    def _connect_sw(self) -> Any:  # noqa: ANN401  COM 动态对象
        """三层降级连接/启动 SolidWorks 并返回 ISldWorks COM 对象。"""
        import win32com.client

        # ---- 第1层: 连接已运行的 SW ----
        logger.info("[SW-COM] 正在连接 SolidWorks (第1层: GetActiveObject)...")
        try:
            sw_app = win32com.client.GetActiveObject("SldWorks.Application")
            logger.info("[SW-COM] 已连接到运行中的 SolidWorks 实例")
            return sw_app
        except Exception as e1:
            logger.info(
                f"[SW-COM] GetActiveObject 失败 ({type(e1).__name__}: {e1})，"
                f"尝试启动新实例..."
            )

        # ---- 第2层: 通过 COM Dispatch 启动 ----
        logger.info("[SW-COM] 正在启动 SolidWorks (第2层: COM Dispatch)...")
        try:
            sw_app = win32com.client.Dispatch("SldWorks.Application")
            try:
                sw_app.UserControl = True
            except Exception:
                pass
            try:
                sw_app.Visible = bool(ENGINE_CONFIG.get("sw_visible", True))
            except Exception:
                pass
            logger.info("[SW-COM] SolidWorks 已通过 COM Dispatch 启动")
            time.sleep(OPERATION_TIMEOUTS["sw_dispatch_startup_delay"])
            return sw_app
        except Exception as e2:
            logger.warning(
                f"[SW-COM] COM Dispatch 失败 ({type(e2).__name__}: {e2})，"
                f"尝试备选方案..."
            )

        # ---- 第3层: 直接启动 exe ----
        logger.warning(
            "[SW-COM] 前两层连接均失败，将强制终止所有残留 SW 进程后重新启动。"
            "若您有其他 SolidWorks 窗口打开且包含未保存数据，请立即保存！"
        )
        logger.info("[SW-COM] 正在启动 SolidWorks (第3层: subprocess)...")
        self._terminate_sw_processes()
        if not self._launch_sw_process():
            logger.error("[SW-COM] 所有启动方式均失败，无法连接 SolidWorks")
            return None
        try:
            sw_app = win32com.client.GetActiveObject("SldWorks.Application")
        except Exception as e3:
            logger.error(f"[SW-COM] 第3层 GetActiveObject 失败: {e3}")
            return None
        try:
            sw_app.Visible = bool(ENGINE_CONFIG.get("sw_visible", True))
        except Exception:
            pass
        logger.info("[SW-COM] 已通过 subprocess 启动并连接 SolidWorks")
        return sw_app

    def _uninitialize_com_if_needed(self) -> None:
        """释放当前线程的 COM 初始化状态。"""
        if not self._com_initialized:
            return
        try:
            import pythoncom
            try:
                pythoncom.CoFreeUnusedLibraries()
            except Exception:
                pass
            pythoncom.CoUninitialize()
        except Exception as e:
            logger.debug(f"[SW-Cleanup] COM 反初始化异常: {e}")
        finally:
            self._com_initialized = False

    def _launch_sw_process(self) -> bool:
        """通过 subprocess 直接启动 SolidWorks.exe，轮询等待 COM 就绪。"""
        sw_exe = LOCAL_PATHS.get("sw_exe", "")
        if not sw_exe or not os.path.exists(sw_exe):
            logger.warning("[SW-COM] 未配置 SolidWorks 可执行文件路径 (sw_exe)，无法使用 subprocess 启动")
            return False

        logger.info(f"[SW-COM] 正在通过 subprocess 启动 SolidWorks: {sw_exe}")
        try:
            creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == "nt" else 0
            subprocess.Popen(
                [sw_exe],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
            logger.info("[SW-COM] SolidWorks 进程已启动，等待 COM 接口就绪...")

            import win32com.client
            max_wait = 60
            for attempt in range(max_wait):
                time.sleep(1)
                try:
                    sw_app = win32com.client.GetActiveObject("SldWorks.Application")
                    if sw_app is not None:
                        logger.info(f"[SW-COM] SolidWorks COM 接口已就绪 (等待了 {attempt + 1} 秒)")
                        return True
                except (AttributeError, TypeError):
                    pass
                except Exception as e:
                    logger.debug(f"[SW-COM] 获取 SolidWorks COM 对象异常: {e}")
            logger.error(f"[SW-COM] 等待 SolidWorks 启动超时 ({max_wait} 秒)")
            return False
        except FileNotFoundError:
            logger.error(f"[SW-COM] 找不到 SolidWorks 可执行文件: {sw_exe}")
            return False
        except OSError as e:
            logger.error(f"[SW-COM] 启动 SolidWorks 进程失败: {e}")
            return False

    def _terminate_sw_processes(self):
        """清理可能残留的 SolidWorks 进程。"""
        if os.name != "nt":
            return
        try:
            if self._is_sw_process_running(timeout=10):
                logger.info("[SW-Cleanup] 检测到残留 SolidWorks 进程，正在终止...")
                kill_result = subprocess.run(
                    ["taskkill", "/f", "/im", "SLDWORKS.exe"],
                    capture_output=True, text=True, timeout=30,
                )
                if kill_result.returncode == 0:
                    logger.info("[SW-Cleanup] ✓ 残留 SolidWorks 进程已终止，等待 3 秒...")
                    time.sleep(3)
                else:
                    logger.warning(
                        f"[SW-Cleanup] taskkill 返回非零码 {kill_result.returncode}: "
                        f"{kill_result.stderr.strip()}"
                    )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"[SW-Cleanup] 检查/终止 SW 进程时异常: {e}")

    @staticmethod
    def _is_sw_process_running(timeout: int = 5) -> bool:
        """检查当前 Windows 会话中是否存在 SolidWorks 进程。"""
        if os.name != "nt":
            return False
        try:
            result = subprocess.run(
                ["tasklist", "/fi", "IMAGENAME eq SLDWORKS.exe", "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug(f"[SW-Cleanup] 检查 SolidWorks 进程时异常: {e}")
            return False
        return "SLDWORKS.exe" in result.stdout

    def _open_sw_model(self, sw_app: Any, sw_model: str, doc_type: int) -> Any:  # noqa: ANN401  COM 动态对象
        """通过 OpenDoc6 打开 SW 模型文件并验证 COM 代理有效性。"""
        import win32com.client
        import pythoncom

        logger.info(f"[SW-COM] 正在打开模型 (OpenDoc6): {os.path.basename(sw_model)}")
        open_errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        open_warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

        try:
            doc = sw_app.OpenDoc6(
                sw_model,
                doc_type,
                self._SW_OPEN_SILENT,
                "",
                open_errors,
                open_warnings,
            )
            logger.info(
                f"[SW-COM] OpenDoc6: Errors={open_errors.value}, "
                f"Warnings={open_warnings.value}"
            )
            if open_errors.value != 0:
                logger.warning(
                    f"[SW-COM] OpenDoc6 返回错误码 {open_errors.value}，"
                    f"模型可能存在问题（缺失参考/重建错误）"
                )
        except Exception as open_err:
            logger.error(
                f"[SW-COM] OpenDoc6 异常 ({type(open_err).__name__}: {open_err})"
            )
            return None

        if doc is None:
            logger.error(
                f"[SW-COM] 无法打开 SW 模型: {sw_model}"
                f"（文件可能损坏、版本不兼容，或路径含特殊字符）"
            )
            return None

        if not self._verify_com_object(doc, "IModelDoc2"):
            logger.error(
                "[SW-COM] OpenDoc6 返回了无效的文档 COM 代理，"
                "模型可能未正确加载"
            )
            try:
                sw_app.CloseDoc(os.path.basename(sw_model))
            except Exception:
                pass
            return None

        logger.info(f"[SW-COM] 模型已打开: {os.path.basename(sw_model)}")
        return doc

    def _disconnect_sw(self, sw_app, doc, sw_model: str):
        """清理 SW COM 资源：关闭文档 → 释放 COM。

        SolidWorks 进程退出统一由全量清理流程处理。
        """
        import pythoncom

        # 步骤 1: 关闭模型文档
        if doc is not None and ENGINE_CONFIG.get("sw_close_doc_on_finish", True):
            try:
                title = doc.GetTitle()
            except Exception:
                title = os.path.basename(sw_model)
            try:
                sw_app.CloseDoc(title)
                logger.info(f"[SW-Cleanup] 已关闭模型文档: {title}")
            except Exception as e_doc:
                logger.debug(f"[SW-Cleanup] 关闭模型文档异常: {e_doc}")

        # 步骤 2: 释放 COM 资源
        del doc
        del sw_app
        gc.collect()
        for _ in range(2):
            try:
                pythoncom.CoFreeUnusedLibraries()
            except Exception:
                pass
            time.sleep(0.5)
        pythoncom.CoUninitialize()

    # ------------------------------------------------------------------
    # 设计表导入
    # ------------------------------------------------------------------

    def _validate_design_table(self, excel_path: str) -> list[str]:
        """预验证 Excel 设计表格式，返回诊断警告列表。"""
        warnings = []
        logger.info(f"[SW-DesignTable] 预验证 Excel 设计表格式: {os.path.basename(excel_path)}")

        if not os.path.exists(excel_path):
            warnings.append(f"Excel 文件不存在: {excel_path}")
            logger.warning(f"[SW-DesignTable] {warnings[-1]}")
            return warnings

        file_size = os.path.getsize(excel_path)
        logger.info(f"[SW-DesignTable] 文件大小: {file_size} bytes")

        try:
            import openpyxl
            wb = None
            try:
                wb = openpyxl.load_workbook(excel_path, data_only=True, read_only=True)
                ws = wb.active

                try:
                    row1_cells = list(ws.iter_rows(min_row=1, max_row=1))
                    if not row1_cells:
                        warnings.append("Excel 第1行缺失（空表格）")
                        logger.warning(f"[SW-DesignTable] {warnings[-1]}")
                        return warnings
                    row1 = [cell.value for cell in row1_cells[0]]
                except (IndexError, StopIteration):
                    warnings.append("Excel 第1行缺失（空表格）")
                    logger.warning(f"[SW-DesignTable] {warnings[-1]}")
                    return warnings
                row1_text = " ".join(str(v) for v in row1 if v is not None)
                logger.info(f"[SW-DesignTable] 第1行内容: {row1_text[:120]}")

                has_design_table_header = "Design Table" in row1_text or "设计表" in row1_text
                if not has_design_table_header:
                    warnings.append(
                        f"Excel 第1行缺少 SW 设计表头（应包含 'Design Table'），"
                        f"实际内容: {row1_text[:80]}"
                    )
                    logger.warning(f"[SW-DesignTable] {warnings[-1]}")

                try:
                    row2_cells = list(ws.iter_rows(min_row=2, max_row=2))
                    if row2_cells:
                        row2 = [cell.value for cell in row2_cells[0]]
                        row2_text = " | ".join(str(v) for v in row2 if v is not None)
                        logger.info(f"[SW-DesignTable] 第2行内容: {row2_text[:200]}")
                        has_param_headers = any(
                            isinstance(v, str) and ("$" in v or "@" in v)
                            for v in row2 if v is not None
                        )
                        if not has_param_headers:
                            warnings.append(
                                f"Excel 第2行似乎不含 SW 参数列头（期望格式如 '$PRP@Dimension1'），"
                                f"实际内容: {row2_text[:100]}"
                            )
                            logger.warning(f"[SW-DesignTable] {warnings[-1]}")
                except (IndexError, StopIteration):
                    warnings.append("Excel 第2行缺失（应为参数列头行）")
                    logger.warning(f"[SW-DesignTable] {warnings[-1]}")

                data_rows = 0
                for row in ws.iter_rows(min_row=3, values_only=True):
                    if row[0] is not None:
                        data_rows += 1
                logger.info(f"[SW-DesignTable] 数据行数 (第3行起): {data_rows}")

            finally:
                if wb is not None:
                    wb.close()

        except ImportError:
            logger.warning("[SW-DesignTable] openpyxl 未安装，跳过 Excel 格式预验证")
        except Exception as e:
            warnings.append(f"Excel 格式预验证异常: {type(e).__name__}: {e}")
            logger.warning(f"[SW-DesignTable] {warnings[-1]}", exc_info=True)

        if not warnings:
            logger.info("[SW-DesignTable] Excel 设计表格式预验证通过")
        return warnings

    def _model_has_design_table(self, doc: Any) -> bool:  # noqa: ANN401  COM 动态对象
        """检测模型是否已存在设计表（链接或内嵌），避免进入 Excel OLE 编辑态。"""
        try:
            extension = getattr(doc, "Extension", None)
            checker = getattr(extension, "HasDesignTable", None)
            if callable(checker) and bool(checker()):
                logger.info("[SW-DesignTable] 检测到模型已有设计表（HasDesignTable=True）")
                return True
        except Exception as e:
            logger.debug(f"[SW-DesignTable] HasDesignTable 检测异常: {e}")

        try:
            getter = getattr(doc, "GetDesignTable", None)
            dt = getter() if callable(getter) else getter
            if dt is not None:
                logger.info("[SW-DesignTable] 检测到模型已有设计表（GetDesignTable 返回非空）")
                return True
        except Exception as e:
            logger.debug(f"[SW-DesignTable] GetDesignTable 检测异常: {e}")

        logger.info("[SW-DesignTable] 模型无设计表，将进行导入")
        return False

    def _import_design_table_with_retry(self, doc: Any, sw_app: Any, excel_path: str, sw_model: str) -> bool:  # noqa: ANN401  COM 动态对象
        """带容错与多策略降级的设计表导入。"""
        basename_model = os.path.basename(sw_model)

        if self._model_has_design_table(doc):
            logger.info("[SW-DesignTable] 模型已有设计表，跳过导入（已自动同步参数）")
            return True

        # ---- 策略A: InsertFamilyTableOpen（最多2次） ----
        tmp_excel_path = None
        try:
            tmp_fd, tmp_excel_path = tempfile.mkstemp(
                suffix=".xlsx", prefix="sw_design_table_"
            )
            os.close(tmp_fd)
            shutil.copy2(excel_path, tmp_excel_path)
            logger.info(
                f"[SW-DesignTable] 已复制 Excel 到临时文件: "
                f"{os.path.basename(tmp_excel_path)}"
            )
        except OSError as e_copy:
            logger.warning(f"[SW-DesignTable] 无法复制 Excel: {e_copy}，使用原始路径")
            tmp_excel_path = excel_path

        insert_ok = False
        for attempt in (1, 2):
            if insert_ok:
                break
            import_path = tmp_excel_path or excel_path
            logger.info(f"[SW-DesignTable] InsertFamilyTableOpen 尝试 {attempt}/2")
            try:
                inserted = doc.InsertFamilyTableOpen(import_path)
                logger.info(f"[SW-DesignTable] InsertFamilyTableOpen 返回: {inserted}")
                if inserted:
                    insert_ok = True
                elif attempt == 1:
                    logger.info("[SW-DesignTable] 等待 3 秒后重试...")
                    time.sleep(3)
            except Exception as e_insert:
                logger.warning(
                    f"[SW-DesignTable] InsertFamilyTableOpen 异常 "
                    f"({type(e_insert).__name__}: {e_insert})"
                )
                if attempt == 1:
                    time.sleep(3)

        if insert_ok:
            logger.info("[SW-DesignTable] ✓ InsertFamilyTableOpen 成功")
            self._post_process_design_table(doc, excel_path)
            self._cleanup_tmp_excel(tmp_excel_path, excel_path)
            return True

        # ---- 策略B: 解析 Excel，COM 直接设参 ----
        logger.info("[SW-DesignTable] InsertFamilyTableOpen 失败，尝试 COM 直接设参...")
        com_ok = self._apply_params_via_com(doc, excel_path)

        self._cleanup_tmp_excel(tmp_excel_path, excel_path)

        if com_ok:
            return True

        # ---- 全部失败 → 详细诊断 ----
        self._diagnose_param_mismatch(doc, excel_path)
        logger.error("=" * 60)
        logger.error("[SW-DesignTable] 所有导入方式均失败！")
        logger.error(f"[SW-DesignTable] Excel: {excel_path}")
        logger.error(f"[SW-DesignTable] 模型: {basename_model}")
        logger.error("[SW-DesignTable] 请检查上述诊断信息中列出的参数名不匹配项。")
        logger.error("=" * 60)
        try:
            sw_app.CloseDoc(basename_model)
        except Exception:
            pass
        return False

    def _post_process_design_table(self, doc: Any, excel_path: str) -> None:  # noqa: ANN401  COM 动态对象
        """InsertFamilyTableOpen 成功后的后处理。"""
        try:
            design_table = doc.GetDesignTable()
            if design_table is not None:
                try:
                    design_table.Updatable = False
                    logger.info("[SW-DesignTable] 已禁止'模型→设计表'反向更新")
                except Exception as e_upd:
                    logger.debug(f"[SW-DesignTable] 设置 Updatable=False 失败: {e_upd}")
                try:
                    design_table.UpdateModel()
                    logger.info("[SW-DesignTable] UpdateModel 完成")
                except Exception as e_um:
                    logger.debug(f"[SW-DesignTable] UpdateModel 异常: {e_um}")
            else:
                logger.info("[SW-DesignTable] GetDesignTable 返回 None（可能已自动应用）")
        except Exception as e_dt:
            logger.debug(f"[SW-DesignTable] 后处理异常: {e_dt}")

    @staticmethod
    def _cleanup_tmp_excel(tmp_path: str, original_path: str):
        """清理临时 Excel 文件。"""
        if tmp_path and tmp_path != original_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _apply_params_via_com(self, doc: Any, excel_path: str) -> bool:  # noqa: ANN401  COM 动态对象
        """策略B: 解析 Excel 参数表，直接通过 COM API 为每个构型设置参数值。"""
        logger.info("[SW-DesignTable] 正在读取 Excel 参数表...")
        import openpyxl

        config_data = {}  # {config_name: [param_values]}
        wb = None
        try:
            wb = openpyxl.load_workbook(excel_path, data_only=True)
            ws = wb.active

            row2_cells = list(ws.iter_rows(min_row=2, max_row=2))
            if not row2_cells:
                logger.error("[SW-DesignTable] Excel 第2行缺失（应包含参数名）")
                return False
            row2 = [cell.value for cell in row2_cells[0]]
            excel_param_names = [
                str(v).strip() for v in row2[1:] if v is not None and str(v).strip()
            ]
            if not excel_param_names:
                logger.error("[SW-DesignTable] Excel 第2行无有效参数名")
                return False
            logger.info(
                f"[SW-DesignTable] Excel 参数名 ({len(excel_param_names)}个): "
                f"{excel_param_names}"
            )

            for row in ws.iter_rows(min_row=3, values_only=True):
                if row[0] is None:
                    break
                try:
                    cn = int(row[0])
                    vals = [float(row[i]) for i in range(1, len(excel_param_names) + 1)]
                    config_data[cn] = vals
                except (ValueError, TypeError, IndexError):
                    continue
            logger.info(f"[SW-DesignTable] 读取到 {len(config_data)} 个构型数据")

            if not config_data:
                logger.error("[SW-DesignTable] Excel 无有效构型数据")
                return False
        finally:
            if wb is not None:
                wb.close()

        # --- 获取模型配置列表 ---
        model_configs = []
        try:
            raw = None
            try:
                doc._FlagAsMethod('GetConfigurationNames')
                raw = doc.GetConfigurationNames()
            except TypeError:
                raw = doc.GetConfigurationNames
            if isinstance(raw, (tuple, list)):
                model_configs = [str(c) for c in raw]
            elif raw is not None:
                model_configs = [str(raw)]
        except Exception as e:
            logger.warning(f"[SW-DesignTable] 获取配置列表失败 ({type(e).__name__})，尝试替代方法...")
            for cn in sorted(config_data.keys()):
                cfg_str = str(cn)
                try:
                    doc.ShowConfiguration2(cfg_str)
                    model_configs.append(cfg_str)
                except Exception:
                    pass
        if model_configs:
            logger.info(f"[SW-DesignTable] 模型配置 ({len(model_configs)}个): {model_configs[:5]}...")
        else:
            logger.warning("[SW-DesignTable] 无法获取模型配置列表，将尝试所有 Excel 构型")

        # --- 构建参数名映射 ---
        matched_params = []
        unmatched_excel = []
        for ep in excel_param_names:
            try:
                test_param = doc.Parameter(ep)
                if test_param is not None:
                    matched_params.append(ep)
                else:
                    unmatched_excel.append(ep)
            except Exception:
                unmatched_excel.append(ep)

        if unmatched_excel:
            logger.warning(
                f"[SW-DesignTable] {len(unmatched_excel)} 个 Excel 参数在模型中未找到: "
                f"{unmatched_excel}"
            )
        if not matched_params:
            logger.error("[SW-DesignTable] 没有任何 Excel 参数与模型匹配！无法设置参数。")
            if unmatched_excel:
                logger.info(
                    f"[SW-DesignTable] 未匹配的 Excel 参数: {sorted(unmatched_excel)}"
                )
            return False
        logger.info(
            f"[SW-DesignTable] 匹配参数 ({len(matched_params)}个): {matched_params}"
        )

        # --- 逐个构型设置参数 ---
        skip_config_check = not model_configs
        success_count = 0
        for config_name, param_values in config_data.items():
            cfg_str = str(config_name)

            if not skip_config_check and cfg_str not in model_configs:
                logger.debug(f"[SW-DesignTable] 构型{config_name} 不在模型配置列表中，跳过")
                continue

            try:
                doc.ShowConfiguration2(cfg_str)
            except Exception as e:
                logger.warning(f"[SW-DesignTable] 切换构型{cfg_str}失败: {e}")
                continue

            config_ok = True
            for pname, pvalue in zip(matched_params, param_values):
                try:
                    param = doc.Parameter(pname)
                    if param is None:
                        logger.warning(f"[SW-DesignTable] 构型{config_name}: 参数'{pname}'不存在")
                        config_ok = False
                        continue
                    param.Value = pvalue
                    logger.debug(f"[SW-DesignTable] 构型{config_name}: {pname} = {pvalue}")
                except Exception as e:
                    logger.warning(
                        f"[SW-DesignTable] 构型{config_name} 设置 {pname}={pvalue} 失败: "
                        f"{type(e).__name__}: {e}"
                    )
                    config_ok = False

            if config_ok:
                success_count += 1
            else:
                logger.warning(f"[SW-DesignTable] 构型{config_name} 部分参数设置失败")

        logger.info(
            f"[SW-DesignTable] ✓ 完成: {success_count}/{len(config_data)} 个构型参数已设置"
        )
        return success_count > 0

    def _diagnose_param_mismatch(self, doc: Any, excel_path: str) -> None:  # noqa: ANN401  COM 动态对象
        """详细诊断 Excel 参数表与模型参数的不匹配情况。"""
        logger.info("=" * 60)
        logger.info("[SW-DesignTable] 参数名不匹配分析")
        logger.info("=" * 60)

        import openpyxl
        excel_params = []
        wb = None
        try:
            wb = openpyxl.load_workbook(excel_path, data_only=True)
            ws = wb.active
            row2_cells = list(ws.iter_rows(min_row=2, max_row=2))
            if row2_cells:
                row2 = [cell.value for cell in row2_cells[0]]
                excel_params = [
                    str(v).strip() for v in row2[1:] if v is not None and str(v).strip()
                ]
        except Exception:
            pass
        finally:
            if wb is not None:
                wb.close()

        model_param_names = []
        for ep in excel_params:
            try:
                p = doc.Parameter(ep)
                if p is not None:
                    model_param_names.append(ep)
            except Exception:
                pass

        logger.info(f"[SW-DesignTable] Excel 参数 ({len(excel_params)}): {excel_params}")
        logger.info(f"[SW-DesignTable] 模型匹配参数 ({len(model_param_names)}): {model_param_names}")

        unmatched = [p for p in excel_params if p not in model_param_names]

        if unmatched:
            logger.info("")
            logger.info("[SW-DesignTable] 建议修复方式：")
            logger.info("[SW-DesignTable] 1. 更新 Excel 第2行参数名，使其与模型一致")
            logger.info("[SW-DesignTable] 2. 或在 SW 中重命名模型参数，使其与 Excel 一致")
            logger.info("[SW-DesignTable] 3. 若参数名无误，检查 Excel 工作表和 SW 文档类型是否匹配")

    # ------------------------------------------------------------------
    # STEP 导出
    # ------------------------------------------------------------------

    @staticmethod
    def _guess_sw_doc_type(path: str) -> int:
        """根据文件扩展名猜测 SW 文档类型。"""
        ext = os.path.splitext(path)[1].lower()
        if ext == ".sldasm":
            return SWExecutor._SW_DOC_ASSEMBLY
        return SWExecutor._SW_DOC_PART

    @staticmethod
    def _verify_com_object(obj, label: str = "COM对象") -> bool:
        """验证 COM 对象是否有效（非 None 且代理仍存活）。"""
        if obj is None:
            logger.error(f"[SW-COM] {label} 为 None")
            return False

        for method_name in ("GetTitle", "GetPathName", "GetType"):
            try:
                val = getattr(obj, method_name)
                if callable(val):
                    _ = val()
                logger.debug(f"[SW-COM] {label} 有效 (通过 {method_name})")
                return True
            except AttributeError:
                continue
            except Exception as e:
                logger.debug(
                    f"[SW-COM] {label} {method_name} 失败: "
                    f"{type(e).__name__}"
                )
                continue

        logger.warning(
            f"[SW-COM] {label} 所有验证方法均失败，"
            f"对象可能为无效 COM 代理"
        )
        return False

    def _verify_step_exports(self, step_dir: str) -> int:
        """安全网校验：扫描所有构型的 STEP 输出文件，补标记状态数据库。"""
        all_configs = self.state.get_all_configs()
        missing_configs: list[int] = []
        found_configs: list[int] = []
        already_completed: list[int] = []

        logger.info("[SW-Export] STEP 导出完毕，正在校验各构型 STEP 文件...")
        logger.info(f"[SW-Export] 输出目录: {step_dir}")

        for cn in all_configs:
            filename = get_step_filename("sw", cn)
            if not filename:
                logger.warning(f"[SW-Export] 构型{cn}: 无法生成 STEP 文件名，跳过校验")
                continue
            expected_file = os.path.join(step_dir, filename)
            current_status = self.state.get_step_status(cn, "sw")
            if os.path.exists(expected_file):
                if current_status == STATUS_COMPLETED:
                    already_completed.append(cn)
                    logger.debug(f"[SW-Export] 构型{cn} (文件监控器已标记)")
                else:
                    self.state.set_step_status(cn, "sw", STATUS_COMPLETED)
                    found_configs.append(cn)
                    logger.debug(f"[SW-Export] 构型{cn} (安全网补标记)")
            else:
                self.state.set_step_status(
                    cn, "sw", STATUS_ERROR,
                    f"STEP 导出完毕但文件缺失: {filename}"
                )
                missing_configs.append(cn)
                logger.warning(
                    f"[SW-Export] 构型{cn} STEP 缺失 "
                    "— 可能原因: 构型重建失败 / 设计表参数错误"
                )

        total_found = len(already_completed) + len(found_configs)
        logger.info(
            f"[SW-Export] STEP 校验完成: "
            f"{total_found}/{len(all_configs)} 成功"
            f"（文件监控器实时: {len(already_completed)}，安全网: {len(found_configs)}）"
        )
        return total_found
