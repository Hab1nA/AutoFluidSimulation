import os
import unittest


class TestSwStepNaming(unittest.TestCase):
    def test_get_step_filename(self):
        from engine.config import get_step_filename

        self.assertEqual(get_step_filename("SW", 5), "model_gen4.SLDPRT_5.step")
        self.assertEqual(get_step_filename("SC", 12), "model_gen4_12.scdoc")

    def test_step_file_monitor_parse_config(self):
        from engine.file_monitor import StepFileMonitor

        self.assertEqual(StepFileMonitor.parse_config_name("model_gen4.SLDPRT_1.step"), 1)
        self.assertEqual(StepFileMonitor.parse_config_name("MODEL_GEN4.SLDPRT_99.STEP"), 99)
        self.assertIsNone(StepFileMonitor.parse_config_name("unrelated.step"))

    def test_guess_sw_doc_type(self):
        from engine.task_runner import TaskRunner

        self.assertEqual(TaskRunner._guess_sw_doc_type(r"C:\a\b\part.SLDPRT"), TaskRunner._SW_DOC_PART)
        self.assertEqual(TaskRunner._guess_sw_doc_type(r"C:\a\b\asm.SLDASM"), TaskRunner._SW_DOC_ASSEMBLY)


class TestEnvOverrides(unittest.TestCase):
    def test_env_override_local_paths(self):
        import importlib
        import engine.config as config

        # ensure default is not equal to our override value
        override_val = r"C:\override\model.SLDPRT"

        old = os.environ.get("AUTOFLUID_SW_MODEL")
        try:
            os.environ["AUTOFLUID_SW_MODEL"] = override_val
            # reload to apply env override at module import time
            importlib.reload(config)
            self.assertEqual(config.LOCAL_PATHS["sw_model"], override_val)
        finally:
            if old is None:
                os.environ.pop("AUTOFLUID_SW_MODEL", None)
            else:
                os.environ["AUTOFLUID_SW_MODEL"] = old
            importlib.reload(config)

