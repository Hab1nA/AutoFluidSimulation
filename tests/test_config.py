"""
===============================================================================
配置系统单元测试 (M1)

覆盖：
- _env_override: 环境变量覆盖逻辑
- _expand_dict_env_vars: 字典批量环境变量展开
- _apply_env_overrides: 环境变量覆盖 LOCAL_PATHS/REMOTE_CONFIG
- LOCAL_PATHS / REMOTE_CONFIG / IPC_CONFIG: 默认值完整性
- get_step_filename: 全步骤文件名生成
- ensure_directories: 目录自动创建
- validate_config: 配置验证（5 条警告路径）
- compute_config_fingerprint: 指纹确定性与灵敏度
- get_db_path_for_fingerprint: 数据库路径格式
- reload_config_from_toml: TOML 加载与合并（全分支）
- load_toml_config: TOML 解析
- _expand_env_vars: 环境变量展开
- STEP_FILE_PATTERNS: 文件名匹配
- STEP_NAMES / ALL_STATUSES / STEP_INDEX: 常量完整性
===============================================================================
"""
from __future__ import annotations

import re


# ====================================================================
# _env_override 测试
# ====================================================================

class TestEnvOverride:
    """验证 _env_override 的三路逻辑。"""

    def test_returns_default_when_env_not_set(self, monkeypatch):
        monkeypatch.delenv("AF_TEST_KEY_NOT_EXIST", raising=False)
        from engine.config import _env_override
        assert _env_override("AF_TEST_KEY_NOT_EXIST", "fallback") == "fallback"

    def test_returns_env_value_when_set(self, monkeypatch):
        monkeypatch.setenv("AF_TEST_KEY_EXISTS", "custom_value")
        from engine.config import _env_override
        assert _env_override("AF_TEST_KEY_EXISTS", "fallback") == "custom_value"

    def test_returns_default_when_env_empty(self, monkeypatch):
        monkeypatch.setenv("AF_TEST_KEY_EMPTY", "")
        from engine.config import _env_override
        assert _env_override("AF_TEST_KEY_EMPTY", "fallback") == "fallback"

    def test_returns_default_when_env_whitespace(self, monkeypatch):
        monkeypatch.setenv("AF_TEST_KEY_SPACE", "   \t  ")
        from engine.config import _env_override
        assert _env_override("AF_TEST_KEY_SPACE", "fallback") == "fallback"


# ====================================================================
# LOCAL_PATHS 完整性
# ====================================================================

class TestLocalPathsCompleteness:
    """验证 LOCAL_PATHS 包含所有必需键。"""

    _REQUIRED_KEYS = {
        "sw_exe", "sw_model", "excel", "step_dir",
        "sc_exe", "sc_script", "sc_bridge", "scdoc_dir",
        "log_dir", "data_dir", "remote_scripts_dir",
    }

    def test_all_required_keys_present(self):
        from engine.config import LOCAL_PATHS
        missing = self._REQUIRED_KEYS - set(LOCAL_PATHS.keys())
        assert not missing, f"LOCAL_PATHS 缺失以下键: {sorted(missing)}"

    def test_all_values_are_non_empty_strings(self):
        from engine.config import LOCAL_PATHS
        for key in self._REQUIRED_KEYS:
            val = LOCAL_PATHS.get(key)
            assert isinstance(val, str) and val, (
                f"LOCAL_PATHS['{key}'] 应为非空字符串，实际: {val!r}"
            )


# ====================================================================
# get_step_filename 测试
# ====================================================================

class TestGetStepFilename:
    """验证所有步骤的文件名生成。"""

    def test_sw_filename(self):
        from engine.config import get_step_filename
        assert get_step_filename("sw", 5) == "model_gen4.SLDPRT_5.step"

    def test_sc_filename(self):
        from engine.config import get_step_filename
        assert get_step_filename("sc", 12) == "model_gen4_12.scdoc"

    def test_transfer_returns_none(self):
        from engine.config import get_step_filename
        assert get_step_filename("transfer", 1) is None

    def test_meshing_filename(self):
        from engine.config import get_step_filename
        assert get_step_filename("meshing", 3) == "model_gen4_3.msh.h5"

    def test_solver_filename(self):
        from engine.config import get_step_filename
        assert get_step_filename("solver", 7) == "model_gen4_7.cas.h5"

    def test_unknown_step_returns_none(self):
        from engine.config import get_step_filename
        assert get_step_filename("UnknownStep", 1) is None

    def test_zero_config_name(self):
        from engine.config import get_step_filename
        assert get_step_filename("sw", 0) == "model_gen4.SLDPRT_0.step"

    def test_large_config_name(self):
        from engine.config import get_step_filename
        assert get_step_filename("sc", 9999) == "model_gen4_9999.scdoc"


# ====================================================================
# STEP_FILE_PATTERNS 正则匹配
# ====================================================================

class TestStepFilePatterns:
    """验证 STEP_FILE_PATTERNS 中的文件名格式可被正确匹配。"""

    def test_sw_pattern_match(self):
        from engine.config import STEP_FILE_PATTERNS
        pattern = STEP_FILE_PATTERNS["sw"]
        assert pattern is not None
        filename = pattern.format(config=5)
        assert filename == "model_gen4.SLDPRT_5.step"

    def test_sc_pattern_match(self):
        from engine.config import STEP_FILE_PATTERNS
        pattern = STEP_FILE_PATTERNS["sc"]
        assert pattern is not None
        filename = pattern.format(config=12)
        assert filename == "model_gen4_12.scdoc"

    def test_transfer_pattern_is_none(self):
        from engine.config import STEP_FILE_PATTERNS
        assert STEP_FILE_PATTERNS["transfer"] is None

    def test_meshing_pattern(self):
        from engine.config import STEP_FILE_PATTERNS
        pattern = STEP_FILE_PATTERNS["meshing"]
        assert pattern is not None
        filename = pattern.format(config=3)
        assert filename == "model_gen4_3.msh.h5"

    def test_solver_pattern(self):
        from engine.config import STEP_FILE_PATTERNS
        pattern = STEP_FILE_PATTERNS["solver"]
        assert pattern is not None
        filename = pattern.format(config=7)
        assert filename == "model_gen4_7.cas.h5"

    def test_solver_data_pattern(self):
        from engine.config import STEP_FILE_PATTERNS
        pattern = STEP_FILE_PATTERNS["solverdata"]
        assert pattern is not None
        filename = pattern.format(config=7)
        assert filename == "model_gen4_7.dat.h5"


# ====================================================================
# STEP_NAMES / ALL_STATUSES / STEP_INDEX 常量
# ====================================================================

class TestConstants:
    """验证常量完整性和一致性。"""

    def test_step_names_complete(self):
        from engine.config import STEP_NAMES
        expected = ["sw", "sc", "transfer", "meshing", "solver"]
        assert STEP_NAMES == expected

    def test_all_statuses_complete(self):
        from engine.config import ALL_STATUSES
        expected = ["Waiting", "Running", "Paused", "Retrying", "Completed", "Error"]
        assert ALL_STATUSES == expected

    def test_step_index_consistent_with_step_names(self):
        from engine.config import STEP_NAMES, STEP_INDEX
        for i, name in enumerate(STEP_NAMES):
            assert STEP_INDEX[name] == i

    def test_status_constants_are_strings(self):
        from engine.config import (
            STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED,
            STATUS_RETRYING, STATUS_COMPLETED, STATUS_ERROR,
        )
        for s in [STATUS_WAITING, STATUS_RUNNING, STATUS_PAUSED,
                  STATUS_RETRYING, STATUS_COMPLETED, STATUS_ERROR]:
            assert isinstance(s, str) and s


# ====================================================================
# ensure_directories 测试
# ====================================================================

class TestEnsureDirectories:
    """验证 ensure_directories 创建必要目录。"""

    def test_creates_missing_directories(self, tmp_path, monkeypatch):
        from engine.config import LOCAL_PATHS

        # 临时替换路径到 tmp_path 下
        original = {k: LOCAL_PATHS[k] for k in ["step_dir", "scdoc_dir", "log_dir", "data_dir"]}
        try:
            for key in ["step_dir", "scdoc_dir", "log_dir", "data_dir"]:
                monkeypatch.setitem(LOCAL_PATHS, key, str(tmp_path / key))

            from engine.config import ensure_directories
            ensure_directories()

            for key in ["step_dir", "scdoc_dir", "log_dir", "data_dir"]:
                assert (tmp_path / key).is_dir(), f"目录 {key} 未创建"
        finally:
            LOCAL_PATHS.update(original)

    def test_existing_directories_no_error(self, tmp_path, monkeypatch):
        from engine.config import LOCAL_PATHS

        original = {k: LOCAL_PATHS[k] for k in ["step_dir", "scdoc_dir", "log_dir", "data_dir"]}
        try:
            for key in ["step_dir", "scdoc_dir", "log_dir", "data_dir"]:
                d = tmp_path / key
                d.mkdir()
                monkeypatch.setitem(LOCAL_PATHS, key, str(d))

            from engine.config import ensure_directories
            ensure_directories()  # 不应抛异常
        finally:
            LOCAL_PATHS.update(original)


# ====================================================================
# validate_config 测试
# ====================================================================

class TestValidateConfig:
    """验证 validate_config 返回正确的警告。"""

    def test_returns_empty_when_all_valid(self, tmp_path, monkeypatch):
        from engine.config import LOCAL_PATHS, REMOTE_CONFIG

        # 创建假文件使路径"存在"
        sw_model = tmp_path / "model.SLDPRT"
        sw_model.touch()
        excel = tmp_path / "table.xlsx"
        excel.touch()
        sc_exe = tmp_path / "SpaceClaim.exe"
        sc_exe.touch()
        sw_exe = tmp_path / "SLDWORKS.exe"
        sw_exe.touch()

        orig_local = {k: LOCAL_PATHS[k] for k in ["sw_model", "excel", "sc_exe", "sw_exe"]}
        orig_password = REMOTE_CONFIG["password"]
        try:
            monkeypatch.setitem(LOCAL_PATHS, "sw_model", str(sw_model))
            monkeypatch.setitem(LOCAL_PATHS, "excel", str(excel))
            monkeypatch.setitem(LOCAL_PATHS, "sc_exe", str(sc_exe))
            monkeypatch.setitem(LOCAL_PATHS, "sw_exe", str(sw_exe))
            monkeypatch.setitem(REMOTE_CONFIG, "password", "valid_pass")

            from engine.config import validate_config
            warnings = validate_config()
            assert warnings == []
        finally:
            LOCAL_PATHS.update(orig_local)
            REMOTE_CONFIG["password"] = orig_password

    def test_warns_missing_password(self, tmp_path, monkeypatch):
        from engine.config import LOCAL_PATHS, REMOTE_CONFIG

        # 创建所有文件
        for key, name in [("sw_model", "m.SLDPRT"), ("excel", "t.xlsx"),
                          ("sc_exe", "sc.exe"), ("sw_exe", "sw.exe")]:
            f = tmp_path / name
            f.touch()
            monkeypatch.setitem(LOCAL_PATHS, key, str(f))
        monkeypatch.setitem(REMOTE_CONFIG, "password", "")

        from engine.config import validate_config
        warnings = validate_config()
        assert any("SSH 密码" in w for w in warnings)

    def test_warns_missing_sw_model(self, tmp_path, monkeypatch):
        from engine.config import LOCAL_PATHS, REMOTE_CONFIG

        monkeypatch.setitem(LOCAL_PATHS, "sw_model", str(tmp_path / "nonexistent.SLDPRT"))
        for key, name in [("excel", "t.xlsx"), ("sc_exe", "sc.exe"), ("sw_exe", "sw.exe")]:
            f = tmp_path / name
            f.touch()
            monkeypatch.setitem(LOCAL_PATHS, key, str(f))
        monkeypatch.setitem(REMOTE_CONFIG, "password", "pass")

        from engine.config import validate_config
        warnings = validate_config()
        assert any("SW 模型文件不存在" in w for w in warnings)

    def test_warns_missing_excel(self, tmp_path, monkeypatch):
        from engine.config import LOCAL_PATHS, REMOTE_CONFIG

        monkeypatch.setitem(LOCAL_PATHS, "excel", str(tmp_path / "nonexistent.xlsx"))
        for key, name in [("sw_model", "m.SLDPRT"), ("sc_exe", "sc.exe"), ("sw_exe", "sw.exe")]:
            f = tmp_path / name
            f.touch()
            monkeypatch.setitem(LOCAL_PATHS, key, str(f))
        monkeypatch.setitem(REMOTE_CONFIG, "password", "pass")

        from engine.config import validate_config
        warnings = validate_config()
        assert any("Excel 参数表不存在" in w for w in warnings)

    def test_warns_missing_sc_exe(self, tmp_path, monkeypatch):
        from engine.config import LOCAL_PATHS, REMOTE_CONFIG

        monkeypatch.setitem(LOCAL_PATHS, "sc_exe", str(tmp_path / "nonexistent.exe"))
        for key, name in [("sw_model", "m.SLDPRT"), ("excel", "t.xlsx"), ("sw_exe", "sw.exe")]:
            f = tmp_path / name
            f.touch()
            monkeypatch.setitem(LOCAL_PATHS, key, str(f))
        monkeypatch.setitem(REMOTE_CONFIG, "password", "pass")

        from engine.config import validate_config
        warnings = validate_config()
        assert any("SpaceClaim 可执行文件不存在" in w for w in warnings)

    def test_warns_missing_sw_exe(self, tmp_path, monkeypatch):
        from engine.config import LOCAL_PATHS, REMOTE_CONFIG

        monkeypatch.setitem(LOCAL_PATHS, "sw_exe", str(tmp_path / "nonexistent.exe"))
        for key, name in [("sw_model", "m.SLDPRT"), ("excel", "t.xlsx"), ("sc_exe", "sc.exe")]:
            f = tmp_path / name
            f.touch()
            monkeypatch.setitem(LOCAL_PATHS, key, str(f))
        monkeypatch.setitem(REMOTE_CONFIG, "password", "pass")

        from engine.config import validate_config
        warnings = validate_config()
        assert any("SolidWorks 可执行文件不存在" in w for w in warnings)


# ====================================================================
# compute_config_fingerprint 测试
# ====================================================================

class TestConfigFingerprint:
    """验证配置指纹的确定性和灵敏度。"""

    def test_deterministic(self):
        from engine.config_fingerprint import compute_config_fingerprint
        configs = {1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]}
        fp1 = compute_config_fingerprint(configs)
        fp2 = compute_config_fingerprint(configs)
        assert fp1 == fp2

    def test_order_independent(self):
        from engine.config_fingerprint import compute_config_fingerprint
        configs_a = {1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]}
        configs_b = {2: [5.0, 6.0, 7.0, 8.0], 1: [1.0, 2.0, 3.0, 4.0]}
        assert compute_config_fingerprint(configs_a) == compute_config_fingerprint(configs_b)

    def test_changes_when_param_changes(self):
        from engine.config_fingerprint import compute_config_fingerprint
        configs_a = {1: [1.0, 2.0, 3.0, 4.0]}
        configs_b = {1: [1.0, 2.0, 3.0, 4.1]}
        assert compute_config_fingerprint(configs_a) != compute_config_fingerprint(configs_b)

    def test_changes_when_config_added(self):
        from engine.config_fingerprint import compute_config_fingerprint
        configs_a = {1: [1.0, 2.0, 3.0, 4.0]}
        configs_b = {1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]}
        assert compute_config_fingerprint(configs_a) != compute_config_fingerprint(configs_b)

    def test_returns_hex_string(self):
        from engine.config_fingerprint import compute_config_fingerprint
        fp = compute_config_fingerprint({1: [1.0, 2.0, 3.0, 4.0]})
        assert len(fp) == 8
        assert re.fullmatch(r'[0-9a-f]{8}', fp)

    def test_empty_configs(self):
        from engine.config_fingerprint import compute_config_fingerprint
        fp = compute_config_fingerprint({})
        assert len(fp) == 8


# ====================================================================
# get_db_path_for_fingerprint 测试
# ====================================================================

class TestDbPathForFingerprint:
    """验证数据库路径生成。"""

    def test_contains_fingerprint(self, monkeypatch):
        from engine.config_fingerprint import get_db_path_for_fingerprint
        from engine.config import LOCAL_PATHS
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", "/tmp/data")
        path = get_db_path_for_fingerprint("abcd1234")
        assert "pipeline_state_abcd1234.db" in path

    def test_under_data_dir(self, monkeypatch):
        from engine.config_fingerprint import get_db_path_for_fingerprint
        from engine.config import LOCAL_PATHS
        monkeypatch.setitem(LOCAL_PATHS, "data_dir", "/tmp/data")
        path = get_db_path_for_fingerprint("abcd1234")
        assert path.startswith("/tmp/data")


# ====================================================================
# load_toml_config 测试
# ====================================================================

class TestLoadTomlConfig:
    """验证 TOML 配置文件加载。"""

    def test_returns_empty_when_file_not_found(self, tmp_path):
        from engine.config import load_toml_config
        result = load_toml_config(str(tmp_path / "nonexistent.toml"))
        assert result == {}

    def test_parses_valid_toml(self, tmp_path):
        from engine.config import load_toml_config
        toml_file = tmp_path / "test.toml"
        toml_file.write_text(
            '[local_paths]\nstep_dir = "C:\\\\test\\\\step"\n',
            encoding="utf-8",
        )
        result = load_toml_config(str(toml_file))
        assert "local_paths" in result
        assert result["local_paths"]["step_dir"] == "C:\\test\\step"

    def test_returns_empty_on_invalid_toml(self, tmp_path):
        from engine.config import load_toml_config
        toml_file = tmp_path / "bad.toml"
        toml_file.write_text("this is not valid toml [[[[", encoding="utf-8")
        result = load_toml_config(str(toml_file))
        assert result == {}


# ====================================================================
# _expand_env_vars 测试
# ====================================================================

class TestExpandEnvVars:
    """验证环境变量展开。"""

    def test_expands_existing_var(self, monkeypatch):
        monkeypatch.setenv("AF_EXPAND_TEST", "expanded_value")
        from engine.config import _expand_env_vars
        assert _expand_env_vars("${AF_EXPAND_TEST}") == "expanded_value"

    def test_preserves_undefined_var(self):
        from engine.config import _expand_env_vars
        assert _expand_env_vars("${AF_UNDEF_VAR_XYZ}") == "${AF_UNDEF_VAR_XYZ}"

    def test_non_string_passthrough(self):
        from engine.config import _expand_env_vars
        assert _expand_env_vars(42) == 42
        assert _expand_env_vars(None) is None
        assert _expand_env_vars([1, 2]) == [1, 2]

    def test_partial_expansion(self, monkeypatch):
        monkeypatch.setenv("AF_PART_A", "hello")
        from engine.config import _expand_env_vars
        result = _expand_env_vars("${AF_PART_A}_${AF_UNDEF_PART_B}")
        assert result == "hello_${AF_UNDEF_PART_B}"

    def test_empty_string_passthrough(self):
        from engine.config import _expand_env_vars
        assert _expand_env_vars("") == ""

    def test_plain_text_no_vars(self):
        from engine.config import _expand_env_vars
        assert _expand_env_vars("just text") == "just text"


class TestExpandDictEnvVars:
    """验证 _expand_dict_env_vars 批量展开。"""

    def test_expands_all_values(self, monkeypatch):
        monkeypatch.setenv("AF_DICT_HOST", "192.168.1.1")
        monkeypatch.setenv("AF_DICT_PORT", "8080")
        from engine.config import _expand_dict_env_vars
        result = _expand_dict_env_vars({
            "host": "${AF_DICT_HOST}",
            "port": "${AF_DICT_PORT}",
            "name": "fixed",
        })
        assert result["host"] == "192.168.1.1"
        assert result["port"] == "8080"
        assert result["name"] == "fixed"

    def test_non_string_values_preserved(self):
        from engine.config import _expand_dict_env_vars
        result = _expand_dict_env_vars({"count": 42, "flag": True})
        assert result["count"] == 42
        assert result["flag"] is True


class TestApplyEnvOverrides:
    """验证 _apply_env_overrides 覆盖逻辑。"""

    def test_local_paths_override(self, monkeypatch):
        from engine.config import LOCAL_PATHS
        original = LOCAL_PATHS.get("sw_model", "")
        monkeypatch.setenv("AUTOFLUID_SW_MODEL", r"C:\override\model.SLDPRT")
        try:
            import engine.config as cfg
            cfg._apply_env_overrides()
            assert LOCAL_PATHS["sw_model"] == r"C:\override\model.SLDPRT"
        finally:
            LOCAL_PATHS["sw_model"] = original

    def test_remote_config_override(self, monkeypatch):
        from engine.config import REMOTE_CONFIG
        original_host = REMOTE_CONFIG.get("host", "")
        original_port = REMOTE_CONFIG.get("port", 22)
        monkeypatch.setenv("AUTOFLUID_SSH_HOST", "10.0.0.1")
        monkeypatch.setenv("AUTOFLUID_SSH_PORT", "2222")
        try:
            import engine.config as cfg
            cfg._apply_env_overrides()
            assert REMOTE_CONFIG["host"] == "10.0.0.1"
            assert REMOTE_CONFIG["port"] == 2222
        finally:
            REMOTE_CONFIG["host"] = original_host
            REMOTE_CONFIG["port"] = original_port


# ====================================================================
# reload_config_from_toml 测试
# ====================================================================

class TestReloadConfigFromToml:
    """验证 TOML 重载合并逻辑。"""

    def test_returns_false_when_no_toml(self, monkeypatch):
        import engine.config as cfg

        def _empty_load(*args, **kwargs):
            return {}

        monkeypatch.setattr(cfg, "load_toml_config", _empty_load)
        assert cfg.reload_config_from_toml() is False

    def test_merges_local_paths(self, monkeypatch):
        import engine.config as cfg
        from engine.config import LOCAL_PATHS

        original_step_dir = LOCAL_PATHS.get("step_dir", "")

        def _mock_load(*args, **kwargs):
            return {"local_paths": {"step_dir": r"C:\\new_step"}}

        monkeypatch.setattr(cfg, "load_toml_config", _mock_load)
        try:
            assert cfg.reload_config_from_toml() is True
            assert LOCAL_PATHS["step_dir"] == r"C:\\new_step"
        finally:
            LOCAL_PATHS["step_dir"] = original_step_dir

    def test_merges_remote_config(self, monkeypatch):
        import engine.config as cfg
        from engine.config import REMOTE_CONFIG

        original_host = REMOTE_CONFIG.get("host", "")

        def _mock_load(*args, **kwargs):
            return {"remote_config": {"host": "10.99.99.99"}}

        monkeypatch.setattr(cfg, "load_toml_config", _mock_load)
        try:
            assert cfg.reload_config_from_toml() is True
            assert REMOTE_CONFIG["host"] == "10.99.99.99"
        finally:
            REMOTE_CONFIG["host"] = original_host

    def test_merges_workstations_and_expands_env_vars(self, monkeypatch):
        import engine.config as cfg
        from engine.config import WORKSTATIONS

        monkeypatch.setenv("AUTOFLUID_WS_A_PASSWORD", "secret-a")
        original = [dict(ws) for ws in WORKSTATIONS]

        def _mock_load(*args, **kwargs):
            return {
                "workstations": [
                    {
                        "id": "WS-A",
                        "host": "10.0.0.10",
                        "reachable_host": "100.64.1.20",
                        "reachable_port": "2222",
                        "connectivity_mode": "tailscale",
                        "port": 2222,
                        "username": "ps",
                        "password": "${AUTOFLUID_WS_A_PASSWORD}",
                        "working_dir": r"D:\work",
                        "scripts_dir": r"D:\scripts",
                        "ref_files_dir": r"D:\refs",
                        "scdoc_dir": r"D:\scdoc",
                        "msh_dir": r"D:\msh",
                        "result_dir": r"D:\case",
                        "flag_dir": r"D:\flags",
                        "conda_env": "pyfluent",
                        "conda_exe": r"C:\conda.exe",
                        "mpi_bin_dir": r"C:\mpi",
                    }
                ]
            }

        monkeypatch.setattr(cfg, "load_toml_config", _mock_load)
        try:
            assert cfg.reload_config_from_toml() is True
            assert WORKSTATIONS == [
                {
                    "id": "WS-A",
                    "host": "10.0.0.10",
                    "reachable_host": "100.64.1.20",
                    "reachable_port": 2222,
                    "connectivity_mode": "tailscale",
                    "port": 2222,
                    "username": "ps",
                    "password": "secret-a",
                    "working_dir": r"D:\work",
                    "scripts_dir": r"D:\scripts",
                    "ref_files_dir": r"D:\refs",
                    "scdoc_dir": r"D:\scdoc",
                    "msh_dir": r"D:\msh",
                    "result_dir": r"D:\case",
                    "flag_dir": r"D:\flags",
                    "conda_env": "pyfluent",
                    "conda_exe": r"C:\conda.exe",
                    "mpi_bin_dir": r"C:\mpi",
                }
            ]
        finally:
            WORKSTATIONS[:] = original

    def test_remote_config_backfills_default_workstation(self, monkeypatch):
        import engine.config as cfg
        from engine.config import REMOTE_CONFIG, WORKSTATIONS

        original_remote = dict(REMOTE_CONFIG)
        original_workstations = [dict(ws) for ws in WORKSTATIONS]
        monkeypatch.delenv("AUTOFLUID_SSH_PASSWORD", raising=False)

        def _mock_load(*args, **kwargs):
            return {
                "remote_config": {
                    "host": "10.99.99.99",
                    "reachable_host": "203.0.113.10",
                    "connectivity_mode": "public",
                    "password": "pw",
                }
            }

        monkeypatch.setattr(cfg, "load_toml_config", _mock_load)
        try:
            assert cfg.reload_config_from_toml() is True
            assert WORKSTATIONS[0]["id"] == "default"
            assert WORKSTATIONS[0]["host"] == "10.99.99.99"
            assert WORKSTATIONS[0]["reachable_host"] == "203.0.113.10"
            assert WORKSTATIONS[0]["connectivity_mode"] == "public"
            assert WORKSTATIONS[0]["password"] == "pw"
        finally:
            REMOTE_CONFIG.update(original_remote)
            WORKSTATIONS[:] = original_workstations

    def test_merges_ipc_config_and_applies_env_overrides(self, monkeypatch):
        import engine.config as cfg
        from engine.config import IPC_CONFIG

        original = dict(IPC_CONFIG)
        monkeypatch.setenv("AUTOFLUID_IPC_HOST", "127.0.0.1")
        monkeypatch.setenv("AUTOFLUID_IPC_PORT", "9650")
        monkeypatch.setenv("AUTOFLUID_IPC_AUTH_TOKEN", "env-token")

        def _mock_load(*args, **kwargs):
            return {
                "ipc_config": {
                    "host": "0.0.0.0",
                    "port": 9528,
                    "auth_token": "${AUTOFLUID_IPC_AUTH_TOKEN}",
                }
            }

        monkeypatch.setattr(cfg, "load_toml_config", _mock_load)
        try:
            assert cfg.reload_config_from_toml() is True
            assert IPC_CONFIG["host"] == "127.0.0.1"
            assert IPC_CONFIG["port"] == 9650
            assert IPC_CONFIG["auth_token"] == "env-token"
        finally:
            IPC_CONFIG.clear()
            IPC_CONFIG.update(original)

    def test_merges_step_file_patterns(self, monkeypatch):
        import engine.config as cfg
        from engine.config import STEP_FILE_PATTERNS

        original = STEP_FILE_PATTERNS.get("sw", "")

        def _mock_load(*args, **kwargs):
            return {"step_file_patterns": {"sw": "custom_{config}.stp"}}

        monkeypatch.setattr(cfg, "load_toml_config", _mock_load)
        try:
            assert cfg.reload_config_from_toml() is True
            assert STEP_FILE_PATTERNS["sw"] == "custom_{config}.stp"
        finally:
            STEP_FILE_PATTERNS["sw"] = original

    def test_merges_engine_config(self, monkeypatch):
        import engine.config as cfg
        from engine.config import ENGINE_CONFIG

        original_max_retries = ENGINE_CONFIG.get("max_retries", 3)

        def _mock_load(*args, **kwargs):
            return {"engine_config": {"max_retries": 10}}

        monkeypatch.setattr(cfg, "load_toml_config", _mock_load)
        try:
            assert cfg.reload_config_from_toml() is True
            assert ENGINE_CONFIG["max_retries"] == 10
        finally:
            ENGINE_CONFIG["max_retries"] = original_max_retries

    def test_merges_operation_timeouts(self, monkeypatch):
        import engine.config as cfg
        from engine.config import OPERATION_TIMEOUTS

        original = OPERATION_TIMEOUTS.get("ssh_connection", 10)

        def _mock_load(*args, **kwargs):
            return {"operation_timeouts": {"ssh_connection": 30}}

        monkeypatch.setattr(cfg, "load_toml_config", _mock_load)
        try:
            assert cfg.reload_config_from_toml() is True
            assert OPERATION_TIMEOUTS["ssh_connection"] == 30
        finally:
            OPERATION_TIMEOUTS["ssh_connection"] = original

    def test_new_format_solidworks_section(self, monkeypatch):
        import engine.config as cfg
        from engine.config import ENGINE_CONFIG

        original = ENGINE_CONFIG.get("sw_macro_timeout", 3600)

        def _mock_load(*args, **kwargs):
            return {"solidworks": {"sw_macro_timeout": 7200}}

        monkeypatch.setattr(cfg, "load_toml_config", _mock_load)
        try:
            assert cfg.reload_config_from_toml() is True
            assert ENGINE_CONFIG["sw_macro_timeout"] == 7200
        finally:
            ENGINE_CONFIG["sw_macro_timeout"] = original

    def test_new_format_spaceclaim_section(self, monkeypatch):
        import engine.config as cfg
        from engine.config import ENGINE_CONFIG

        original = ENGINE_CONFIG.get("sc_timeout", 300)

        def _mock_load(*args, **kwargs):
            return {"spaceclaim": {"sc_timeout": 600}}

        monkeypatch.setattr(cfg, "load_toml_config", _mock_load)
        try:
            assert cfg.reload_config_from_toml() is True
            assert ENGINE_CONFIG["sc_timeout"] == 600
        finally:
            ENGINE_CONFIG["sc_timeout"] = original

    def test_new_format_global_settings_section(self, monkeypatch):
        import engine.config as cfg
        from engine.config import ENGINE_CONFIG

        original_refresh = ENGINE_CONFIG.get("state_refresh_interval", 0.5)
        original_meshing_processor_count = ENGINE_CONFIG.get("meshing_processor_count", 8)
        original_solver_processor_count = ENGINE_CONFIG.get("solver_processor_count", 128)

        def _mock_load(*args, **kwargs):
            return {
                "global_settings": {
                    "state_refresh_interval": 2.0,
                    "meshing_processor_count": 4,
                    "solver_processor_count": 64,
                }
            }

        monkeypatch.setattr(cfg, "load_toml_config", _mock_load)
        try:
            assert cfg.reload_config_from_toml() is True
            assert ENGINE_CONFIG["state_refresh_interval"] == 2.0
            assert ENGINE_CONFIG["meshing_processor_count"] == 4
            assert ENGINE_CONFIG["solver_processor_count"] == 64
        finally:
            ENGINE_CONFIG["state_refresh_interval"] = original_refresh
            ENGINE_CONFIG["meshing_processor_count"] = original_meshing_processor_count
            ENGINE_CONFIG["solver_processor_count"] = original_solver_processor_count

    def test_env_var_still_overrides_toml(self, monkeypatch):
        import engine.config as cfg
        from engine.config import LOCAL_PATHS

        monkeypatch.setenv("AUTOFLUID_SW_EXE", r"C:\env\override\sw.exe")
        original = LOCAL_PATHS.get("sw_exe", "")

        def _mock_load(*args, **kwargs):
            return {"local_paths": {"sw_exe": r"C:\toml\sw.exe"}}

        monkeypatch.setattr(cfg, "load_toml_config", _mock_load)
        try:
            cfg.reload_config_from_toml()
            assert LOCAL_PATHS["sw_exe"] == r"C:\env\override\sw.exe"
        finally:
            LOCAL_PATHS["sw_exe"] = original
            monkeypatch.delenv("AUTOFLUID_SW_EXE", raising=False)


class TestConfigDictCompleteness:
    """验证各配置字典的必填键完整性。"""

    _REMOTE_REQUIRED_KEYS = {
        "host", "port", "username", "password",
        "working_dir", "scripts_dir", "ref_files_dir",
        "scdoc_dir", "msh_dir", "result_dir", "flag_dir",
        "conda_env", "conda_exe", "mpi_bin_dir",
    }

    _IPC_REQUIRED_KEYS = {
        "host", "port", "db_path", "timeout", "max_connections", "auth_token",
    }

    _ENGINE_REQUIRED_KEYS = {
        "watchdog_interval", "sw_macro_timeout", "max_retries",
        "sc_timeout", "transfer_timeout", "meshing_timeout",
        "meshing_processor_count", "solver_timeout", "solver_processor_count",
        "solver_iteration_count",
    }

    def test_remote_config_keys(self):
        from engine.config import REMOTE_CONFIG
        missing = self._REMOTE_REQUIRED_KEYS - set(REMOTE_CONFIG.keys())
        assert not missing, f"REMOTE_CONFIG 缺失: {sorted(missing)}"

    def test_workstations_keys(self):
        from engine.config import WORKSTATIONS

        assert WORKSTATIONS
        for workstation in WORKSTATIONS:
            missing = (self._REMOTE_REQUIRED_KEYS | {"id"}) - set(workstation.keys())
            assert not missing, f"WORKSTATIONS 缺失: {sorted(missing)}"

    def test_ipc_config_keys(self):
        from engine.config import IPC_CONFIG
        missing = self._IPC_REQUIRED_KEYS - set(IPC_CONFIG.keys())
        assert not missing, f"IPC_CONFIG 缺失: {sorted(missing)}"

    def test_engine_config_keys(self):
        from engine.config import ENGINE_CONFIG
        missing = self._ENGINE_REQUIRED_KEYS - set(ENGINE_CONFIG.keys())
        assert not missing, f"ENGINE_CONFIG 缺失: {sorted(missing)}"


class TestWorkstationLookup:
    """验证工作站配置查询。"""

    def test_get_workstation_config_returns_copy(self, monkeypatch):
        import engine.config as cfg
        from engine.config import WORKSTATIONS

        original = [dict(ws) for ws in WORKSTATIONS]
        WORKSTATIONS[:] = [
            {
                "id": "WS-A",
                "host": "10.0.0.10",
                "port": 22,
                "username": "ps",
                "password": "pw",
                "working_dir": r"D:\work",
                "scripts_dir": r"D:\scripts",
                "ref_files_dir": r"D:\refs",
                "scdoc_dir": r"D:\scdoc",
                "msh_dir": r"D:\msh",
                "result_dir": r"D:\case",
                "flag_dir": r"D:\flags",
                "conda_env": "pyfluent",
                "conda_exe": r"C:\conda.exe",
                "mpi_bin_dir": r"C:\mpi",
            }
        ]
        try:
            workstation = cfg.get_workstation_config("WS-A")
            workstation["host"] = "changed"
            assert WORKSTATIONS[0]["host"] == "10.0.0.10"
        finally:
            WORKSTATIONS[:] = original

    def test_get_workstation_config_uses_reachable_host_in_server_mode(self, monkeypatch):
        import engine.config as cfg
        from engine.config import WORKSTATIONS

        original = [dict(ws) for ws in WORKSTATIONS]
        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        WORKSTATIONS[:] = [
            {
                "id": "WS-A",
                "host": "172.17.135.240",
                "reachable_host": "100.64.1.20",
                "connectivity_mode": "tailscale",
                "port": 22,
                "username": "ps",
                "password": "",
                "working_dir": r"D:\work",
                "scripts_dir": r"D:\scripts",
                "ref_files_dir": r"D:\refs",
                "scdoc_dir": r"D:\scdoc",
                "msh_dir": r"D:\msh",
                "result_dir": r"D:\case",
                "flag_dir": r"D:\flags",
                "conda_env": "pyfluent",
                "conda_exe": r"C:\conda.exe",
                "mpi_bin_dir": r"C:\mpi",
            }
        ]
        try:
            workstation = cfg.get_workstation_config("WS-A")

            assert workstation["host"] == "100.64.1.20"
            assert workstation["reachable_host"] == "100.64.1.20"
        finally:
            WORKSTATIONS[:] = original

    def test_get_workstation_config_uses_reachable_port_in_server_mode(self, monkeypatch):
        import engine.config as cfg
        from engine.config import WORKSTATIONS

        original = [dict(ws) for ws in WORKSTATIONS]
        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        WORKSTATIONS[:] = [
            {
                "id": "WS-A",
                "host": "172.17.135.240",
                "reachable_host": "127.0.0.1",
                "reachable_port": 2222,
                "connectivity_mode": "reverse_tunnel",
                "port": 22,
                "username": "ps",
                "password": "",
                "working_dir": r"D:\work",
                "remote_script_dir": r"D:\scripts",
                "scdoc_dir": r"D:\scdoc",
                "msh_dir": r"D:\msh",
                "result_dir": r"D:\case",
                "fluent_log_dir": r"D:\logs",
                "fluent_journal": r"D:\solver.jou",
                "fluent_post_journal": r"D:\post.jou",
                "conda_exe": r"C:\conda.exe",
                "mpi_bin_dir": r"C:\mpi",
            }
        ]
        try:
            workstation = cfg.get_workstation_config("WS-A")

            assert workstation["host"] == "127.0.0.1"
            assert workstation["port"] == 2222
            assert workstation["reachable_port"] == 2222
        finally:
            WORKSTATIONS[:] = original

    def test_get_workstation_config_rejects_unknown_id(self):
        import pytest
        import engine.config as cfg

        with pytest.raises(KeyError):
            cfg.get_workstation_config("missing")


class TestServerModeLocalPaths:
    """验证 server 模式下 daemon 本地产物路径不会落到 Windows 默认路径。"""

    def test_reload_config_uses_server_local_scdoc_dir(self, monkeypatch, tmp_path):
        import engine.config as cfg

        original_local = dict(cfg.LOCAL_PATHS)
        original_remote = dict(cfg.REMOTE_CONFIG)
        original_workstations = [dict(ws) for ws in cfg.WORKSTATIONS]
        data_dir = tmp_path / "server-data"

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        monkeypatch.delenv("AUTOFLUID_SCDOC_DIR", raising=False)
        monkeypatch.delenv("AUTOFLUID_DATA_DIR", raising=False)
        monkeypatch.setattr(
            cfg,
            "load_toml_config",
            lambda: {
                "local_paths": {
                    "data_dir": str(data_dir),
                    "scdoc_dir": r"C:\Users\XKZ\Documents\000ansys_data\scdoc",
                }
            },
        )

        try:
            assert cfg.reload_config_from_toml() is True

            assert cfg.LOCAL_PATHS["data_dir"] == str(data_dir)
            assert cfg.LOCAL_PATHS["scdoc_dir"] == str(data_dir / "scdoc")
        finally:
            cfg.LOCAL_PATHS.clear()
            cfg.LOCAL_PATHS.update(original_local)
            cfg.REMOTE_CONFIG.clear()
            cfg.REMOTE_CONFIG.update(original_remote)
            cfg.WORKSTATIONS[:] = original_workstations

    def test_reload_config_keeps_explicit_scdoc_env_override(self, monkeypatch, tmp_path):
        import engine.config as cfg

        original_local = dict(cfg.LOCAL_PATHS)
        original_remote = dict(cfg.REMOTE_CONFIG)
        original_workstations = [dict(ws) for ws in cfg.WORKSTATIONS]
        explicit_scdoc_dir = tmp_path / "explicit-scdoc"

        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        monkeypatch.setenv("AUTOFLUID_SCDOC_DIR", str(explicit_scdoc_dir))
        monkeypatch.setattr(
            cfg,
            "load_toml_config",
            lambda: {
                "local_paths": {
                    "data_dir": str(tmp_path / "server-data"),
                    "scdoc_dir": r"C:\Users\XKZ\Documents\000ansys_data\scdoc",
                }
            },
        )

        try:
            assert cfg.reload_config_from_toml() is True

            assert cfg.LOCAL_PATHS["scdoc_dir"] == str(explicit_scdoc_dir)
        finally:
            cfg.LOCAL_PATHS.clear()
            cfg.LOCAL_PATHS.update(original_local)
            cfg.REMOTE_CONFIG.clear()
            cfg.REMOTE_CONFIG.update(original_remote)
            cfg.WORKSTATIONS[:] = original_workstations
