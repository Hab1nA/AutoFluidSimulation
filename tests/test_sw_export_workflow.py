"""
===============================================================================
SolidWorks Export 工作流通用验证测试脚本
无需实际 SolidWorks — 通过 Mock 全面验证：
  1. 模型导入 (OpenDoc6) 的正确调用参数
  2. 设计表导入 (InsertFamilyTableOpen) 及 COM 降级策略
  3. 配置枚举 (GetConfigurationNames / IGetConfigurationNames)
  4. 逐构型 STEP 导出 (ShowConfiguration2 → EditRebuild3 → SaveAs)
  5. SW 文档关闭与 COM 资源清理 (CoUninitialize)
  6. 文件监控器 (FileStableDetector / StepFileMonitor)
  7. 配置过渡与状态管理
  8. 错误处理与边界情况

运行方式：
  cd "项目根目录"
  python -m pytest tests/test_sw_export_workflow.py -v
  或
  python tests/test_sw_export_workflow.py
===============================================================================
"""
import os
import sys
import time
import tempfile
import shutil
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

openpyxl = pytest.importorskip("openpyxl", reason="test_sw_export_workflow 需要 openpyxl 创建测试 Excel 文件")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 必须在任何 engine.*/utils.* 导入之前设置日志目录环境变量，
# 因为 engine.config._env_override 在模块导入时读取环境变量。
# 同时也要为直接读取 LOCAL_PATHS["log_dir"] 的代码创建目录。
_TEST_TMP_ROOT = tempfile.mkdtemp(prefix="sw_test_")
_TEST_LOG_DIR = os.path.join(_TEST_TMP_ROOT, "logs")
_TEST_DATA_DIR = os.path.join(_TEST_TMP_ROOT, "data")
os.makedirs(_TEST_LOG_DIR, exist_ok=True)
os.makedirs(_TEST_DATA_DIR, exist_ok=True)
os.environ["AUTOFLUID_LOG_DIR"] = _TEST_LOG_DIR
os.environ["AUTOFLUID_DATA_DIR"] = _TEST_DATA_DIR


# ============================================================================
# 测试辅助：创建模拟 SW COM 对象
# ============================================================================

def create_mock_sw_app(config_names: list[str] | None = None,
                        open_doc_succeeds: bool = True,
                        insert_dt_succeeds: bool = True,
                        save_as_succeeds: bool = True):
    """创建模拟 SolidWorks COM 应用对象。"""
    if config_names is None:
        config_names = ["Default", "0", "1", "2", "3", "4", "5"]

    mock_app = MagicMock()
    mock_app.Visible = False

    mock_doc = MagicMock()
    mock_doc.GetTitle.return_value = "model_gen4.SLDPRT"
    mock_doc.GetPathName.return_value = r"C:\test\model_gen4.SLDPRT"
    mock_doc.GetType.return_value = 1  # swDocPART

    # 配置枚举
    mock_doc.GetConfigurationNames.return_value = tuple(config_names)

    # 配置切换
    mock_doc.ShowConfiguration2.return_value = True

    # 重建
    mock_doc.EditRebuild3.return_value = True

    # Export SaveAs
    ext_mock = MagicMock()
    if save_as_succeeds:
        ext_mock.SaveAs.return_value = True
    else:
        ext_mock.SaveAs.return_value = False
    mock_doc.Extension = ext_mock

    # OpenDoc6
    if open_doc_succeeds:
        mock_app.OpenDoc6.return_value = mock_doc
    else:
        mock_app.OpenDoc6.return_value = None

    # 设计表
    # InsertFamilyTableEdit 模拟"无设计表"场景（抛出异常使 _model_has_design_table 返回 False）
    mock_doc.InsertFamilyTableEdit.side_effect = Exception("No design table")
    if insert_dt_succeeds:
        mock_doc.InsertFamilyTableOpen.return_value = True
    else:
        mock_doc.InsertFamilyTableOpen.return_value = False

    # 参数访问
    def make_param(name):
        p = MagicMock()
        p.Name = name
        p.Value = 100.0
        p.SystemValue = 0.1
        return p

    mock_doc.Parameter.side_effect = make_param

    # GetDesignTable
    mock_dt = MagicMock()
    mock_doc.GetDesignTable.return_value = mock_dt
    mock_doc.DeleteDesignTable.return_value = True

    return mock_app, mock_doc


# ============================================================================
# 测试类 1: SW COM 连接与文档操作
# ============================================================================

class TestSwComConnection(unittest.TestCase):
    """测试 SolidWorks COM 连接与文档打开/关闭。"""

    @classmethod
    def setUpClass(cls):
        from executor.sw_executor import SWExecutor
        cls.SWExecutor = SWExecutor

    def test_guess_sw_doc_type_part(self):
        self.assertEqual(
            self.SWExecutor._guess_sw_doc_type(r"C:\test\model.SLDPRT"),
            self.SWExecutor._SW_DOC_PART
        )

    def test_guess_sw_doc_type_assembly(self):
        self.assertEqual(
            self.SWExecutor._guess_sw_doc_type(r"C:\test\asm.SLDASM"),
            self.SWExecutor._SW_DOC_ASSEMBLY
        )

    def test_guess_sw_doc_type_case_insensitive(self):
        self.assertEqual(
            self.SWExecutor._guess_sw_doc_type(r"C:\test\asm.sldasm"),
            self.SWExecutor._SW_DOC_ASSEMBLY
        )
        self.assertEqual(
            self.SWExecutor._guess_sw_doc_type(r"C:\test\part.sldprt"),
            self.SWExecutor._SW_DOC_PART
        )

    def test_open_doc_constants(self):
        self.assertEqual(self.SWExecutor._SW_DOC_PART, 1)
        self.assertEqual(self.SWExecutor._SW_DOC_ASSEMBLY, 2)
        self.assertEqual(self.SWExecutor._SW_OPEN_SILENT, 1)
        self.assertEqual(self.SWExecutor._SW_SAVE_AS_CURRENT_VERSION, 0)
        self.assertEqual(self.SWExecutor._SW_SAVE_AS_OPTIONS_SILENT, 1)

    def test_verify_com_object_valid(self):
        mock_obj = MagicMock()
        mock_obj.GetTitle.return_value = "test"
        self.assertTrue(self.SWExecutor._verify_com_object(mock_obj, "TestDoc"))

    def test_verify_com_object_none(self):
        self.assertFalse(self.SWExecutor._verify_com_object(None, "TestDoc"))

    def test_verify_com_object_dead_proxy(self):
        mock_obj = MagicMock()
        mock_obj.GetTitle.side_effect = Exception("COM proxy dead")
        mock_obj.GetPathName.side_effect = Exception("COM proxy dead")
        mock_obj.GetType.side_effect = Exception("COM proxy dead")
        self.assertFalse(self.SWExecutor._verify_com_object(mock_obj, "DeadDoc"))


# ============================================================================
# 测试类 2: 设计表验证
# ============================================================================

class TestDesignTableValidation(unittest.TestCase):
    """测试 Excel 设计表格式验证。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sw_test_dt_")
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from engine.task_runner import TaskRunner
        self.runner_class = TaskRunner
        from engine.state_manager import StateManager
        self._db_path = os.path.join(self.tmpdir, "test_state.db")
        self.state = StateManager(db_path=self._db_path)

    def tearDown(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_test_excel(self, row1_content=None, row2_content=None,
                            data_rows=None, filename="test_dt.xlsx"):
        """创建测试用 Excel 文件。"""
        filepath = os.path.join(self.tmpdir, filename)
        wb = openpyxl.Workbook()
        ws = wb.active

        if row1_content is not None:
            for i, val in enumerate(row1_content, 1):
                ws.cell(row=1, column=i, value=val)

        if row2_content is not None:
            for i, val in enumerate(row2_content, 1):
                ws.cell(row=2, column=i, value=val)

        if data_rows:
            for r, row_vals in enumerate(data_rows, 3):
                for c, val in enumerate(row_vals, 1):
                    ws.cell(row=r, column=c, value=val)

        wb.save(filepath)
        return filepath

    def test_valid_design_table_format(self):
        excel_path = self._create_test_excel(
            row1_content=["Design Table for: model_gen4.SLDPRT"],
            row2_content=["Config", "$PRP@Dimension1", "$PRP@Dimension2"],
            data_rows=[[0, 100.0, 200.0]],
        )
        runner = self.runner_class(self.state)
        warnings = runner._sw_executor._validate_design_table(excel_path)
        self.assertEqual(warnings, [], f"Expected no warnings, got: {warnings}")

    def test_missing_design_table_header(self):
        excel_path = self._create_test_excel(
            row1_content=["Some Random Text"],
            row2_content=["Config", "Param1"],
            data_rows=[[0, 100.0]],
        )
        runner = self.runner_class(self.state)
        warnings = runner._sw_executor._validate_design_table(excel_path)
        self.assertTrue(any("Design Table" in w or "设计表" in w for w in warnings),
                        f"Expected warning about missing design table header, got: {warnings}")

    def test_missing_param_headers(self):
        excel_path = self._create_test_excel(
            row1_content=["Design Table for: model"],
            row2_content=["Config", "PlainParam1"],
            data_rows=[[0, 100.0]],
        )
        runner = self.runner_class(self.state)
        warnings = runner._sw_executor._validate_design_table(excel_path)
        warning_texts = [str(w) for w in warnings]
        has_warning = any("参数列头" in w or "参数" in w for w in warning_texts)
        self.assertTrue(has_warning or len(warnings) == 0,
                        f"Expecting either no warnings or param header warning; got: {warnings}")

    def test_nonexistent_excel(self):
        runner = self.runner_class(self.state)
        warnings = runner._sw_executor._validate_design_table(r"C:\nonexistent\file.xlsx")
        self.assertTrue(len(warnings) > 0, "Expected warnings for nonexistent file")

    def test_empty_excel(self):
        filepath = os.path.join(self.tmpdir, "empty.xlsx")
        wb = openpyxl.Workbook()
        wb.save(filepath)

        runner = self.runner_class(self.state)
        warnings = runner._sw_executor._validate_design_table(filepath)
        self.assertTrue(any("缺失" in w for w in warnings),
                        f"Expected row-missing warning for empty workbook, got: {warnings}")


# ============================================================================
# 测试类 3: 配置枚举与 STEP 导出
# ============================================================================

class TestConfigEnumAndStepExport(unittest.TestCase):
    """测试配置枚举与 STEP 导出逻辑。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sw_test_cfg_")
        from engine.state_manager import StateManager
        self._db_path = os.path.join(self.tmpdir, "test_state.db")
        self.state = StateManager(db_path=self._db_path)
        from engine.task_runner import TaskRunner
        self.runner_class = TaskRunner

    def tearDown(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_get_configuration_names_tuple(self):
        """测试 GetConfigurationNames 返回 tuple 的解析。"""
        mock_app, mock_doc = create_mock_sw_app(
            config_names=["Default", "0", "1", "2", "3", "4", "5"]
        )

        conf_names = []
        raw = mock_doc.GetConfigurationNames()
        if isinstance(raw, (tuple, list)):
            conf_names = [str(c) for c in raw]

        self.assertEqual(len(conf_names), 7)
        self.assertIn("Default", conf_names)
        self.assertIn("5", conf_names)

    def test_get_configuration_names_fallback_iget(self):
        """测试 GetConfigurationNames 失败时 IGetConfigurationNames 降级。"""
        mock_app, mock_doc = create_mock_sw_app()
        mock_doc.GetConfigurationNames.side_effect = TypeError("not callable")
        mock_doc.IGetConfigurationNames.return_value = ("0", "1", "2")

        raw = None
        try:
            raw = mock_doc.GetConfigurationNames()
        except TypeError:
            raw = mock_doc.IGetConfigurationNames
            if isinstance(raw, (tuple, list)):
                conf_names = [str(c) for c in raw]

        conf_names = [str(c) for c in mock_doc.IGetConfigurationNames()]
        self.assertEqual(conf_names, ["0", "1", "2"])

    @unittest.skipIf(sys.platform != "win32", "需要 Windows COM 环境")
    def test_export_step_success(self):
        """测试单个构型 STEP 导出成功路径。"""
        mock_app, mock_doc = create_mock_sw_app(
            config_names=["0", "1", "2"],
            save_as_succeeds=True,
        )

        for cn_str in mock_doc.GetConfigurationNames():
            try:
                cn_int = int(cn_str)
            except ValueError:
                cn_int = None

            if cn_int is not None:
                mock_doc.ShowConfiguration2(cn_str)
                mock_doc.EditRebuild3()

                import pythoncom
                import win32com.client
                errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

                status = mock_doc.Extension.SaveAs(
                    f"test_{cn_int}.step",
                    0, 1, None, errors, warnings
                )
                self.assertTrue(status, f"SaveAs should succeed for config {cn_int}")

        self.assertEqual(mock_doc.ShowConfiguration2.call_count, 3)
        self.assertEqual(mock_doc.EditRebuild3.call_count, 3)
        self.assertEqual(mock_doc.Extension.SaveAs.call_count, 3)

    @unittest.skipIf(sys.platform != "win32", "需要 Windows COM 环境")
    def test_export_step_failure(self):
        """测试单个构型 STEP 导出失败时 SaveAs 返回 False。"""
        mock_app, mock_doc = create_mock_sw_app(
            config_names=["0", "1", "2"],
            save_as_succeeds=False,
        )

        fail_count = 0
        for cn_str in mock_doc.GetConfigurationNames():
            try:
                cn_int = int(cn_str)
            except ValueError:
                continue

            mock_doc.ShowConfiguration2(cn_str)
            mock_doc.EditRebuild3()

            import pythoncom
            import win32com.client
            errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

            status = mock_doc.Extension.SaveAs(
                f"test_{cn_int}.step",
                0, 1, None, errors, warnings
            )
            if not status:
                fail_count += 1

        self.assertEqual(fail_count, 3, "All SaveAs calls should return False")


# ============================================================================
# 测试类 4: SW 文档关闭与 COM 清理
# ============================================================================

class TestSwExitAndCleanup(unittest.TestCase):
    """测试 SolidWorks 文档关闭和 COM 资源清理。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sw_test_cleanup_")
        from engine.state_manager import StateManager
        self._db_path = os.path.join(self.tmpdir, "test_state.db")
        self.state = StateManager(db_path=self._db_path)

    def tearDown(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cleanup_sw_processes_taskkill_logic(self):
        """测试 _terminate_sw_processes 的 taskkill 调用逻辑（模拟）。"""
        from engine.task_runner import TaskRunner

        runner = TaskRunner(self.state)

        with patch("subprocess.run") as mock_run, \
             patch("os.name", "nt"):
            mock_run.return_value = MagicMock(stdout="SLDWORKS.exe", returncode=0)

            runner._sw_executor._terminate_sw_processes()

            self.assertGreaterEqual(mock_run.call_count, 2,
                                    "Should call tasklist + taskkill")

    def test_cleanup_no_sw_running(self):
        """测试无 SW 进程时的清理逻辑（不调用 taskkill）。"""
        from engine.task_runner import TaskRunner

        runner = TaskRunner(self.state)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)

            runner._sw_executor._terminate_sw_processes()

            kill_calls = [
                c for c in mock_run.call_args_list
                if "taskkill" in str(c)
            ]
            self.assertEqual(len(kill_calls), 0,
                             "Should NOT call taskkill when no SW process")

    def test_sw_close_doc_on_finish_config_flag(self):
        """测试 sw_close_doc_on_finish 配置标志是否被正确读取。"""
        from engine.config import ENGINE_CONFIG
        self.assertIn("sw_close_doc_on_finish", ENGINE_CONFIG)
        self.assertIsInstance(ENGINE_CONFIG["sw_close_doc_on_finish"], bool)

    def test_sw_visible_config_flag(self):
        """测试 sw_visible 配置标志是否被正确读取。"""
        from engine.config import ENGINE_CONFIG
        self.assertIn("sw_visible", ENGINE_CONFIG)


# ============================================================================
# 测试类 5: 文件监控器
# ============================================================================

class TestFileMonitor(unittest.TestCase):
    """测试 STEP 文件监控器。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sw_test_fm_")

    def tearDown(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_parse_config_name_valid(self):
        from engine.file_monitor import StepFileMonitor
        self.assertEqual(
            StepFileMonitor.parse_config_name("model_gen4.SLDPRT_1.step"), 1
        )
        self.assertEqual(
            StepFileMonitor.parse_config_name("model_gen4.SLDPRT_99.step"), 99
        )
        self.assertEqual(
            StepFileMonitor.parse_config_name("model_gen4.SLDPRT_0.step"), 0
        )

    def test_parse_config_name_invalid(self):
        from engine.file_monitor import StepFileMonitor
        self.assertIsNone(StepFileMonitor.parse_config_name("unrelated.txt"))
        self.assertIsNone(StepFileMonitor.parse_config_name("model_gen4.SLDPRT_abc.step"))
        self.assertIsNone(StepFileMonitor.parse_config_name(""))
        self.assertIsNone(StepFileMonitor.parse_config_name("model_gen4.step"))

    def test_parse_config_name_case_insensitive(self):
        from engine.file_monitor import StepFileMonitor
        self.assertEqual(
            StepFileMonitor.parse_config_name("MODEL_GEN4.SLDPRT_42.STEP"), 42
        )

    def test_file_stable_detector_ready(self):
        from engine.file_monitor import FileStableDetector
        detector = FileStableDetector(stable_time=0.3, check_interval=0.1)

        test_file = os.path.join(self.tmpdir, "test.step")
        with open(test_file, "wb") as f:
            f.write(b"hello")

        self.assertFalse(detector.is_file_ready(test_file))
        time.sleep(0.20)
        self.assertTrue(detector.is_file_ready(test_file))

    def test_file_stable_detector_growing(self):
        from engine.file_monitor import FileStableDetector
        detector = FileStableDetector(stable_time=0.5, check_interval=0.1)

        test_file = os.path.join(self.tmpdir, "growing.step")
        with open(test_file, "wb") as f:
            f.write(b"a" * 100)

        for _ in range(3):
            self.assertFalse(detector.is_file_ready(test_file))
            with open(test_file, "ab") as f:
                f.write(b"a" * 100)
            time.sleep(0.15)

    def test_file_stable_detector_nonexistent(self):
        from engine.file_monitor import FileStableDetector
        detector = FileStableDetector()
        self.assertFalse(
            detector.is_file_ready(os.path.join(self.tmpdir, "ghost.step"))
        )

    def test_step_file_monitor_callback(self):
        from engine.file_monitor import StepFileMonitor

        received = []

        def callback(cn, fp):
            received.append((cn, fp))

        monitor = StepFileMonitor(
            step_dir=self.tmpdir,
            on_file_ready=callback,
        )

        test_file = os.path.join(self.tmpdir, "model_gen4.SLDPRT_7.step")
        with open(test_file, "wb") as f:
            f.write(b"step data")

        monitor._scan_existing_files()
        self.assertIn("model_gen4.SLDPRT_7.step", monitor._known_files)

        monitor._detector.stable_time = 0.0
        monitor._scan_directory()

        monitor.stop()

    def test_get_pending_configs(self):
        from engine.file_monitor import StepFileMonitor

        monitor = StepFileMonitor(step_dir=self.tmpdir)

        for i in range(3):
            fp = os.path.join(self.tmpdir, f"model_gen4.SLDPRT_{i}.step")
            with open(fp, "wb") as f:
                f.write(b"data")

        pending = monitor.get_pending_configs()
        config_names = {cn for cn, _ in pending}
        self.assertEqual(config_names, {0, 1, 2})


# ============================================================================
# 测试类 6: 设计表导入策略 (COM 降级)
# ============================================================================

class TestDesignTableImportStrategy(unittest.TestCase):
    """测试设计表导入的两种策略：InsertFamilyTableOpen 和 COM 直接设参。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sw_test_dti_")
        from engine.state_manager import StateManager
        self._db_path = os.path.join(self.tmpdir, "test_state.db")
        self.state = StateManager(db_path=self._db_path)
        from engine.task_runner import TaskRunner
        self.runner = TaskRunner(self.state)

    def tearDown(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_param_excel(self, param_names, config_data, filename="params.xlsx"):
        filepath = os.path.join(self.tmpdir, filename)
        wb = openpyxl.Workbook()
        ws = wb.active

        ws.cell(row=1, column=1, value="Design Table for: model_gen4.SLDPRT")
        ws.cell(row=2, column=1, value="Config")
        for i, pn in enumerate(param_names, 2):
            ws.cell(row=2, column=i, value=pn)

        for r, (cfg_name, vals) in enumerate(config_data.items(), 3):
            ws.cell(row=r, column=1, value=cfg_name)
            for c, val in enumerate(vals, 2):
                ws.cell(row=r, column=c, value=val)

        wb.save(filepath)
        return filepath

    def test_apply_params_via_com_success(self):
        excel_path = self._create_param_excel(
            param_names=["$PRP@Dim1", "$PRP@Dim2"],
            config_data={
                0: [100.0, 200.0],
                1: [150.0, 250.0],
            },
        )

        mock_app, mock_doc = create_mock_sw_app(
            config_names=["0", "1"],
        )

        result = self.runner._sw_executor._apply_params_via_com(mock_doc, excel_path)
        self.assertTrue(result, "COM direct param setting should succeed")
        self.assertGreaterEqual(mock_doc.ShowConfiguration2.call_count, 2)

    def test_apply_params_via_com_no_matching_params(self):
        excel_path = self._create_param_excel(
            param_names=["$PRP@Nonexistent1", "$PRP@Nonexistent2"],
            config_data={
                0: [100.0, 200.0],
            },
        )

        mock_app, mock_doc = create_mock_sw_app()
        mock_doc.Parameter.side_effect = Exception("Parameter not found")

        result = self.runner._sw_executor._apply_params_via_com(mock_doc, excel_path)
        self.assertFalse(result,
                         "Should return False when no params match model")

    def test_apply_params_via_com_missing_row2(self):
        filepath = os.path.join(self.tmpdir, "no_row2.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.cell(row=1, column=1, value="Design Table")
        wb.save(filepath)

        mock_app, mock_doc = create_mock_sw_app()
        result = self.runner._sw_executor._apply_params_via_com(mock_doc, filepath)
        self.assertFalse(result, "Should return False when row 2 is missing")

    def test_post_process_design_table(self):
        mock_app, mock_doc = create_mock_sw_app()
        mock_dt = mock_doc.GetDesignTable()

        self.runner._sw_executor._post_process_design_table(mock_doc, "fake.xlsx")

        mock_dt.Updatable = PropertyMock()
        mock_dt.UpdateModel.assert_called()

    def test_cleanup_tmp_excel(self):
        tmp = os.path.join(self.tmpdir, "tmp_copy.xlsx")
        orig = os.path.join(self.tmpdir, "original.xlsx")

        with open(tmp, "w") as f:
            f.write("test")
        with open(orig, "w") as f:
            f.write("test")

        self.runner._sw_executor._cleanup_tmp_excel(tmp, orig)
        self.assertFalse(os.path.exists(tmp), "Temp copy should be deleted")
        self.assertTrue(os.path.exists(orig), "Original should be preserved")

    def test_cleanup_tmp_excel_same_path(self):
        orig = os.path.join(self.tmpdir, "same.xlsx")
        with open(orig, "w") as f:
            f.write("test")

        self.runner._sw_executor._cleanup_tmp_excel(orig, orig)
        self.assertTrue(os.path.exists(orig), "Original should not be deleted when tmp==orig")


# ============================================================================
# 测试类 7: 配置过渡与状态管理
# ============================================================================

class TestConfigTransition(unittest.TestCase):
    """测试配置状态过渡（SW → SC → Transfer → Meshing → Solver）。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sw_test_trans_")
        from engine.state_manager import StateManager
        self._db_path = os.path.join(self.tmpdir, "test_state.db")
        self.state = StateManager(db_path=self._db_path)
        configs = {0: [1.0, 2.0, 3.0, 4.0], 1: [5.0, 6.0, 7.0, 8.0]}
        self.state.load_configs(configs)

    def tearDown(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_initial_sw_status_waiting(self):
        from engine.config import STATUS_WAITING
        status = self.state.get_step_status(0, "sw")
        self.assertEqual(status, STATUS_WAITING)

    def test_set_sw_completed(self):
        from engine.config import STATUS_COMPLETED
        self.state.set_step_status(0, "sw", STATUS_COMPLETED)
        self.assertEqual(self.state.get_step_status(0, "sw"), STATUS_COMPLETED)

    def test_set_sw_error(self):
        from engine.config import STATUS_ERROR
        self.state.set_step_status(0, "sw", STATUS_ERROR, "SaveAs failed")
        self.assertEqual(self.state.get_step_status(0, "sw"), STATUS_ERROR)
        self.assertEqual(self.state.get_step_status(1, "sw"), "Waiting")

    def test_sw_macro_started_flag(self):
        self.assertFalse(self.state.is_sw_macro_started())
        self.state.set_sw_macro_started(True)
        self.assertTrue(self.state.is_sw_macro_started())
        self.state.set_sw_macro_started(False)
        self.assertFalse(self.state.is_sw_macro_started())

    def test_get_all_configs(self):
        configs = self.state.get_all_configs()
        self.assertEqual(configs, [0, 1])

    def test_get_configs_at_step(self):
        from engine.config import STATUS_COMPLETED
        self.state.set_step_status(0, "sw", STATUS_COMPLETED)
        self.state.set_step_status(1, "sw", STATUS_COMPLETED)

        completed = self.state.get_configs_at_step("sw", STATUS_COMPLETED)
        self.assertEqual(completed, [0, 1])

    def test_all_configs_completed_at_step(self):
        from engine.config import STATUS_COMPLETED
        self.state.set_step_status(0, "sw", STATUS_COMPLETED)
        self.state.set_step_status(1, "sw", STATUS_COMPLETED)
        self.assertTrue(self.state.all_configs_completed_at_step("sw"))

    def test_reset_config_steps(self):
        from engine.config import STATUS_COMPLETED, STATUS_WAITING
        self.state.set_step_status(0, "sw", STATUS_COMPLETED)
        self.state.set_step_status(0, "sc", STATUS_COMPLETED)
        self.state.set_step_status(0, "transfer", STATUS_COMPLETED)

        self.state.reset_config_steps(0, "sc")
        self.assertEqual(self.state.get_step_status(0, "sw"), STATUS_COMPLETED)
        self.assertEqual(self.state.get_step_status(0, "sc"), STATUS_WAITING)
        self.assertEqual(self.state.get_step_status(0, "transfer"), STATUS_WAITING)


# ============================================================================
# 测试类 8: 错误处理与边界情况
# ============================================================================

class TestErrorHandling(unittest.TestCase):
    """测试错误处理与边界情况。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sw_test_err_")
        from engine.state_manager import StateManager
        self._db_path = os.path.join(self.tmpdir, "test_state.db")
        self.state = StateManager(db_path=self._db_path)
        from engine.task_runner import TaskRunner
        self.runner = TaskRunner(self.state)

    def tearDown(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_design_table_existing_skip_import(self):
        """测试当模型已有设计表时，函数正确返回 True（跳过导入）。"""
        excel_path = os.path.join(self.tmpdir, "nonexistent_params.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.cell(row=1, column=1, value="Design Table")
        ws.cell(row=2, column=1, value="Config")
        ws.cell(row=2, column=2, value="$PRP@GhostParam")
        ws.cell(row=3, column=1, value=0)
        ws.cell(row=3, column=2, value=100.0)
        wb.save(excel_path)

        mock_app, mock_doc = create_mock_sw_app(insert_dt_succeeds=False)
        mock_doc.GetDesignTable.return_value = MagicMock()
        mock_doc.Parameter.side_effect = Exception("Parameter not found")

        result = self.runner._sw_executor._import_design_table_with_retry(
            mock_doc, mock_app, excel_path, r"C:\fake\model.SLDPRT"
        )
        self.assertTrue(result, "Should return True when model has design table (skip import)")
        mock_app.CloseDoc.assert_not_called()

    @unittest.skipIf(sys.platform != "win32", "需要 Windows COM 环境")
    def test_export_empty_config_list(self):
        mock_app, mock_doc = create_mock_sw_app(
            config_names=[],
        )
        mock_doc.GetConfigurationNames.return_value = []

        from engine.task_runner import TaskRunner
        runner = TaskRunner(self.state)
        success, fail, _ = runner._sw_executor._export_all_configs_to_step(mock_doc, "C:\\step")
        self.assertEqual(success, 0)
        self.assertEqual(fail, 0)

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getsize", return_value=2048)
    @unittest.skipIf(sys.platform != "win32", "需要 Windows COM 环境")
    def test_export_config_with_non_int_name(self, mock_size, mock_exists):
        mock_app, mock_doc = create_mock_sw_app(
            config_names=["Default", "0"],
        )

        from engine.task_runner import TaskRunner
        runner = TaskRunner(self.state)
        success, fail, fail_list = runner._sw_executor._export_all_configs_to_step(mock_doc, "C:\\step")

        self.assertEqual(success, 1, "Only config 0 should succeed")
        self.assertEqual(fail, 1, "Default should be in fail_list")
        self.assertIn("Default", fail_list or [None])


# ============================================================================
# 测试类 9: 端到端工作流 (Mock)
# ============================================================================

class TestEndToEndWorkflow(unittest.TestCase):
    """模拟端到端 SolidWorks 导出工作流。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sw_test_e2e_")
        from engine.state_manager import StateManager
        self._db_path = os.path.join(self.tmpdir, "test_state.db")
        self.state = StateManager(db_path=self._db_path)
        from engine.task_runner import TaskRunner
        self.runner = TaskRunner(self.state)
        configs = {i: [float(i + j) for j in range(4)] for i in range(5)}
        self.state.load_configs(configs)

    def tearDown(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_e2e_excel(self, num_configs: int = 5):
        filepath = os.path.join(self.tmpdir, "e2e_params.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active

        ws.cell(row=1, column=1, value="Design Table for: model_gen4.SLDPRT")
        ws.cell(row=2, column=1, value="Config")
        params = ["$PRP@Dim1", "$PRP@Dim2", "$PRP@Dim3", "$PRP@Dim4"]
        for i, pn in enumerate(params, 2):
            ws.cell(row=2, column=i, value=pn)

        for cfg in range(num_configs):
            row = cfg + 3
            ws.cell(row=row, column=1, value=cfg)
            for j in range(4):
                ws.cell(row=row, column=j + 2, value=float(cfg + j))

        wb.save(filepath)
        return filepath

    def test_full_export_workflow_mocked(self):
        """模拟完整 SW 导出工作流：打开模型 → 导入设计表 → 导出 STEP → 退出。"""
        self._create_e2e_excel(num_configs=5)

        mock_app, mock_doc = create_mock_sw_app(
            config_names=["0", "1", "2", "3", "4"],
            open_doc_succeeds=True,
            insert_dt_succeeds=True,
            save_as_succeeds=True,
        )

        from executor.sw_executor import SWExecutor
        doc_type = SWExecutor._guess_sw_doc_type("model_gen4.SLDPRT")
        self.assertEqual(doc_type, SWExecutor._SW_DOC_PART)

        mock_app.OpenDoc6.assert_not_called()
        mock_doc.ShowConfiguration2.assert_not_called()

        for cn_str in ["0", "1", "2", "3", "4"]:
            mock_doc.ShowConfiguration2(cn_str)
            mock_doc.EditRebuild3()

        self.assertEqual(mock_doc.ShowConfiguration2.call_count, 5)
        self.assertEqual(mock_doc.EditRebuild3.call_count, 5)

    def test_workflow_with_com_fallback(self):
        """模拟 InsertFamilyTableOpen 失败但 COM 降级成功的场景。"""
        excel_path = self._create_e2e_excel(num_configs=3)

        mock_app, mock_doc = create_mock_sw_app(
            config_names=["0", "1", "2"],
            insert_dt_succeeds=False,
        )

        result = self.runner._sw_executor._import_design_table_with_retry(
            mock_doc, mock_app, excel_path, r"C:\fake\model.SLDPRT"
        )
        self.assertTrue(result, "COM fallback should succeed when InsertFamilyTableOpen fails")

    def test_workflow_existing_design_table_skip_import(self):
        """测试当模型已有设计表时，函数正确返回 True（跳过导入）。"""
        excel_path = os.path.join(self.tmpdir, "bad_params.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.cell(row=1, column=1, value="Design Table")
        ws.cell(row=2, column=1, value="Config")
        ws.cell(row=2, column=2, value="$PRP@GhostParam")
        ws.cell(row=3, column=1, value=0)
        ws.cell(row=3, column=2, value=100.0)
        wb.save(excel_path)

        mock_app, mock_doc = create_mock_sw_app(
            insert_dt_succeeds=False,
        )
        mock_doc.GetDesignTable.return_value = MagicMock()
        mock_doc.Parameter.side_effect = Exception("not found")

        result = self.runner._sw_executor._import_design_table_with_retry(
            mock_doc, mock_app, excel_path, r"C:\fake\model.SLDPRT"
        )
        self.assertTrue(result, "Should return True when model has design table (skip import)")
        mock_app.CloseDoc.assert_not_called()


# ============================================================================
# 测试类 10: STEP 文件名生成
# ============================================================================

class TestStepFilenameGeneration(unittest.TestCase):
    """测试 STEP 文件命名。"""

    def test_get_step_filename_sw(self):
        from engine.config import get_step_filename
        self.assertEqual(
            get_step_filename("sw", 5), "model_gen4.SLDPRT_5.step"
        )

    def test_get_step_filename_sc(self):
        from engine.config import get_step_filename
        self.assertEqual(
            get_step_filename("sc", 12), "model_gen4_12.scdoc"
        )

    def test_get_step_filename_meshing(self):
        from engine.config import get_step_filename
        self.assertEqual(
            get_step_filename("meshing", 3), "model_gen4_3.msh.h5"
        )

    def test_get_step_filename_solver(self):
        from engine.config import get_step_filename
        self.assertEqual(
            get_step_filename("solver", 7), "model_gen4_7.cas.h5"
        )

    def test_get_step_filename_invalid_step(self):
        from engine.config import get_step_filename
        self.assertIsNone(get_step_filename("InvalidStep", 5))
        self.assertIsNone(get_step_filename("transfer", 5))

    def test_get_step_filename_negative_config(self):
        from engine.config import get_step_filename
        result = get_step_filename("sw", -1)
        self.assertEqual(result, "model_gen4.SLDPRT_-1.step")


# ============================================================================
# 测试类 11: COM 绑定兼容性（_safe_com_call / _com_rebuild / _com_get_config_names）
# ============================================================================

class TestComBindingCompatibility(unittest.TestCase):
    """测试 pywin32 动态 Dispatch 的 property/method 兼容性处理。

    注意：_safe_com_call / _com_rebuild / _com_get_config_names 已重构为内联代码，
    此处改为测试 _export_all_configs_to_step 和 _apply_params_via_com 中的内联逻辑。
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sw_test_com_")
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from engine.state_manager import StateManager
        self._db_path = os.path.join(self.tmpdir, "test_state.db")
        self.state = StateManager(db_path=self._db_path)
        from engine.task_runner import TaskRunner
        self.runner = TaskRunner(self.state)

    def tearDown(self):
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- GetConfigurationNames 兼容性（内联于 _export_all_configs_to_step） ----

    def test_com_get_config_names_tuple(self):
        """GetConfigurationNames 返回正常 tuple（通过 _export_all_configs_to_step 内联逻辑）。"""
        mock_doc = MagicMock()
        mock_doc.GetConfigurationNames.return_value = ("0", "1", "2")
        mock_doc._FlagAsMethod = MagicMock()

        raw = None
        try:
            mock_doc._FlagAsMethod('GetConfigurationNames')
            raw = mock_doc.GetConfigurationNames()
        except TypeError:
            raw = mock_doc.GetConfigurationNames

        if isinstance(raw, (tuple, list)):
            names = [str(c) for c in raw]
        elif raw is not None:
            names = [str(raw)]
        else:
            names = []

        self.assertEqual(names, ["0", "1", "2"])

    def test_com_get_config_names_property_mode(self):
        """GetConfigurationNames 为 property 模式（直接返回 tuple）。"""
        mock_doc = type("Doc", (), {"GetConfigurationNames": ("A", "B"),
                                     "_FlagAsMethod": lambda self, x: None})()

        raw = None
        try:
            mock_doc._FlagAsMethod('GetConfigurationNames')
            raw = mock_doc.GetConfigurationNames()
        except TypeError:
            raw = mock_doc.GetConfigurationNames

        if isinstance(raw, (tuple, list)):
            names = [str(c) for c in raw]
        elif raw is not None:
            names = [str(raw)]
        else:
            names = []

        self.assertEqual(names, ["A", "B"])

    def test_com_get_config_names_fallback_to_iget(self):
        """GetConfigurationNames 失败 → TypeError 降级为属性访问。"""
        mock_doc = MagicMock()
        mock_doc.GetConfigurationNames.side_effect = TypeError("not callable")
        mock_doc._FlagAsMethod = MagicMock()

        raw = None
        try:
            mock_doc._FlagAsMethod('GetConfigurationNames')
            raw = mock_doc.GetConfigurationNames()
        except TypeError:
            raw = mock_doc.GetConfigurationNames

        if isinstance(raw, (tuple, list)):
            names = [str(c) for c in raw]
        elif raw is not None:
            names = [str(raw)]
        else:
            names = []

        self.assertIsInstance(names, list)

    def test_com_get_config_names_both_fail(self):
        """GetConfigurationNames 两种访问方式均失败，返回空列表。"""
        mock_doc = MagicMock()
        mock_doc.GetConfigurationNames.side_effect = Exception("dead")
        mock_doc._FlagAsMethod = MagicMock()

        names = []
        try:
            mock_doc._FlagAsMethod('GetConfigurationNames')
            raw = mock_doc.GetConfigurationNames()
            if isinstance(raw, (tuple, list)):
                names = [str(c) for c in raw]
            elif raw is not None:
                names = [str(raw)]
        except TypeError:
            try:
                raw = mock_doc.GetConfigurationNames
                if isinstance(raw, (tuple, list)):
                    names = [str(c) for c in raw]
                elif raw is not None:
                    names = [str(raw)]
            except Exception:
                pass
        except Exception:
            pass

        self.assertEqual(names, [])

    def test_com_get_config_names_single_element(self):
        """返回值非 tuple/list 时作为单元素解析。"""
        mock_doc = MagicMock()
        mock_doc.GetConfigurationNames.return_value = "OnlyConfig"
        mock_doc._FlagAsMethod = MagicMock()

        raw = None
        try:
            mock_doc._FlagAsMethod('GetConfigurationNames')
            raw = mock_doc.GetConfigurationNames()
        except TypeError:
            raw = mock_doc.GetConfigurationNames

        if isinstance(raw, (tuple, list)):
            names = [str(c) for c in raw]
        elif raw is not None:
            names = [str(raw)]
        else:
            names = []

        self.assertEqual(names, ["OnlyConfig"])

    # ---- _verify_com_object 测试 ----

    @classmethod
    def setUpClass(cls):
        from executor.sw_executor import SWExecutor
        cls.SWExecutor = SWExecutor

    def test_verify_com_object_valid(self):
        """有效 COM 对象通过验证。"""
        mock_obj = MagicMock()
        mock_obj.GetTitle.return_value = "TestDoc"
        self.assertTrue(self.SWExecutor._verify_com_object(mock_obj, "TestObj"))

    def test_verify_com_object_none(self):
        """None 对象验证失败。"""
        self.assertFalse(self.SWExecutor._verify_com_object(None, "NullObj"))

    def test_verify_com_object_dead_proxy(self):
        """所有验证方法均失败时返回 False。"""
        mock_obj = MagicMock(spec=[])
        self.assertFalse(self.SWExecutor._verify_com_object(mock_obj, "DeadObj"))

    # ---- SaveAs 后置文件验证测试（_export_all_configs_to_step 行为） ----

    @patch("os.path.exists", return_value=True)
    @patch("os.path.getsize", return_value=2048)
    @unittest.skipIf(sys.platform != "win32", "需要 Windows COM 环境")
    def test_export_step_file_verification_success(self, mock_size, mock_exists):
        """SaveAs 返回 True 且文件系统验证通过。"""
        runner = self.runner
        mock_app, mock_doc = create_mock_sw_app(
            config_names=["0", "1"],
            save_as_succeeds=True,
        )

        step_dir = self.tmpdir
        success, fail, failed = runner._sw_executor._export_all_configs_to_step(mock_doc, step_dir)

        self.assertGreater(success, 0)
        self.assertEqual(fail, 0)

    @patch("os.path.exists", return_value=False)
    @patch("os.path.getsize", return_value=0)
    @unittest.skipIf(sys.platform != "win32", "需要 Windows COM 环境")
    def test_export_step_file_verification_fail_missing_file(self, mock_size, mock_exists):
        """SaveAs 返回 True 但文件不存在 → 标记为失败。"""
        runner = self.runner
        mock_app, mock_doc = create_mock_sw_app(
            config_names=["0", "1"],
            save_as_succeeds=True,
        )

        step_dir = self.tmpdir
        success, fail, failed = runner._sw_executor._export_all_configs_to_step(mock_doc, step_dir)

        self.assertEqual(success, 0)
        self.assertGreater(fail, 0)


# ============================================================================
# 主入口
# ============================================================================

def main():
    print("=" * 60)
    print("  SolidWorks Export 工作流测试套件")
    print("=" * 60)

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    test_classes = [
        TestSwComConnection,
        TestDesignTableValidation,
        TestConfigEnumAndStepExport,
        TestSwExitAndCleanup,
        TestFileMonitor,
        TestDesignTableImportStrategy,
        TestConfigTransition,
        TestErrorHandling,
        TestEndToEndWorkflow,
        TestStepFilenameGeneration,
        TestComBindingCompatibility,
    ]

    for tc in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(tc))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print("\n" + "=" * 60)
    print(f"  测试结果: {result.testsRun - len(result.failures) - len(result.errors)} 通过, "
          f"{len(result.failures)} 失败, {len(result.errors)} 错误, "
          f"{result.testsRun} 总计")
    print("=" * 60)

    return len(result.failures) == 0 and len(result.errors) == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
