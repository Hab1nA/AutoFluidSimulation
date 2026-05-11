"""
===============================================================================
任务执行器 (Task Runner)
负责执行每个阶段的具体操作：

1. SW 阶段：通过 win32com 唤醒 SolidWorks，执行 Macro1.swp 宏
2. SC 阶段：通过 subprocess 无头调用 SpaceClaim
3. Transfer 阶段：通过 paramiko SSH 上传 scdoc 文件
4. Meshing 阶段：通过 SSH 远程启动网格划分后台任务
5. Solver 阶段：通过 SSH 远程启动仿真求解后台任务（全局屏障后）

每个任务执行后会更新 StateManager 中的状态。
===============================================================================
"""
import os
import shutil
import subprocess
import tempfile
import time
import threading
import gc
from typing import Optional, List

from engine.config import (
    LOCAL_PATHS, REMOTE_CONFIG, ENGINE_CONFIG,
    STATUS_COMPLETED, STATUS_ERROR, STEP_NAMES, STEP_FILE_PATTERNS, get_step_filename,
)
from utils.logger import setup_logger
from utils.ssh_client import RemoteWorkstation

logger = setup_logger(__name__)


class TaskRunner:
    """
    任务执行器。

    封装了流水线中每个步骤的执行逻辑。
    每个 execute_* 方法负责执行一个步骤并返回成功/失败。
    """

    def __init__(self, state_manager):
        """
        初始化任务执行器。

        Args:
            state_manager: StateManager 实例，用于读写任务状态
        """
        self.state = state_manager
        self._ssh: Optional[RemoteWorkstation] = None
        self._ssh_lock = threading.RLock()  # 可重入锁：SSH 操作需串行化，get_ssh() 内部也需加锁

    # ------------------------------------------------------------------
    # SSH 连接管理
    # ------------------------------------------------------------------

    def get_ssh(self) -> RemoteWorkstation:
        """
        获取（或创建）SSH 客户端实例。

        线程安全：使用 _ssh_lock 防止多线程同时创建连接。
        始终返回 RemoteWorkstation 实例（即使连接失败），
        调用者需通过 is_connected() 检查连接状态。
        """
        with self._ssh_lock:
            if self._ssh is None:
                self._ssh = RemoteWorkstation(
                    host=REMOTE_CONFIG["host"],  # type: ignore[arg-type]
                    port=REMOTE_CONFIG["port"],  # type: ignore[arg-type]
                    username=REMOTE_CONFIG["username"],  # type: ignore[arg-type]
                    password=REMOTE_CONFIG["password"],  # type: ignore[arg-type]
                )
            # 如果连接断开则尝试重连
            if not self._ssh.is_connected():
                if not self._ssh.connect():
                    logger.error("SSH 重连失败")
            return self._ssh

    def disconnect_ssh(self):
        """断开 SSH 连接。"""
        if self._ssh:
            self._ssh.disconnect()
            self._ssh = None

    # ------------------------------------------------------------------
    # 阶段 2: SolidWorks 宏执行
    # ------------------------------------------------------------------

    def _launch_solidworks_via_subprocess(self) -> bool:
        """
        备选方案：通过 subprocess 直接启动 SolidWorks.exe，
        然后轮询等待其 COM 接口就绪。

        Returns:
            True 表示 SolidWorks 进程已成功启动
        """
        sw_exe = LOCAL_PATHS.get("sw_exe", "")
        if not sw_exe or not os.path.exists(sw_exe):
            logger.warning("未配置 SolidWorks 可执行文件路径 (sw_exe)，无法使用 subprocess 启动")
            return False

        logger.info(f"正在通过 subprocess 启动 SolidWorks: {sw_exe}")
        try:
            # 使用 Popen 启动 SW，不等待其退出
            creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == "nt" else 0
            subprocess.Popen(
                [sw_exe],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
            logger.info("SolidWorks 进程已启动，等待 COM 接口就绪...")

            # 轮询等待 SW 完全启动（最多等待 60 秒）
            import win32com.client
            max_wait = 60
            for attempt in range(max_wait):
                time.sleep(1)
                try:
                    sw_app = win32com.client.GetActiveObject("SldWorks.Application")
                    if sw_app is not None:
                        logger.info(f"SolidWorks COM 接口已就绪 (等待了 {attempt + 1} 秒)")
                        return True
                except Exception:
                    pass  # SW 还没完全启动，继续等
            logger.error(f"等待 SolidWorks 启动超时 ({max_wait} 秒)")
            return False
        except FileNotFoundError:
            logger.error(f"找不到 SolidWorks 可执行文件: {sw_exe}")
            return False
        except OSError as e:
            logger.error(f"启动 SolidWorks 进程失败: {e}")
            return False

    def _validate_design_table_excel(self, excel_path: str) -> list:
        """
        预验证 Excel 设计表格式，返回诊断警告列表。

        检查项：
        1. Excel 文件是否可读取（不被锁定）
        2. 第1行是否包含 SW 设计表头特征 ("Design Table")
        3. 第2行（参数行）是否包含有效的参数列头 ($PRP@... 或 $属性@... 等)

        Args:
            excel_path: Excel 文件完整路径

        Returns:
            警告信息字符串列表（空列表 = 格式正常）
        """
        warnings = []
        logger.info(f"[诊断] 预验证 Excel 设计表格式: {os.path.basename(excel_path)}")

        # 1) 文件可读性检查
        if not os.path.exists(excel_path):
            warnings.append(f"Excel 文件不存在: {excel_path}")
            logger.warning(f"[诊断] {warnings[-1]}")
            return warnings

        file_size = os.path.getsize(excel_path)
        logger.info(f"[诊断]   文件大小: {file_size} bytes")

        try:
            import openpyxl
            wb = None
            try:
                wb = openpyxl.load_workbook(excel_path, data_only=True, read_only=True)
                ws = wb.active

                # 2) 检查行1是否为设计表头
                try:
                    row1_cells = list(ws.iter_rows(min_row=1, max_row=1))
                    if not row1_cells:
                        warnings.append("Excel 第1行缺失（空表格）")
                        logger.warning(f"[诊断] {warnings[-1]}")
                        return warnings
                    row1 = [cell.value for cell in row1_cells[0]]
                except (IndexError, StopIteration):
                    warnings.append("Excel 第1行缺失（空表格）")
                    logger.warning(f"[诊断] {warnings[-1]}")
                    return warnings
                row1_text = " ".join(str(v) for v in row1 if v is not None)
                logger.info(f"[诊断]   第1行内容: {row1_text[:120]}")

                has_design_table_header = "Design Table" in row1_text or "设计表" in row1_text
                if not has_design_table_header:
                    warnings.append(
                        f"Excel 第1行缺少 SW 设计表头（应包含 'Design Table'），"
                        f"实际内容: {row1_text[:80]}"
                    )
                    logger.warning(f"[诊断] {warnings[-1]}")

                # 3) 检查行2是否包含参数列头
                try:
                    row2_cells = list(ws.iter_rows(min_row=2, max_row=2))
                    if row2_cells:
                        row2 = [cell.value for cell in row2_cells[0]]
                        row2_text = " | ".join(str(v) for v in row2 if v is not None)
                        logger.info(f"[诊断]   第2行内容: {row2_text[:200]}")
                        # SW 参数列头通常以 $ 开头，或包含 @
                        has_param_headers = any(
                            isinstance(v, str) and ("$" in v or "@" in v)
                            for v in row2 if v is not None
                        )
                        if not has_param_headers:
                            warnings.append(
                                f"Excel 第2行似乎不含 SW 参数列头（期望格式如 '$PRP@Dimension1'），"
                                f"实际内容: {row2_text[:100]}"
                            )
                            logger.warning(f"[诊断] {warnings[-1]}")
                except (IndexError, StopIteration):
                    warnings.append("Excel 第2行缺失（应为参数列头行）")
                    logger.warning(f"[诊断] {warnings[-1]}")

                # 4) 检查数据行数
                data_rows = 0
                for row in ws.iter_rows(min_row=3, values_only=True):
                    if row[0] is not None:
                        data_rows += 1
                logger.info(f"[诊断]   数据行数 (第3行起): {data_rows}")

            finally:
                if wb is not None:
                    wb.close()

        except ImportError:
            logger.warning("[诊断] openpyxl 未安装，跳过 Excel 格式预验证")
        except Exception as e:
            warnings.append(f"Excel 格式预验证异常: {type(e).__name__}: {e}")
            logger.warning(f"[诊断] {warnings[-1]}", exc_info=True)

        if not warnings:
            logger.info("[诊断] ✓ Excel 设计表格式预验证通过")
        return warnings

    def _cleanup_sw_processes(self):
        """
        清理可能残留的 SolidWorks 进程。

        在启动新 SW 实例前调用，避免多个 SW 实例冲突或 COM 路由错误。
        仅在 Windows 平台生效。
        """
        if os.name != "nt":
            return
        try:
            # 检查是否有正在运行的 SW 进程
            result = subprocess.run(
                ["tasklist", "/fi", "IMAGENAME eq SLDWORKS.exe", "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=10,
            )
            if "SLDWORKS.exe" in result.stdout:
                logger.info("[清理] 检测到残留 SolidWorks 进程，正在终止...")
                kill_result = subprocess.run(
                    ["taskkill", "/f", "/im", "SLDWORKS.exe"],
                    capture_output=True, text=True, timeout=30,
                )
                if kill_result.returncode == 0:
                    logger.info("[清理] ✓ 残留 SolidWorks 进程已终止，等待 3 秒...")
                    time.sleep(3)
                else:
                    logger.warning(
                        f"[清理] taskkill 返回非零码 {kill_result.returncode}: "
                        f"{kill_result.stderr.strip()}"
                    )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"[清理] 检查/终止 SW 进程时异常: {e}")

    def _model_has_design_table(self, doc) -> bool:
        """
        检测模型是否已存在设计表（链接或内嵌）。

        策略：
        1. 尝试 InsertFamilyTableEdit → 若成功说明有设计表 → 立即 CloseFamilyTable
        2. 若失败，尝试 GetDesignTable 属性访问

        对已有链接设计表的模型，SolidWorks 打开模型时会自动读取设计表参数，
        无需再次调用 InsertFamilyTableOpen。

        Returns:
            True 表示模型已有设计表（无需再导入）
        """
        # 方式1: InsertFamilyTableEdit（最可靠的检测手段）
        try:
            doc.InsertFamilyTableEdit()
            logger.info("[设计表] 检测到模型已有设计表（InsertFamilyTableEdit 成功）")
            try:
                doc.CloseFamilyTable()
            except Exception:
                pass
            return True
        except Exception:
            pass

        # 方式2: GetDesignTable 属性访问（pywin32 兼容写法）
        try:
            dt = doc.GetDesignTable
            if dt is not None:
                logger.info("[设计表] 检测到模型已有设计表（GetDesignTable 返回非空）")
                return True
        except Exception:
            pass

        try:
            dt = doc.GetDesignTable()
            if dt is not None:
                logger.info("[设计表] 检测到模型已有设计表（GetDesignTable() 返回非空）")
                return True
        except Exception:
            pass

        logger.info("[设计表] 模型无设计表，将进行导入")
        return False

    def _import_design_table_with_retry(
        self, doc, sw_app, excel_path: str, sw_model: str
    ) -> bool:
        """
        带容错与多策略降级的设计表导入。

        策略优先级：
        0. 检测模型是否已有设计表 → 有则跳过导入（链接模型自动同步）
        1. InsertFamilyTableOpen → SW 原生导入（最快最可靠）
        2. 解析 Excel 参数名 → COM 直接设参（绕过设计表，兼容参数名不匹配）
        3. 详细诊断报告 → 帮助用户定位 Excel 与模型不匹配的具体原因

        Args:
            doc: SW IModelDoc2 COM 对象
            sw_app: SW ISldWorks COM 对象
            excel_path: Excel 设计表文件路径
            sw_model: SW 模型文件路径（用于 CloseDoc）

        Returns:
            True 表示参数已成功应用到模型
        """
        basename_model = os.path.basename(sw_model)

        # ---- 0) 检测模型是否已有设计表（链接或内嵌） ----
        # 链接设计表的模型在 SW 打开时已自动同步参数，无需也无法再次导入
        if self._model_has_design_table(doc):
            logger.info("[设计表] 模型已有设计表，跳过导入（已自动同步参数）")
            return True

        # ---- 1) 策略A: InsertFamilyTableOpen（最多2次） ----
        tmp_excel_path = None
        try:
            tmp_fd, tmp_excel_path = tempfile.mkstemp(
                suffix=".xlsx", prefix="sw_design_table_"
            )
            os.close(tmp_fd)
            shutil.copy2(excel_path, tmp_excel_path)
            logger.info(
                f"[设计表] 已复制 Excel 到临时文件: "
                f"{os.path.basename(tmp_excel_path)}"
            )
        except OSError as e_copy:
            logger.warning(f"[设计表] 无法复制 Excel: {e_copy}，使用原始路径")
            tmp_excel_path = excel_path

        insert_ok = False
        for attempt in (1, 2):
            if insert_ok:
                break
            import_path = tmp_excel_path or excel_path
            logger.info(
                f"[设计表] InsertFamilyTableOpen 尝试 {attempt}/2"
            )
            try:
                inserted = doc.InsertFamilyTableOpen(import_path)
                logger.info(f"[设计表] InsertFamilyTableOpen 返回: {inserted}")
                if inserted:
                    insert_ok = True
                elif attempt == 1:
                    logger.info("[设计表] 等待 3 秒后重试...")
                    time.sleep(3)
            except Exception as e_insert:
                logger.warning(
                    f"[设计表] InsertFamilyTableOpen 异常 "
                    f"({type(e_insert).__name__}: {e_insert})"
                )
                if attempt == 1:
                    time.sleep(3)

        if insert_ok:
            logger.info("[设计表] ✓ InsertFamilyTableOpen 成功")
            self._post_process_design_table(doc, excel_path)
            self._cleanup_tmp_excel(tmp_excel_path, excel_path)
            return True

        # ---- 3) 策略B: 解析 Excel，COM 直接设参 ----
        logger.info("[设计表] InsertFamilyTableOpen 失败，尝试 COM 直接设参...")
        com_ok = self._apply_params_via_com(doc, excel_path)

        # ---- 4) 清理 ----
        self._cleanup_tmp_excel(tmp_excel_path, excel_path)

        if com_ok:
            return True

        # ---- 5) 全部失败 → 详细诊断 ----
        self._diagnose_param_mismatch(doc, excel_path)
        logger.error("=" * 60)
        logger.error("[设计表] 所有导入方式均失败！")
        logger.error(f"  Excel: {excel_path}")
        logger.error(f"  模型: {basename_model}")
        logger.error("  请检查上述诊断信息中列出的参数名不匹配项。")
        logger.error("=" * 60)
        try:
            sw_app.CloseDoc(basename_model)
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # 设计表导入辅助方法
    # ------------------------------------------------------------------

    def _post_process_design_table(self, doc, excel_path: str):
        """InsertFamilyTableOpen 成功后的后处理。"""
        try:
            design_table = doc.GetDesignTable()
            if design_table is not None:
                try:
                    design_table.Updatable = False
                    logger.info("[设计表]   已禁止'模型→设计表'反向更新")
                except Exception as e_upd:
                    logger.debug(
                        f"[设计表]   设置 Updatable=False 失败: {e_upd}"
                    )
                try:
                    design_table.UpdateModel()
                    logger.info("[设计表]   ✓ UpdateModel 完成")
                except Exception as e_um:
                    logger.debug(f"[设计表]   UpdateModel 异常: {e_um}")
            else:
                logger.info("[设计表] GetDesignTable 返回 None（可能已自动应用）")
        except Exception as e_dt:
            logger.debug(f"[设计表] 后处理异常: {e_dt}")

    def _cleanup_tmp_excel(self, tmp_path: str, original_path: str):
        """清理临时 Excel 文件。"""
        if tmp_path and tmp_path != original_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _apply_params_via_com(self, doc, excel_path: str) -> bool:
        """
        策略B: 解析 Excel 参数表，直接通过 COM API 为每个构型设置参数值。

        适用于：InsertFamilyTableOpen 失败时（参数名不匹配 / SW 版本差异等）。

        工作流程：
        1. 读取 Excel → 提取行2参数名列表 + 行3+构型数据
        2. 枚举模型配置 → 逐个激活
        3. 通过 doc.Parameter(name).SystemValue = value 设置参数
        4. 仅设置 Excel 和模型都存在的参数（跳过不匹配的）

        Returns:
            True 表示至少有一个构型的参数被成功设置
        """
        logger.info("[COM设参] 正在读取 Excel 参数表...")
        import openpyxl

        wb = None
        try:
            wb = openpyxl.load_workbook(excel_path, data_only=True)
            ws = wb.active

            # --- 提取行2参数名 ---
            row2_cells = list(ws.iter_rows(min_row=2, max_row=2))
            if not row2_cells:
                logger.error("[COM设参] Excel 第2行缺失（应包含参数名）")
                return False
            row2 = [cell.value for cell in row2_cells[0]]
            # 第1列（index 0）是构型名称列，参数从第2列开始
            excel_param_names = [
                str(v).strip() for v in row2[1:] if v is not None and str(v).strip()
            ]
            if not excel_param_names:
                logger.error("[COM设参] Excel 第2行无有效参数名")
                return False
            logger.info(
                f"[COM设参] Excel 参数名 ({len(excel_param_names)}个): "
                f"{excel_param_names}"
            )

            # --- 提取行3+构型数据 ---
            config_data = {}  # {config_name: [param_values]}
            for row in ws.iter_rows(min_row=3, values_only=True):
                if row[0] is None:
                    break
                try:
                    cn = int(row[0])
                    vals = [float(row[i]) for i in range(1, len(excel_param_names) + 1)]
                    config_data[cn] = vals
                except (ValueError, TypeError, IndexError):
                    continue
            logger.info(f"[COM设参] 读取到 {len(config_data)} 个构型数据")

            if not config_data:
                logger.error("[COM设参] Excel 无有效构型数据")
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
            logger.warning(f"[COM设参] 获取配置列表失败 ({type(e).__name__})，尝试替代方法...")
            # 备选：从 Excel 数据中推断配置名
            for cn in sorted(config_data.keys()):
                cfg_str = str(cn)
                try:
                    doc.ShowConfiguration2(cfg_str)
                    model_configs.append(cfg_str)
                except Exception:
                    pass
        if model_configs:
            logger.info(f"[COM设参] 模型配置 ({len(model_configs)}个): {model_configs[:5]}...")
        else:
            logger.warning("[COM设参] 无法获取模型配置列表，将尝试所有 Excel 构型")

        # --- 构建参数名映射：逐个验证 Excel 参数在模型中是否存在 ---
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
                f"[COM设参] {len(unmatched_excel)} 个 Excel 参数在模型中未找到: "
                f"{unmatched_excel}"
            )
        if not matched_params:
            logger.error("[COM设参] 没有任何 Excel 参数与模型匹配！无法设置参数。")
            if unmatched_excel:
                logger.info(
                    f"[COM设参] 未匹配的 Excel 参数: {sorted(unmatched_excel)}"
                )
            return False
        logger.info(
            f"[COM设参] 匹配参数 ({len(matched_params)}个): {matched_params}"
        )

        # --- 逐个构型设置参数 ---
        # 若无法获取模型配置列表，直接尝试所有 Excel 构型
        skip_config_check = not model_configs
        success_count = 0
        for config_name, param_values in config_data.items():
            cfg_str = str(config_name)

            # 仅在已知配置列表时才做存在性检查
            if not skip_config_check and cfg_str not in model_configs:
                logger.debug(f"[COM设参] 构型{config_name} 不在模型配置列表中，跳过")
                continue

            try:
                doc.ShowConfiguration2(cfg_str)
            except Exception as e:
                logger.warning(f"[COM设参] 切换构型{cfg_str}失败: {e}")
                continue

            # 设置每个匹配的参数
            config_ok = True
            for pname, pvalue in zip(matched_params, param_values):
                try:
                    param = doc.Parameter(pname)
                    if param is None:
                        logger.warning(
                            f"[COM设参] 构型{config_name}: 参数'{pname}'不存在"
                        )
                        config_ok = False
                        continue
                    # 使用 Value 属性（文档显示单位），设计表值已是正确单位
                    # SystemValue 始终为 SI 单位（米），手动转换对非长度参数不可靠
                    param.Value = pvalue
                    logger.debug(
                        f"[COM设参]   构型{config_name}: {pname} = {pvalue}"
                    )
                except Exception as e:
                    logger.warning(
                        f"[COM设参] 构型{config_name} 设置 {pname}={pvalue} 失败: "
                        f"{type(e).__name__}: {e}"
                    )
                    config_ok = False

            if config_ok:
                success_count += 1
            else:
                logger.warning(f"[COM设参] 构型{config_name} 部分参数设置失败")

        logger.info(
            f"[COM设参] ✓ 完成: {success_count}/{len(config_data)} 个构型参数已设置"
        )
        return success_count > 0

    def _diagnose_param_mismatch(self, doc, excel_path: str):
        """
        详细诊断 Excel 参数表与模型参数的不匹配情况。
        帮助用户快速定位问题。
        """
        logger.info("=" * 60)
        logger.info("[诊断] 参数名不匹配分析")
        logger.info("=" * 60)

        # 读取 Excel 参数名
        import openpyxl
        excel_params = []
        try:
            wb = openpyxl.load_workbook(excel_path, data_only=True)
            ws = wb.active
            row2_cells = list(ws.iter_rows(min_row=2, max_row=2))
            if row2_cells:
                row2 = [cell.value for cell in row2_cells[0]]
                excel_params = [
                    str(v).strip() for v in row2[1:] if v is not None and str(v).strip()
                ]
            wb.close()
        except Exception:
            pass

        # 获取模型参数 — 逐个测试 Excel 参数名
        model_param_names = []
        for ep in excel_params:
            try:
                p = doc.Parameter(ep)
                if p is not None:
                    model_param_names.append(ep)
            except Exception:
                pass

        logger.info(f"  Excel 参数 ({len(excel_params)}): {excel_params}")
        logger.info(f"  模型匹配参数 ({len(model_param_names)}): {model_param_names}")

        unmatched = [p for p in excel_params if p not in model_param_names]

        if unmatched:
            logger.info("")
            logger.info("  建议修复方式：")
            logger.info("  1. 更新 Excel 第2行参数名，使其与模型一致")
            logger.info("  2. 或在 SW 中重命名模型参数，使其与 Excel 一致")
            logger.info("  3. 若参数名无误，检查 Excel 工作表和 SW 文档类型是否匹配")

    @staticmethod
    def _verify_com_object(obj, label: str = "COM对象") -> bool:
        """
        验证 COM 对象是否有效（非 None 且代理仍存活）。

        通过调用一个无副作用的属性读取来检测代理是否仍连接到后端对象。
        COM 代理可能在以下情况失效：
        - 后端 SW 进程崩溃
        - COM 引用计数归零导致对象被释放
        - 跨线程封送失败

        Args:
            obj: COM 对象
            label: 用于日志的对象描述

        Returns:
            True 表示 COM 对象有效
        """
        if obj is None:
            logger.error(f"[COM验证] {label} 为 None")
            return False

        for method_name in ("GetTitle", "GetPathName", "GetType"):
            try:
                val = getattr(obj, method_name)
                if callable(val):
                    _ = val()
                logger.debug(f"[COM验证] ✓ {label} 有效 (通过 {method_name})")
                return True
            except AttributeError:
                continue
            except Exception as e:
                logger.debug(
                    f"[COM验证] {label} {method_name} 失败: "
                    f"{type(e).__name__}"
                )
                continue

        logger.warning(
            f"[COM验证] {label} 所有验证方法均失败，"
            f"对象可能为无效 COM 代理"
        )
        return False
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

    @staticmethod
    def _guess_sw_doc_type(path: str) -> int:
        """
        根据文件扩展名猜测 SW 文档类型，用于 OpenDoc6 / GetMacroMethods。
        """
        ext = os.path.splitext(path)[1].lower()
        if ext == ".sldasm":
            return TaskRunner._SW_DOC_ASSEMBLY
        return TaskRunner._SW_DOC_PART

    def _export_configs_to_step(self, doc, step_dir: str):
        """
        直接通过 COM API 遍历所有配置并导出 STEP 文件。

        替代原 Macro1.swp 宏文件中的导出循环。
        ForceRebuildAll 已在调用前完成，此处仅负责切换配置并导出。
        每个构型导出后立即更新状态数据库。

        Args:
            doc: SW IModelDoc2 COM 对象（模型已打开）
            step_dir: STEP 文件输出目录

        Returns:
            (success_count, fail_count, failed_configs_list)
        """
        logger.info("正在通过 COM 直接导出各构型 STEP 文件...")

        import pythoncom
        import win32com.client

        # 获取所有配置名称（兼容 pywin32 属性/方法两种访问方式）
        conf_names = []
        try:
            doc._FlagAsMethod('GetConfigurationNames')
            raw = doc.GetConfigurationNames()
        except TypeError:
            raw = doc.GetConfigurationNames
        if isinstance(raw, (tuple, list)):
            conf_names = [str(c) for c in raw]
        elif raw is not None:
            conf_names = [str(raw)]

        if not conf_names:
            logger.error("无法获取模型配置名称列表")
            return 0, 0, []

        logger.info(f"发现 {len(conf_names)} 个配置，开始逐构型导出...")

        success_configs = []
        fail_configs = []

        for cn_str in conf_names:
            # 尝试解析为整数（匹配 configs 表中的构型名称）
            try:
                cn_int = int(cn_str)
            except ValueError:
                cn_int = None

            filename = get_step_filename("SW", cn_int) if cn_int is not None else None
            if not filename:
                logger.warning(f"  构型{cn_str}: 无法生成 STEP 文件名，跳过")
                fail_configs.append(cn_int if cn_int is not None else cn_str)
                continue

            filepath = os.path.join(step_dir, filename)

            # ---- 切换配置 ----
            try:
                doc.ShowConfiguration2(cn_str)
            except Exception as e:
                logger.error(
                    f"  构型{cn_str}: ShowConfiguration2 失败 "
                    f"({type(e).__name__}: {e})"
                )
                if cn_int is not None:
                    self.state.set_step_status(
                        cn_int, "SW", STATUS_ERROR,
                        f"ShowConfiguration2 失败: {type(e).__name__}: {e}"
                    )
                    fail_configs.append(cn_int)
                continue

            # ---- 导出 STEP ----
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

                if status:
                    logger.info(
                        f"  ✓ 构型{cn_str}: {os.path.basename(filepath)} "
                        f"(Errors={save_errors.value}, Warnings={save_warnings.value})"
                    )
                    if cn_int is not None:
                        self.state.set_step_status(cn_int, "SW", STATUS_COMPLETED)
                        success_configs.append(cn_int)
                else:
                    logger.warning(
                        f"  ✗ 构型{cn_str}: SaveAs 返回 False "
                        f"(Errors={save_errors.value}, Warnings={save_warnings.value})"
                    )
                    if cn_int is not None:
                        self.state.set_step_status(
                            cn_int, "SW", STATUS_ERROR,
                            f"SaveAs 返回 False (Errors={save_errors.value})"
                        )
                        fail_configs.append(cn_int)
            except Exception as e:
                logger.error(
                    f"  ✗ 构型{cn_str}: SaveAs 异常 ({type(e).__name__}: {e})"
                )
                if cn_int is not None:
                    self.state.set_step_status(
                        cn_int, "SW", STATUS_ERROR,
                        f"SaveAs 异常: {type(e).__name__}: {e}"
                    )
                    fail_configs.append(cn_int)

        total = len(success_configs) + len(fail_configs)
        logger.info(
            f"STEP 导出完成: {len(success_configs)}/{total} 成功"
            f"（{len(fail_configs)} 失败）"
        )
        return len(success_configs), len(fail_configs), fail_configs

    # ------------------------------------------------------------------
    # 阶段 2: SolidWorks STEP 导出（入口）
    # ------------------------------------------------------------------

    def execute_sw_macro(self) -> bool:
        """
        SolidWorks STEP 批量导出（直接 COM 调用，不再依赖宏文件）。

        执行流程：
        1. 三层降级连接/启动 SolidWorks
        2. OpenDoc6 打开模型文件
        3. 导入 Excel 设计表（带容错重试）
        4. ForceRebuildAll 重建所有构型
        5. 逐构型 ShowConfiguration2 → SaveAs 导出 STEP
        6. 校验并汇总导出结果

        Returns:
            True 表示至少有一个构型导出成功
        """
        logger.info("=" * 60)
        logger.info("启动 SolidWorks STEP 导出流程")
        logger.info("=" * 60)

        sw_model = LOCAL_PATHS["sw_model"]
        excel_path = LOCAL_PATHS.get("excel", "")
        step_dir = LOCAL_PATHS.get("step_dir", "")
        doc_type = self._guess_sw_doc_type(sw_model)

        # 检查必要文件
        if not os.path.exists(sw_model):
            logger.error(f"SW 模型文件不存在: {sw_model}")
            return False
        if not excel_path or not os.path.exists(excel_path):
            logger.error(f"Excel 参数表不存在: {excel_path}")
            return False
        if not step_dir:
            logger.error("未配置 STEP 输出目录 (step_dir)")
            return False
        try:
            os.makedirs(step_dir, exist_ok=True)
        except OSError as e:
            logger.error(f"无法创建/访问 STEP 输出目录: {step_dir}: {e}")
            return False

        # ---- 预验证 Excel 设计表格式 ----
        excel_warnings = self._validate_design_table_excel(excel_path)
        if excel_warnings:
            for w in excel_warnings:
                logger.warning(f"  ⚠ {w}")

        # ---- 清理残留 SW 进程（仅在连接失败时作为备选方案的辅助） ----
        # 注意：不在此处无条件清理，避免误杀用户正在运行的 SW 实例。
        # _launch_solidworks_via_subprocess 会在启动新的 SW 进程前自行处理。

        try:
            import win32com.client
            import pythoncom

            pythoncom.CoInitialize()

            try:
                sw_app = None
                doc = None

                # ---- 第1层: 连接已运行的 SW ----
                logger.info("正在连接 SolidWorks (第1层: GetActiveObject)...")
                try:
                    sw_app = win32com.client.GetActiveObject("SldWorks.Application")
                    logger.info("已连接到运行中的 SolidWorks 实例")
                except Exception as e1:
                    logger.info(
                        f"GetActiveObject 失败 ({type(e1).__name__}: {e1})，"
                        f"尝试启动新实例..."
                    )

                    # ---- 第2层: 通过 COM Dispatch 启动 ----
                    logger.info("正在启动 SolidWorks (第2层: COM Dispatch)...")
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
                        logger.info("SolidWorks 已通过 COM Dispatch 启动")
                        # 等待 SW 窗口完全加载
                        time.sleep(8)
                    except Exception as e2:
                        logger.warning(
                            f"COM Dispatch 失败 ({type(e2).__name__}: {e2})，"
                            f"尝试备选方案..."
                        )

                        # ---- 第3层: 直接启动 exe ----
                        logger.info("正在启动 SolidWorks (第3层: subprocess)...")
                        # 在 subprocess 启动前清理可能残留的 SW 僵尸进程
                        self._cleanup_sw_processes()
                        if not self._launch_solidworks_via_subprocess():
                            logger.error("所有启动方式均失败，无法连接 SolidWorks")
                            return False
                        sw_app = win32com.client.GetActiveObject("SldWorks.Application")
                        try:
                            sw_app.Visible = bool(ENGINE_CONFIG.get("sw_visible", True))
                        except Exception:
                            pass
                        logger.info("已通过 subprocess 启动并连接 SolidWorks")

                # ---- 确保 SW 可见且就绪 ----
                try:
                    sw_app.Visible = bool(ENGINE_CONFIG.get("sw_visible", True))
                except Exception:
                    pass

                # ---- 步骤 A: 宏模块解析已跳过（直接 COM 导出模式） ----
                logger.info("宏解析: 已跳过（使用直接 COM 导出，不依赖宏文件）")

                # ---- 步骤 B: 打开模型文件 (OpenDoc6) ----
                # OpenDoc6(FileName, Type, Options, Configuration, Errors, Warnings)
                logger.info(f"正在打开模型 (OpenDoc6): {os.path.basename(sw_model)}")
                open_errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                open_warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

                try:
                    doc = sw_app.OpenDoc6(
                        sw_model,
                        doc_type,                # Type: 1=swDocPART, 2=swDocASSEMBLY
                        self._SW_OPEN_SILENT,    # Options: 1=Silent（抑制弹窗）
                        "",                       # Configuration: 空=上次保存的配置
                        open_errors,
                        open_warnings,
                    )
                    logger.info(
                        f"OpenDoc6: Errors={open_errors.value}, "
                        f"Warnings={open_warnings.value}"
                    )
                    if open_errors.value != 0:
                        logger.warning(
                            f"OpenDoc6 返回错误码 {open_errors.value}，"
                            f"模型可能存在问题（缺失参考/重建错误），"
                            f"后续宏执行可能异常"
                        )
                except Exception as open_err:
                    logger.error(
                        f"OpenDoc6 异常 ({type(open_err).__name__}: {open_err})"
                    )
                    return False

                if doc is None:
                    logger.error(
                        f"无法打开 SW 模型: {sw_model}"
                        f"（文件可能损坏、版本不兼容，或路径含特殊字符）"
                    )
                    return False

                # ---- 验证 doc COM 对象有效性 ----
                if not self._verify_com_object(doc, "IModelDoc2"):
                    logger.error(
                        "OpenDoc6 返回了无效的文档 COM 代理，"
                        "模型可能未正确加载"
                    )
                    try:
                        sw_app.CloseDoc(os.path.basename(sw_model))
                    except Exception:
                        pass
                    return False
                logger.info(f"✓ 模型已打开: {os.path.basename(sw_model)}")

                # ---- 步骤 B2: 从文件导入设计表（带容错与重试） ----
                if not self._import_design_table_with_retry(
                    doc, sw_app, excel_path, sw_model
                ):
                    return False

                # ---- 步骤 B3: 重建所有构型 ----
                # 导入设计表后，模型参数已更新但几何体未重建。
                # ForceRebuildAll 强制重建所有构型而不逐个激活。
                # pywin32 延迟绑定可能导致 ForceRebuildAll 被误识别为属性，
                # 使用 _FlagAsMethod 确保其作为方法调用。
                logger.info("正在重建所有构型（ForceRebuildAll）...")
                rebuild_ok = False
                # 策略1: 显式标记 ForceRebuildAll 为方法 (pywin32 兼容)
                try:
                    ext = doc.Extension
                    ext._FlagAsMethod('ForceRebuildAll')
                    ext.ForceRebuildAll()
                    rebuild_ok = True
                    logger.info("✓ 所有构型重建完成 (ForceRebuildAll)")
                except Exception as e_rebuild:
                    logger.warning(
                        f"ForceRebuildAll 策略1 失败 "
                        f"({type(e_rebuild).__name__}: {e_rebuild})"
                    )
                # 策略2: 降级为逐个配置重建
                if not rebuild_ok:
                    try:
                        logger.info("降级为逐个配置 EditRebuild3...")
                        doc._FlagAsMethod('GetConfigurationNames')
                        raw = doc.GetConfigurationNames()
                        if isinstance(raw, (tuple, list)):
                            configs = [str(c) for c in raw]
                        elif raw is not None:
                            configs = [str(raw)]
                        else:
                            configs = []
                        rebuilt_count = 0
                        for cfg in configs:
                            try:
                                doc.ShowConfiguration2(cfg)
                                doc.EditRebuild3()
                                rebuilt_count += 1
                            except Exception:
                                pass
                        if rebuilt_count > 0:
                            rebuild_ok = True
                            logger.info(f"✓ 逐个配置重建完成 ({rebuilt_count}/{len(configs)} 个)")
                        else:
                            logger.warning("逐个配置重建: 0 个成功")
                    except Exception as e_rebuild2:
                        logger.warning(
                            f"逐个配置重建失败 "
                            f"({type(e_rebuild2).__name__}: {e_rebuild2})"
                        )

                # ---- 步骤 C: 直接 COM 导出 STEP（替代已弃用的 Macro1.swp） ----
                # 通过 COM API 遍历配置 → ShowConfiguration2 → SaveAs 导出 STEP。
                # 不再依赖宏文件，消除 VBA 模块名/过程名匹配问题。
                logger.info("正在通过 COM 直接导出各构型 STEP 文件...")
                success_cnt, fail_cnt, failed_cfgs = self._export_configs_to_step(doc, step_dir)
                logger.info(
                    f"COM 直接导出完成: {success_cnt} 成功, {fail_cnt} 失败"
                    f"（{f'失败构型: {failed_cfgs}' if failed_cfgs else '无失败'}）"
                )
                if success_cnt == 0:
                    logger.error("所有构型 STEP 导出均失败，无法继续")
                    try:
                        sw_app.CloseDoc(os.path.basename(sw_model))
                    except Exception:
                        pass
                    return False

                logger.info("STEP 导出完毕，正在校验各构型 STEP 文件...")
                logger.info(f"  输出目录: {LOCAL_PATHS.get('step_dir', '?')}")

                # ---- 步骤 D: 逐构型安全网校验 STEP 文件 ----
                # _export_configs_to_step 已逐构型更新状态，此处作为安全网——
                # 对文件监控器或导出方法可能遗漏的构型做补标记，
                # 同时检测因错误而缺失的 STEP 文件。
                all_configs = self.state.get_all_configs()
                missing_configs: List[int] = []
                found_configs: List[int] = []
                already_completed: List[int] = []

                for cn in all_configs:
                    filename = get_step_filename("SW", cn)
                    if not filename:
                        logger.warning(f"  构型{cn}: 无法生成 STEP 文件名，跳过校验")
                        continue
                    expected_file = os.path.join(
                        step_dir,
                        filename
                    )
                    current_sw = self.state.get_step_status(cn, "SW")
                    if os.path.exists(expected_file):
                        if current_sw == STATUS_COMPLETED:
                            # 文件监控器已实时检测到并标记完成
                            already_completed.append(cn)
                            logger.debug(f"  构型{cn} ✓ (文件监控器已标记)")
                        else:
                            # 安全网：文件存在但文件监控器尚未标记 → 补标记
                            self.state.set_step_status(cn, "SW", STATUS_COMPLETED)
                            found_configs.append(cn)
                            logger.debug(f"  构型{cn} ✓ (安全网补标记)")
                    else:
                        self.state.set_step_status(
                            cn, "SW", STATUS_ERROR,
                            f"STEP 导出完毕但文件缺失: "
                            f"{filename}"
                        )
                        missing_configs.append(cn)
                        logger.warning(f"  构型{cn} ✗ STEP 缺失")

                total_found = len(already_completed) + len(found_configs)
                logger.info(
                    f"STEP 校验完成: "
                    f"{total_found}/{len(all_configs)} 成功"
                    f"（文件监控器实时: {len(already_completed)}，安全网: {len(found_configs)}）"
                )
                if missing_configs:
                    logger.warning(
                        f"缺失构型: {sorted(missing_configs)} "
                        f"— 可能原因: 构型重建失败 / 设计表参数错误"
                    )
                # 只要至少有一个构型的 STEP 存在，就标记 SW 宏已完成
                # （缺失的构型会在 start_pipeline 的扫尾逻辑中处理）
                if total_found > 0:
                    self.state.set_sw_macro_started(True)
                    logger.info(
                        f"sw_macro_started=True "
                        f"（{total_found}/{len(all_configs)} 构型 STEP 就绪）"
                    )

                return True
            finally:
                # ---- 清理：关闭模型文档 → 退出 SW → 释放 COM 资源 ----

                # 步骤 1: 关闭已打开的模型文档
                if doc is not None and ENGINE_CONFIG.get("sw_close_doc_on_finish", True):
                    try:
                        title = doc.GetTitle()
                    except Exception:
                        title = os.path.basename(sw_model)
                    try:
                        sw_app.CloseDoc(title)  # type: ignore[union-attr]
                        logger.info(f"已关闭模型文档: {title}")
                    except Exception as e_doc:
                        logger.debug(f"关闭模型文档异常: {e_doc}")

                # 步骤 2: 退出 SolidWorks 应用程序
                if sw_app is not None and ENGINE_CONFIG.get("sw_exit_on_finish", True):
                    try:
                        sw_app.ExitApp()
                        logger.info("已请求 SolidWorks 退出 (ExitApp)")
                    except Exception as e_exit:
                        logger.warning(
                            f"ExitApp 调用异常 ({type(e_exit).__name__}): {e_exit}，"
                            f"尝试强制终止..."
                        )
                        self._cleanup_sw_processes()
                    else:
                        # ExitApp 调用成功，等待 SW 进程实际退出
                        logger.info("等待 SolidWorks 进程退出...")
                        sw_exited = False
                        for _ in range(15):  # 最多等待 15 秒
                            time.sleep(1)
                            try:
                                result = subprocess.run(
                                    ["tasklist", "/fi", "IMAGENAME eq SLDWORKS.exe",
                                     "/fo", "csv", "/nh"],
                                    capture_output=True, text=True, timeout=5,
                                )
                                if "SLDWORKS.exe" not in result.stdout:
                                    sw_exited = True
                                    logger.info("✓ SolidWorks 进程已退出")
                                    break
                            except Exception:
                                break
                        if not sw_exited:
                            logger.warning(
                                "SolidWorks 未在 15 秒内退出，强制终止..."
                            )
                            self._cleanup_sw_processes()

                # 步骤 3: 显式释放 COM 对象引用，帮助 pywin32 及时回收
                # 在 CoUninitialize 前用 del 确保 COM 代理的 __del__ 被调用，
                # 防止下次 Dispatch 返回退化的 IDispatch 代理。
                del doc
                del sw_app
                gc.collect()
                # 双重 CoFreeUnusedLibraries 确保 STA 消息泵排空
                for _ in range(2):
                    try:
                        pythoncom.CoFreeUnusedLibraries()
                    except Exception:
                        pass
                    time.sleep(0.5)
                pythoncom.CoUninitialize()

        except ImportError:
            logger.error("win32com 未安装，请执行: pip install pywin32")
            return False
        except Exception as e:
            logger.error(
                f"SW 宏执行失败 ({type(e).__name__}: {e})", exc_info=True
            )
            return False

    # ------------------------------------------------------------------
    # 阶段 3: SpaceClaim 脚本执行
    # ------------------------------------------------------------------

    def execute_spaceclaim(self, config_name: int) -> bool:
        """
        通过 subprocess 无头调用 SpaceClaim，将 STEP 转换为 SCDOC。

        调用格式（严格按需求）：
        SpaceClaim.exe /RunScript="<脚本路径>" /ScriptArgs="<构型名称>"

        Args:
            config_name: 构型名称（整数）

        Returns:
            True 表示 SC 脚本执行成功
        """
        _sw_step_name = get_step_filename("SW", config_name)
        if not _sw_step_name:
            logger.error("无法生成 STEP 文件名：STEP_FILE_PATTERNS['SW'] 未配置或格式错误")
            self.state.set_step_status(config_name, "SC", STATUS_ERROR, "STEP 文件名配置错误")
            return False
        _scdoc_name = get_step_filename("SC", config_name)
        if not _scdoc_name:
            logger.error("无法生成 SCDOC 文件名：STEP_FILE_PATTERNS['SC'] 未配置或格式错误")
            self.state.set_step_status(config_name, "SC", STATUS_ERROR, "SCDOC 文件名配置错误")
            return False

        step_file = os.path.join(LOCAL_PATHS["step_dir"], _sw_step_name)
        scdoc_file = os.path.join(LOCAL_PATHS["scdoc_dir"], _scdoc_name)

        # 检查输入文件
        if not os.path.exists(step_file):
            logger.error(f"STEP 文件不存在: {step_file}")
            self.state.set_step_status(config_name, "SC", STATUS_ERROR, "STEP 文件不存在")
            return False

        sc_exe = LOCAL_PATHS["sc_exe"]
        sc_script = LOCAL_PATHS["sc_script"]

        if not os.path.exists(sc_exe):
            logger.error(f"SpaceClaim 可执行文件不存在: {sc_exe}")
            self.state.set_step_status(config_name, "SC", STATUS_ERROR, "SC 程序不存在")
            return False
        if not os.path.exists(sc_script):
            logger.error(f"SC 脚本文件不存在: {sc_script}")
            self.state.set_step_status(config_name, "SC", STATUS_ERROR, "SC 脚本不存在")
            return False

        # 构建命令行（字符串格式，避免 list2cmdline 破坏引号）
        #
        # 关键：subprocess.Popen 接收列表时，Windows 上会调用 list2cmdline()
        # 将列表转为命令行字符串。list2cmdline 会将参数中已有的双引号用
        # 反斜杠转义（" → \"），导致 SpaceClaim 收到的命令行变为：
        #   /RunScript=\"C:\path\script.scscript\" /ScriptArgs=\"1\"
        # SpaceClaim 无法解析反斜杠转义的引号，因此既找不到脚本也获取不到参数。
        #
        # 解决方案：传入字符串而非列表，Python 直接将字符串传给 CreateProcess，
        # 跳过 list2cmdline 转换，完全掌控引号格式。
        #
        # 命令行格式：
        #   SpaceClaim.exe "step文件" /RunScript="脚本" /ScriptArgs="构型名"
        # - STEP 文件作为位置参数，SpaceClaim 启动时自动加载
        # - /RunScript 指定要执行的脚本
        # - /ScriptArgs 传递构型名称给脚本
        cmd = f'"{sc_exe}" "{step_file}" /RunScript="{sc_script}" /ScriptArgs="{config_name}"'

        logger.info(f"SpaceClaim 启动: 构型{config_name}")
        logger.info(f"命令: {cmd}")

        try:
            sc_creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == "nt" else 0
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=sc_creation_flags,
            )

            # 等待完成（带超时）
            timeout = ENGINE_CONFIG["sc_timeout"]
            try:
                stdout, stderr = process.communicate(timeout=timeout)
                if process.returncode != 0:
                    logger.error(f"SC 脚本执行失败 (exit={process.returncode}): {stderr}")
                    self.state.set_step_status(
                        config_name, "SC", STATUS_ERROR,
                        f"SC 退出码={process.returncode}: {stderr[:200]}"
                    )
                    return False
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass  # 进程已自行退出
                except OSError as e:
                    logger.error(f"无法终止 SC 进程: {e}")
                try:
                    process.communicate(timeout=5)  # 回收子进程资源，避免僵尸进程
                except subprocess.TimeoutExpired:
                    logger.warning("SC 进程在 kill 后未及时退出")
                logger.error(f"SC 脚本执行超时 ({timeout}s)")
                self.state.set_step_status(config_name, "SC", STATUS_ERROR, "SC 执行超时")
                return False

            # 验证输出文件
            if os.path.exists(scdoc_file):
                logger.info(f"SC 转换完成: model_gen4_{config_name}.scdoc")
                return True
            else:
                logger.error(f"SC 输出文件未生成: {scdoc_file}")
                self.state.set_step_status(config_name, "SC", STATUS_ERROR, "SCDOC 文件未生成")
                return False

        except OSError as e:
            logger.error(f"SC 执行失败 (IO错误): {e}")
            self.state.set_step_status(config_name, "SC", STATUS_ERROR, f"IO错误: {e}")
            return False
        except ValueError as e:
            logger.error(f"SC 执行失败 (配置错误): {e}")
            self.state.set_step_status(config_name, "SC", STATUS_ERROR, f"配置错误: {e}")
            return False
        except RuntimeError as e:
            logger.error(f"SC 执行失败 (运行时错误): {e}")
            self.state.set_step_status(config_name, "SC", STATUS_ERROR, f"运行时错误: {e}")
            return False
        except Exception as e:
            logger.error(f"SC 执行失败 (未知错误): {e}", exc_info=True)
            self.state.set_step_status(config_name, "SC", STATUS_ERROR, f"未知错误: {e}")
            return False

    # ------------------------------------------------------------------
    # 阶段 4: 文件传输 (SCDOC -> 远程工作站)
    # ------------------------------------------------------------------

    def execute_transfer(self, config_name: int) -> bool:
        """
        通过 SFTP 将 SCDOC 文件上传到远程工作站。

        Args:
            config_name: 构型名称

        Returns:
            True 表示传输成功
        """
        _scdoc_name = get_step_filename("SC", config_name)
        if not _scdoc_name:
            logger.error("无法生成 SCDOC 文件名：STEP_FILE_PATTERNS['SC'] 未配置或格式错误")
            self.state.set_step_status(config_name, "Transfer", STATUS_ERROR, "SCDOC 文件名配置错误")
            return False
        local_file = os.path.join(
            str(LOCAL_PATHS["scdoc_dir"]),
            _scdoc_name,
        )
        remote_file = os.path.join(
            str(REMOTE_CONFIG["scdoc_dir"]),
            _scdoc_name,
        ).replace("\\", "/")

        if not os.path.exists(local_file):
            logger.error(f"本地 SCDOC 文件不存在: {local_file}")
            self.state.set_step_status(config_name, "Transfer", STATUS_ERROR, "本地文件不存在")
            return False

        with self._ssh_lock:
            try:
                ssh = self.get_ssh()
                success = ssh.upload_file(local_file, remote_file)
                if success:
                    logger.info(f"文件传输完成: 构型{config_name}")
                    return True
                else:
                    self.state.set_step_status(config_name, "Transfer", STATUS_ERROR, "SFTP 上传失败")
                    return False
            except (OSError, ConnectionError) as e:
                logger.error(f"文件传输异常: {e}")
                self.state.set_step_status(config_name, "Transfer", STATUS_ERROR, str(e))
                return False

    # ------------------------------------------------------------------
    # 阶段 5: 网格划分 (远程后台任务)
    # ------------------------------------------------------------------

    def execute_meshing(self, config_name: int) -> bool:
        """
        在远程工作站启动网格划分后台任务。

        通过 SSH 触发，使用 PowerShell Start-Process 确保 SSH 断开后进程存活。
        命令格式: python batch_meshing_gen4.py XX

        Args:
            config_name: 构型名称

        Returns:
            True 表示后台任务启动成功（不等待完成）
        """
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/meshing_done_{config_name}.txt".replace("\\", "/")

        # 构建远程命令：激活 conda 环境后执行网格脚本
        conda_env = REMOTE_CONFIG["conda_env"]
        conda_exe = REMOTE_CONFIG["conda_exe"]
        meshing_script = REMOTE_CONFIG["meshing_script"]
        command = f'call "{conda_exe}" activate {conda_env} && python "{meshing_script}" {config_name}'

        logger.info(f"启动远程网格划分: 构型{config_name}")
        logger.debug(f"远程命令: {command}")

        with self._ssh_lock:
            try:
                ssh = self.get_ssh()
                success = ssh.exec_background(command, flag_file)
                if success:
                    logger.info(f"网格划分后台任务已启动: 构型{config_name}")
                    # 启动后不等待，由调度器轮询标志文件
                    return True
                else:
                    self.state.set_step_status(config_name, "Meshing", STATUS_ERROR, "远程任务启动失败")
                    return False
            except (OSError, ConnectionError) as e:
                logger.error(f"网格划分启动异常: {e}")
                self.state.set_step_status(config_name, "Meshing", STATUS_ERROR, str(e))
                return False

    def wait_meshing_completion(self, config_name: int) -> bool:
        """
        轮询等待网格划分完成。

        Args:
            config_name: 构型名称

        Returns:
            True 表示网格划分成功完成
        """
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/meshing_done_{config_name}.txt".replace("\\", "/")

        with self._ssh_lock:
            try:
                ssh = self.get_ssh()
                success = ssh.wait_for_flag(
                    flag_file,
                    timeout=ENGINE_CONFIG["meshing_timeout"],  # type: ignore[arg-type]
                    poll_interval=10
                )
                return success
            except (OSError, ConnectionError) as e:
                logger.error(f"等待网格划分异常: {e}")
                return False

    # ------------------------------------------------------------------
    # 阶段 6: 仿真求解 (全局屏障后，远程后台任务)
    # ------------------------------------------------------------------

    def execute_solver(self, config_name: int) -> bool:
        """
        在远程工作站启动仿真求解后台任务。

        仅在全局屏障通过后调用。
        命令格式: python batch_solver_gen4.py XX

        Args:
            config_name: 构型名称

        Returns:
            True 表示后台任务启动成功
        """
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/solver_done_{config_name}.txt".replace("\\", "/")

        conda_env = REMOTE_CONFIG["conda_env"]
        conda_exe = REMOTE_CONFIG["conda_exe"]
        solver_script = REMOTE_CONFIG["solver_script"]
        command = f'call "{conda_exe}" activate {conda_env} && python "{solver_script}" {config_name}'

        logger.info(f"启动远程仿真求解: 构型{config_name}")

        with self._ssh_lock:
            try:
                ssh = self.get_ssh()
                success = ssh.exec_background(command, flag_file)
                if success:
                    logger.info(f"仿真求解后台任务已启动: 构型{config_name}")
                    return True
                else:
                    self.state.set_step_status(config_name, "Solver", STATUS_ERROR, "远程求解启动失败")
                    return False
            except (OSError, ConnectionError) as e:
                logger.error(f"仿真求解启动异常: {e}")
                self.state.set_step_status(config_name, "Solver", STATUS_ERROR, str(e))
                return False

    def wait_solver_completion(self, config_name: int) -> bool:
        """轮询等待仿真求解完成。"""
        flag_file = f"{REMOTE_CONFIG['flag_dir']}/solver_done_{config_name}.txt".replace("\\", "/")

        with self._ssh_lock:
            try:
                ssh = self.get_ssh()
                success = ssh.wait_for_flag(
                    flag_file,
                    timeout=ENGINE_CONFIG["solver_timeout"],  # type: ignore[arg-type]
                    poll_interval=30  # 求解时间较长，轮询间隔加大
                )
                return success
            except (OSError, ConnectionError) as e:
                logger.error(f"等待仿真求解异常: {e}")
                return False

    # ------------------------------------------------------------------
    # 系统自检
    # ------------------------------------------------------------------

    def run_system_check(self) -> dict:
        """
        执行系统自检：检查本地路径、SSH 连通性、远程进程状态。

        Returns:
            自检结果字典
        """
        results: dict[str, dict[str, object]] = {
            "local_checks": {},
            "remote_checks": {},
        }

        checks = {
            "SW模型": LOCAL_PATHS["sw_model"],
            "Excel参数表": LOCAL_PATHS["excel"],
            "STEP目录": LOCAL_PATHS["step_dir"],
            "SC程序": LOCAL_PATHS["sc_exe"],
            "SC脚本": LOCAL_PATHS["sc_script"],
            "SCDOC目录": LOCAL_PATHS["scdoc_dir"],
            "日志目录": LOCAL_PATHS["log_dir"],
        }
        for name, path in checks.items():
            exists = os.path.exists(path)
            results["local_checks"][name] = {"path": path, "exists": exists}

        # ---- 远程检查 ----
        try:
            ssh = self.get_ssh()
            if ssh.is_connected():
                results["remote_checks"]["ssh"] = "连接成功"
                remote_info = ssh.check_system(conda_exe=REMOTE_CONFIG["conda_exe"])  # type: ignore[arg-type]
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
        """
        清理指定步骤产生的文件（包括本地和远程工作站上的文件）。

        Args:
            step_name: 步骤名，或 "all" 表示全部步骤
            config_name: 构型名称，若为 None 或 "all" 则清理所有构型
        """
        # 解析 "all" 语义
        if config_name == "all":
            config_name = None

        if step_name == "all":
            for s in STEP_NAMES:
                self._clean_single_step(s, config_name)
        else:
            self._clean_single_step(step_name, config_name)

    def _clean_single_step(self, step_name: str, config_name: int | None = None):
        """清理单个步骤的文件（内部方法）。

        文件命名模式来源于 engine.config.STEP_FILE_PATTERNS，
        由此处统一引用以确保清理与实际产生的文件匹配。
        """
        local_patterns = {
            "SW":       ("step_dir",  STEP_FILE_PATTERNS["SW"],       None),
            "SC":       ("scdoc_dir", STEP_FILE_PATTERNS["SC"],       None),
            "Transfer": None,
            "Meshing":  None,
            "Solver":   None,
        }

        # ---- 远程文件清理映射 ----
        # (目录key, 文件名模板, 额外清理的后缀对 (原后缀, 新后缀))
        remote_patterns = {
            "SW":       None,  # SW 无远程文件
            "SC":       None,  # SC 无远程文件（SCDOC 由 transfer 上传，但源文件在本地）
            "Transfer": None,  # 传输无产出文件
            "Meshing":  ("msh_dir",    STEP_FILE_PATTERNS["Meshing"], None),
            "Solver":   ("result_dir", STEP_FILE_PATTERNS["Solver"],  (".cas.h5", ".dat.h5")),
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
                    pass  # 文件本就不存在，无需处理
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
            dir_key, file_template, extra_suffix_pair = remote_info
            target_dir = str(REMOTE_CONFIG.get(dir_key, ""))
            try:
                ssh = self.get_ssh()
                if ssh.is_connected():
                    for cn in configs:
                        filename = str(file_template).format(config=cn)
                        # 统一使用 / 作为远程路径分隔符（SFTP 协议标准，Windows 兼容）
                        remote_path = f"{target_dir.replace(chr(92), '/')}/{filename}"
                        ssh.delete_remote_file(remote_path)
                        if extra_suffix_pair:
                            old_suffix, new_suffix = extra_suffix_pair
                            extra_filename = filename.rsplit(old_suffix, 1)[0] + new_suffix
                            extra_remote_path = f"{target_dir.replace(chr(92), '/')}/{extra_filename}"
                            ssh.delete_remote_file(extra_remote_path)
                    logger.info(f"步骤 {step_name} 远程文件清理完成 ({target_dir})")
                else:
                    logger.warning(f"SSH 未连接，跳过远程文件清理: {step_name}")
            except (OSError, ConnectionError) as e:
                logger.error(f"远程文件清理异常 ({step_name}): {e}")

        logger.info(f"步骤 {step_name} 文件清理完成")
