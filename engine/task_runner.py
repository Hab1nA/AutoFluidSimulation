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
import subprocess
import time
import threading
from typing import Optional, Callable, List

from engine.config import (
    LOCAL_PATHS, REMOTE_CONFIG, ENGINE_CONFIG,
    STATUS_WAITING, STATUS_RUNNING, STATUS_COMPLETED, STATUS_ERROR, STATUS_RETRYING,
    STEP_NAMES, STEP_FILE_PATTERNS,
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
                    host=REMOTE_CONFIG["host"],
                    port=REMOTE_CONFIG["port"],
                    username=REMOTE_CONFIG["username"],
                    password=REMOTE_CONFIG["password"],
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
            subprocess.Popen(
                [sw_exe],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            logger.info("SolidWorks 进程已启动，等待 COM 接口就绪...")

            # 轮询等待 SW 完全启动（最多等待 60 秒）
            import pythoncom
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

    # ------------------------------------------------------------------
    # SW 2025 API 常量（硬编码以避免 import 依赖）
    # ------------------------------------------------------------------
    # swDocumentTypes_e
    _SW_DOC_PART = 1
    _SW_DOC_ASSEMBLY = 2
    # swOpenDocOptions_e (用于 OpenDoc6)
    _SW_OPEN_SILENT = 1       # 静默：抑制警告对话框
    _SW_OPEN_READONLY = 2     # 只读
    # swRunMacroOption_e (用于 RunMacro2)
    _SW_RUN_MACRO_DEFAULT = 0
    _SW_RUN_MACRO_UNLOAD_AFTER = 1

    def execute_sw_macro(self) -> bool:
        """
        执行 SolidWorks 宏 (Macro1.swp)。

        SW 宏会一次性批量导出所有构型的 STEP 文件到 step_dir。
        整个流水线只需要调用一次此方法（RunMacro2 同步阻塞至宏完成）。

        执行策略（三层降级）：
        1. GetActiveObject → 连接到已运行的 SW 实例
        2. Dispatch → 通过 COM 注册表启动新 SW 实例
        3. subprocess → 直接启动 SLDWORKS.exe，轮询 GetActiveObject

        SW 2025 API 参考：
        - OpenDoc6(FileName, Type, Options, Configuration, Errors, Warnings)
          取代已废弃的 OpenDoc2，推荐使用 OpenDoc6
        - RunMacro2(FilePath, ModuleName, ProcedureName, Options, Error)
          需通过 GetMacroMethods() 动态获取 VBA 模块名

        Returns:
            True 表示宏已执行完毕（RunMacro2 同步等待完成）
        """
        logger.info("=" * 60)
        logger.info("启动 SolidWorks 宏执行")
        logger.info("=" * 60)

        sw_model = LOCAL_PATHS["sw_model"]
        sw_macro = LOCAL_PATHS["sw_macro"]

        # 检查必要文件
        if not os.path.exists(sw_model):
            logger.error(f"SW 模型文件不存在: {sw_model}")
            return False
        if not os.path.exists(sw_macro):
            logger.error(f"SW 宏文件不存在: {sw_macro}")
            return False

        try:
            import win32com.client
            import pythoncom

            pythoncom.CoInitialize()

            try:
                sw_app = None

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
                        sw_app.Visible = True
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
                        if not self._launch_solidworks_via_subprocess():
                            logger.error("所有启动方式均失败，无法连接 SolidWorks")
                            return False
                        sw_app = win32com.client.GetActiveObject("SldWorks.Application")
                        sw_app.Visible = True
                        logger.info("已通过 subprocess 启动并连接 SolidWorks")

                # ---- 确保 SW 可见且就绪 ----
                try:
                    sw_app.Visible = True
                except Exception:
                    pass

                # ---- 步骤 A: 动态获取宏的模块名 ----
                logger.info("正在解析宏结构...")
                macro_module = None
                macro_proc = "main"
                try:
                    # GetMacroMethods(FilePath, DocumentType) → tuple of "Module.Proc" strings
                    methods = sw_app.GetMacroMethods(sw_macro, self._SW_DOC_PART)
                    logger.info(f"GetMacroMethods(PART): {methods}")
                    if methods and isinstance(methods, (tuple, list)) and len(methods) > 0:
                        first = methods[0]
                        if isinstance(first, str) and "." in first:
                            macro_module, macro_proc = first.split(".", 1)
                            logger.info(
                                f"发现宏入口: 模块='{macro_module}', 过程='{macro_proc}'"
                            )
                except Exception as e_methods:
                    logger.warning(
                        f"GetMacroMethods 失败 ({type(e_methods).__name__}: {e_methods})，"
                        f"将使用启发式搜索"
                    )

                # ---- 步骤 B: 打开模型文件 (OpenDoc6) ----
                # OpenDoc6(FileName, Type, Options, Configuration, Errors, Warnings)
                logger.info(f"正在打开模型 (OpenDoc6): {os.path.basename(sw_model)}")
                open_errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                open_warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

                try:
                    doc = sw_app.OpenDoc6(
                        sw_model,
                        self._SW_DOC_PART,       # Type: 1=swDocPART
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
                logger.info(f"✓ 模型已打开: {os.path.basename(sw_model)}")

                # ---- 步骤 B2: 从文件导入设计表 ----
                # 模型不再内嵌链接到设计表，改为每次打开后从 Excel 文件导入。
                # IModelDoc2::InsertFamilyTableOpen(FileName) 会将指定 Excel
                # 作为设计表插入模型，返回 True 表示导入成功。
                excel_path = LOCAL_PATHS.get("excel", "")
                if excel_path and os.path.exists(excel_path):
                    logger.info(
                        f"正在导入设计表: {os.path.basename(excel_path)}"
                    )
                    try:
                        inserted = doc.InsertFamilyTableOpen(excel_path)
                        if inserted:
                            logger.info("✓ 设计表已导入")
                            # 导入后获取设计表接口，确保更新方向正确
                            try:
                                design_table = doc.GetDesignTable()
                                if design_table is not None:
                                    # 禁止反向更新（模型 → 设计表）
                                    try:
                                        design_table.Updatable = False
                                        logger.info("  已禁止'模型→设计表'反向更新")
                                    except Exception as e_upd:
                                        logger.debug(
                                            f"  设置 Updatable=False 失败 "
                                            f"({type(e_upd).__name__}: {e_upd})"
                                        )
                                    # 显式将设计表更改应用到模型
                                    try:
                                        design_table.UpdateModel()
                                        logger.info("  ✓ 设计表更改已应用到模型")
                                    except Exception as e_um:
                                        logger.warning(
                                            f"  UpdateModel 失败 "
                                            f"({type(e_um).__name__}: {e_um})，"
                                            f"模型可能已是最新状态"
                                        )
                                else:
                                    logger.warning(
                                        "InsertFamilyTableOpen 返回 True 但 "
                                        "GetDesignTable 返回 None，"
                                        "设计表更改可能未应用到模型"
                                    )
                            except Exception as e_dt2:
                                logger.debug(
                                    f"设计表后处理异常 "
                                    f"({type(e_dt2).__name__}: {e_dt2})"
                                )
                        else:
                            logger.error(
                                f"设计表导入失败: InsertFamilyTableOpen 返回 False"
                            )
                            logger.error(f"  请检查 Excel 文件格式是否正确: {excel_path}")
                            # 关闭文档，因为模型可能缺少必要参数
                            try:
                                sw_app.CloseDoc(os.path.basename(sw_model))
                            except Exception:
                                pass
                            return False
                    except Exception as e_insert:
                        logger.error(
                            f"InsertFamilyTableOpen 异常 "
                            f"({type(e_insert).__name__}: {e_insert})"
                        )
                        try:
                            sw_app.CloseDoc(os.path.basename(sw_model))
                        except Exception:
                            pass
                        return False
                else:
                    logger.info("未配置 Excel 设计表路径或文件不存在，跳过导入")

                # ---- 步骤 B3: 重建所有构型 ----
                # 导入设计表后，模型参数已更新但几何体未重建。
                # 若不重建，宏文件只会导出已激活过的构型。
                # ForceRebuildAll 强制重建所有构型而不逐个激活。
                logger.info("正在重建所有构型（ForceRebuildAll）...")
                try:
                    doc.Extension.ForceRebuildAll()
                    logger.info("✓ 所有构型重建完成")
                except Exception as e_rebuild:
                    logger.error(
                        f"ForceRebuildAll 失败 "
                        f"({type(e_rebuild).__name__}: {e_rebuild})"
                    )
                    # 降级：尝试普通重建
                    try:
                        doc.EditRebuild3()
                        logger.info("  已降级为当前构型重建")
                    except Exception as e_rebuild2:
                        logger.warning(
                            f"降级重建也失败 "
                            f"({type(e_rebuild2).__name__}: {e_rebuild2})"
                        )

                # ---- 步骤 C: 执行宏 (RunMacro2) ----
                # RunMacro2(FilePath, ModuleName, ProcedureName, Options, Error)
                # 注意：RunMacro2 是同步阻塞的 COM 调用，sw_macro_timeout 作为预期
                # 最大时长记录但不由程序强制中断（COM STA 对象不支持跨线程超时控制）。
                # 若宏卡死，需手动结束 SolidWorks 进程。
                logger.info(
                    f"正在执行宏 (RunMacro2): {os.path.basename(sw_macro)} "
                    f"(预期最大耗时 {ENGINE_CONFIG['sw_macro_timeout']}s)"
                )
                macro_ok = False
                last_error_detail = ""

                # 构建模块名尝试列表（动态发现的模块名优先）
                candidate_modules = []
                if macro_module:
                    candidate_modules.append(macro_module)
                # 常见回退模块名
                for name in ["Module1", "Macro1", "Macro11", "MainModule", "Module"]:
                    if name not in candidate_modules:
                        candidate_modules.append(name)

                # 过程名候选列表
                candidate_procs = [macro_proc] if macro_proc else ["main"]
                for p in ["main", "Main", "MainProc"]:
                    if p not in candidate_procs:
                        candidate_procs.append(p)

                for mod_name in candidate_modules:
                    if macro_ok:
                        break
                    for proc_name in candidate_procs:
                        if macro_ok:
                            break
                        try:
                            run_error = win32com.client.VARIANT(
                                pythoncom.VT_BYREF | pythoncom.VT_I4, 0
                            )
                            logger.info(
                                f"  RunMacro2: module='{mod_name}', "
                                f"proc='{proc_name}'"
                            )
                            result = sw_app.RunMacro2(
                                sw_macro,
                                mod_name,
                                proc_name,
                                self._SW_RUN_MACRO_DEFAULT,
                                run_error,
                            )
                            logger.info(
                                f"  RunMacro2 返回: {result}, Error={run_error.value}"
                            )
                            if result:
                                logger.info(
                                    f"✓ 宏已启动并完成 "
                                    f"(模块: {mod_name}, 过程: {proc_name})"
                                )
                                macro_ok = True
                            else:
                                last_error_detail = (
                                    f"module='{mod_name}', proc='{proc_name}', "
                                    f"Error={run_error.value}"
                                )
                                logger.debug(f"  失败: {last_error_detail}")
                        except Exception as e_macro:
                            last_error_detail = (
                                f"module='{mod_name}', proc='{proc_name}', "
                                f"{type(e_macro).__name__}: {e_macro}"
                            )
                            logger.debug(f"  异常: {last_error_detail}")

                if not macro_ok:
                    logger.error("所有宏执行方式均失败。最后错误详情:")
                    logger.error(f"  {last_error_detail}")
                    logger.error("请检查:")
                    logger.error(f"  1. 宏文件是否可正常运行: {sw_macro}")
                    logger.error(
                        f"  2. VBA 模块名（可用 GetMacroMethods 查询）"
                    )
                    logger.error(
                        f"  3. VBA 过程是否为 Public Sub（无参数）"
                    )
                    # 关闭已打开的文档
                    try:
                        sw_app.CloseDoc(os.path.basename(sw_model))
                    except Exception:
                        pass
                    return False

                logger.info("SW 宏执行完毕，正在校验各构型 STEP 文件...")
                logger.info(f"  输出目录: {LOCAL_PATHS.get('step_dir', '?')}")
                self.state.set_sw_macro_started(True)

                # ---- 步骤 D: 逐构型校验 STEP 文件 ----
                # RunMacro2 是同步调用，返回时宏已执行完毕。
                # 逐个检查预期 STEP 文件是否存在：
                #   存在 → SW 步骤 Completed
                #   缺失 → SW 步骤 Error
                all_configs = self.state.get_all_configs()
                step_dir = LOCAL_PATHS.get("step_dir", "")
                missing_configs: List[int] = []
                found_configs: List[int] = []

                for cn in all_configs:
                    expected_file = os.path.join(
                        step_dir,
                        f"model_gen4.SLDPRT_{cn}.step"
                    )
                    if os.path.exists(expected_file):
                        self.state.set_step_status(cn, "SW", STATUS_COMPLETED)
                        found_configs.append(cn)
                        logger.debug(f"  构型{cn} ✓")
                    else:
                        self.state.set_step_status(
                            cn, "SW", STATUS_ERROR,
                            f"宏执行完毕但 STEP 缺失: "
                            f"model_gen4.SLDPRT_{cn}.step"
                        )
                        missing_configs.append(cn)
                        logger.warning(f"  构型{cn} ✗ STEP 缺失")

                logger.info(
                    f"STEP 校验完成: "
                    f"{len(found_configs)}/{len(all_configs)} 成功"
                )
                if missing_configs:
                    logger.warning(
                        f"缺失构型: {sorted(missing_configs)} "
                        f"— 可能原因: 构型重建失败 / 设计表参数错误"
                    )

                # 不关闭 SW（保留以供调试/断点续传检查）
                return True
            finally:
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
        step_file = os.path.join(
            LOCAL_PATHS["step_dir"],
            f"model_gen4.SLDPRT_{config_name}.step"
        )
        scdoc_file = os.path.join(
            LOCAL_PATHS["scdoc_dir"],
            f"model_gen4_{config_name}.scdoc"
        )

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

        # 构建命令行（严格按需求格式）
        cmd = [
            sc_exe,
            f'/RunScript="{sc_script}"',
            f'/ScriptArgs="{config_name}"',
        ]

        logger.info(f"SpaceClaim 启动: 构型{config_name}")
        logger.debug(f"命令: {' '.join(cmd)}")

        try:
            # 使用 subprocess 启动 SpaceClaim（无头模式）
            # SpaceClaim 在 /RunScript 模式下会自动在脚本执行完毕后退出
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
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

        except (OSError, ValueError, RuntimeError) as e:
            logger.error(f"SC 执行异常: {e}")
            self.state.set_step_status(config_name, "SC", STATUS_ERROR, str(e))
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
        local_file = os.path.join(
            LOCAL_PATHS["scdoc_dir"],
            f"model_gen4_{config_name}.scdoc"
        )
        remote_file = os.path.join(
            REMOTE_CONFIG["scdoc_dir"],
            f"model_gen4_{config_name}.scdoc"
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
                    timeout=ENGINE_CONFIG["meshing_timeout"],
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
                    timeout=ENGINE_CONFIG["solver_timeout"],
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
        results = {
            "local_checks": {},
            "remote_checks": {},
        }

        # ---- 本地检查 ----
        checks = {
            "SW模型": LOCAL_PATHS["sw_model"],
            "Excel参数表": LOCAL_PATHS["excel"],
            "SW宏文件": LOCAL_PATHS["sw_macro"],
            "STEP目录": LOCAL_PATHS["step_dir"],
            "SC程序": LOCAL_PATHS["sc_exe"],
            "SC脚本": LOCAL_PATHS["sc_script"],
            "SCDOC目录": LOCAL_PATHS["scdoc_dir"],
            "日志目录": LOCAL_PATHS["log_dir"],
        }
        for name, path in checks.items():
            exists = os.path.exists(path)
            results["local_checks"][name] = {
                "path": path,
                "exists": exists,
            }

        # ---- 远程检查 ----
        try:
            ssh = self.get_ssh()
            if ssh.is_connected():
                results["remote_checks"]["ssh"] = "连接成功"
                remote_info = ssh.check_system(conda_exe=REMOTE_CONFIG["conda_exe"])
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

    def _clean_single_step(self, step_name: str, config_name: int = None):
        """清理单个步骤的文件（内部方法）。

        文件命名模式来源于 engine.config.STEP_FILE_PATTERNS，
        由此处统一引用以确保清理与实际产生的文件匹配。
        """
        # ---- 本地文件清理映射 ----
        # (目录key, 文件名模板, 额外清理的后缀对 (原后缀, 新后缀))
        # 注意：SW 导出的 STEP 文件名为 model_gen4.SLDPRT_{N}.step（含 .SLDPRT 中缀）
        local_patterns = {
            "SW":       ("step_dir",  STEP_FILE_PATTERNS["SW"],       None),
            "SC":       ("scdoc_dir", STEP_FILE_PATTERNS["SC"],       None),
            "Transfer": None,  # 传输无本地文件
            "Meshing":  None,  # MSH 文件仅在远程工作站上
            "Solver":   None,  # CAS/DAT 文件仅在远程工作站上
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
            target_dir = LOCAL_PATHS.get(dir_key, "")
            for cn in configs:
                filename = file_template.format(config=cn)
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
            target_dir = REMOTE_CONFIG.get(dir_key, "")
            try:
                ssh = self.get_ssh()
                if ssh.is_connected():
                    for cn in configs:
                        filename = file_template.format(config=cn)
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
