"""
===============================================================================
Cleaner 和工具模块测试 (M8)

覆盖：
- FileCleaner.run_system_check: 本地路径检查
- FileCleaner.clean_step_files: 本地文件清理
- is_process_alive: 进程存活检测
- read_pid_file / write_pid_file / remove_pid_file: PID 文件 CRUD
- check_ipc_ready: IPC 端口探测
- read_model_configs: Excel 读取（mock openpyxl）
===============================================================================
"""
from __future__ import annotations

import os
import socket
from types import TracebackType

import pytest

from engine.config import LOCAL_PATHS, REMOTE_CONFIG, STEP_FILE_PATTERNS, WORKSTATIONS


# ====================================================================
# FileCleaner 测试
# ====================================================================

class _LockProbe:
    """测试用上下文锁，记录远端 SSH 操作是否发生在锁内。"""

    def __init__(self) -> None:
        self.held = False
        self.entries = 0

    def __enter__(self) -> "_LockProbe":
        self.held = True
        self.entries += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self.held = False
        return False


class TestFileCleanerSystemCheck:
    """验证 FileCleaner.run_system_check 本地路径检查。"""

    def test_checks_local_paths(self, tmp_path, monkeypatch):
        """系统自检应检查所有本地路径。"""
        # 创建假路径
        sw_exe = tmp_path / "SLDWORKS.exe"
        sw_exe.touch()
        sw_model = tmp_path / "model.SLDPRT"
        sw_model.touch()
        excel = tmp_path / "table.xlsx"
        excel.touch()
        step_dir = tmp_path / "step"
        step_dir.mkdir()
        sc_exe = tmp_path / "SpaceClaim.exe"
        sc_exe.touch()
        scdoc_dir = tmp_path / "scdoc"
        scdoc_dir.mkdir()

        monkeypatch.setitem(LOCAL_PATHS, "sw_exe", str(sw_exe))
        monkeypatch.setitem(LOCAL_PATHS, "sw_model", str(sw_model))
        monkeypatch.setitem(LOCAL_PATHS, "excel", str(excel))
        monkeypatch.setitem(LOCAL_PATHS, "step_dir", str(step_dir))
        monkeypatch.setitem(LOCAL_PATHS, "sc_exe", str(sc_exe))
        monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))

        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        # 创建临时 DB
        db_path = str(tmp_path / "test.db")
        import engine.config as cfg
        orig = cfg.IPC_CONFIG["db_path"]
        cfg.IPC_CONFIG["db_path"] = db_path
        try:
            state = StateManager(db_path=db_path)
            # 提供一个返回 "未连接" 的 SSH getter，避免 None 调用
            class _DisconnectedSSH:
                def is_connected(self):
                    return False

            cleaner = FileCleaner(state, lambda: _DisconnectedSSH())
            result = cleaner.run_system_check()

            assert "local_checks" in result
            for name, info in result["local_checks"].items():
                assert info["exists"] is True, f"{name}: {info['path']} should exist"
            # SSH 未连接时应报告连接失败
            assert result["remote_checks"]["ssh"] == "连接失败"
        finally:
            cfg.IPC_CONFIG["db_path"] = orig

    def test_remote_system_check_holds_ssh_lock(self, tmp_path):
        """远端系统自检应在共享 SSH 锁内执行。"""
        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        class _ConnectedSSH:
            def __init__(self, lock: _LockProbe) -> None:
                self.lock = lock
                self.check_system_saw_lock = False

            def is_connected(self) -> bool:
                return True

            def check_system(self, **_kwargs: object) -> dict[str, object]:
                self.check_system_saw_lock = self.lock.held
                return {"ssh_connected": True}

        db_path = str(tmp_path / "test.db")
        import engine.config as cfg
        orig = cfg.IPC_CONFIG["db_path"]
        cfg.IPC_CONFIG["db_path"] = db_path
        try:
            state = StateManager(db_path=db_path)
            lock = _LockProbe()
            ssh = _ConnectedSSH(lock)
            cleaner = FileCleaner(state, lambda: ssh, ssh_lock=lock)

            cleaner.run_system_check()

            assert ssh.check_system_saw_lock is True
            assert lock.entries == 1
        finally:
            cfg.IPC_CONFIG["db_path"] = orig

    def test_server_mode_warns_about_private_workstation_host(self, tmp_path, monkeypatch):
        """server/ocar 模式下应提示私网工作站地址可能不可达。"""
        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        class _DisconnectedSSH:
            def is_connected(self):
                return False

        import engine.config as cfg
        original_db_path = cfg.IPC_CONFIG["db_path"]
        original_workstations = [dict(ws) for ws in WORKSTATIONS]
        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        cfg.IPC_CONFIG["db_path"] = str(tmp_path / "test.db")
        WORKSTATIONS[:] = [
            {
                **REMOTE_CONFIG,
                "id": "WS-A",
                "host": "172.17.135.240",
                "port": 22,
            }
        ]
        try:
            state = StateManager(db_path=cfg.IPC_CONFIG["db_path"])
            cleaner = FileCleaner(state, lambda: _DisconnectedSSH())

            result = cleaner.run_system_check()

            workstation_checks = result["workstation_checks"]
            assert workstation_checks["server_mode"] is True
            assert workstation_checks["workstations"][0]["id"] == "WS-A"
            assert "ocar" in workstation_checks["workstations"][0]["warning"]
            assert workstation_checks["workstations"][0]["severity"] == "error"
            assert workstation_checks["workstations"][0]["effective_host"] == "172.17.135.240"
        finally:
            cfg.IPC_CONFIG["db_path"] = original_db_path
            WORKSTATIONS[:] = original_workstations

    def test_server_mode_separates_daemon_and_local_worker_checks(self, tmp_path, monkeypatch):
        """server/ocar 模式下 daemon 自检不应把 Windows 本地路径当作本机失败项。"""
        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        class _DisconnectedSSH:
            def is_connected(self):
                return False

        import engine.config as cfg
        original_db_path = cfg.IPC_CONFIG["db_path"]
        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        cfg.IPC_CONFIG["db_path"] = str(tmp_path / "test.db")
        try:
            state = StateManager(db_path=cfg.IPC_CONFIG["db_path"])
            cleaner = FileCleaner(state, lambda: _DisconnectedSSH())

            result = cleaner.run_system_check()

            assert result["local_checks"] == {}
            assert result["daemon_checks"]["server_mode"]["value"] is True
            assert "scdoc_dir" in result["daemon_checks"]
            assert "workstation_checks" in result
        finally:
            cfg.IPC_CONFIG["db_path"] = original_db_path

    def test_server_mode_accepts_explicit_ocar_reachable_workstation_host(self, tmp_path, monkeypatch):
        """server/ocar 模式下应优先使用显式配置的 ocar 可达地址。"""
        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        class _DisconnectedSSH:
            def is_connected(self):
                return False

        import engine.config as cfg
        original_db_path = cfg.IPC_CONFIG["db_path"]
        original_workstations = [dict(ws) for ws in WORKSTATIONS]
        monkeypatch.setenv("AUTOFLUID_SERVER_MODE", "server")
        cfg.IPC_CONFIG["db_path"] = str(tmp_path / "test.db")
        WORKSTATIONS[:] = [
            {
                **REMOTE_CONFIG,
                "id": "WS-A",
                "host": "172.17.135.240",
                "reachable_host": "100.64.1.20",
                "connectivity_mode": "tailscale",
                "port": 22,
            }
        ]
        try:
            state = StateManager(db_path=cfg.IPC_CONFIG["db_path"])
            cleaner = FileCleaner(state, lambda: _DisconnectedSSH())

            result = cleaner.run_system_check()

            workstation = result["workstation_checks"]["workstations"][0]
            assert workstation["host"] == "172.17.135.240"
            assert workstation["effective_host"] == "100.64.1.20"
            assert workstation["connectivity_mode"] == "tailscale"
            assert workstation["severity"] == "ok"
            assert workstation["warning"] == ""
        finally:
            cfg.IPC_CONFIG["db_path"] = original_db_path
            WORKSTATIONS[:] = original_workstations


class TestFileCleanerCleanStepFiles:
    """验证 FileCleaner.clean_step_files 本地文件清理。"""

    def test_clean_sw_files(self, tmp_path, monkeypatch):
        """清理 SW 步骤的本地 STEP 文件。"""
        step_dir = tmp_path / "step"
        step_dir.mkdir()
        monkeypatch.setitem(LOCAL_PATHS, "step_dir", str(step_dir))

        # 创建 STEP 文件
        step_file = step_dir / STEP_FILE_PATTERNS["sw"].format(config=1)
        step_file.write_bytes(b"step data")

        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        db_path = str(tmp_path / "test.db")
        import engine.config as cfg
        orig = cfg.IPC_CONFIG["db_path"]
        cfg.IPC_CONFIG["db_path"] = db_path
        try:
            state = StateManager(db_path=db_path)
            state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            cleaner = FileCleaner(state, lambda: None)
            cleaner.clean_step_files("sw", config_name=1)

            assert not step_file.exists()
        finally:
            cfg.IPC_CONFIG["db_path"] = orig

    def test_clean_sc_files(self, tmp_path, monkeypatch):
        """清理 SC 步骤的本地 SCDOC 文件。"""
        scdoc_dir = tmp_path / "scdoc"
        scdoc_dir.mkdir()
        monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))

        scdoc_name = STEP_FILE_PATTERNS["sc"].format(config=2)
        scdoc_file = scdoc_dir / scdoc_name
        scdoc_file.write_bytes(b"scdoc data")

        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        db_path = str(tmp_path / "test.db")
        import engine.config as cfg
        orig = cfg.IPC_CONFIG["db_path"]
        cfg.IPC_CONFIG["db_path"] = db_path
        try:
            state = StateManager(db_path=db_path)
            state.load_configs({2: [1.0, 2.0, 3.0, 4.0]})

            class _DisconnectedSSH:
                def is_connected(self):
                    return False

            cleaner = FileCleaner(state, lambda: _DisconnectedSSH())
            cleaner.clean_local_step_files("sc", config_name=2)

            assert not scdoc_file.exists()
        finally:
            cfg.IPC_CONFIG["db_path"] = orig

    def test_clean_nonexistent_file_no_error(self, tmp_path, monkeypatch):
        """清理不存在的文件不抛异常。"""
        monkeypatch.setitem(LOCAL_PATHS, "step_dir", str(tmp_path / "empty"))

        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        db_path = str(tmp_path / "test.db")
        import engine.config as cfg
        orig = cfg.IPC_CONFIG["db_path"]
        cfg.IPC_CONFIG["db_path"] = db_path
        try:
            state = StateManager(db_path=db_path)
            state.load_configs({1: [1.0, 2.0, 3.0, 4.0]})
            cleaner = FileCleaner(state, lambda: None)
            cleaner.clean_step_files("sw", config_name=1)  # 不应抛异常
        finally:
            cfg.IPC_CONFIG["db_path"] = orig

    def test_clean_all_cache_clears_remote_work_and_flag_dirs(self, tmp_path, monkeypatch):
        """clean all cache 应清空远程工作目录和标志目录内容。"""
        monkeypatch.setitem(REMOTE_CONFIG, "working_dir", r"D:\xkz_1020\workingdir")
        monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\xkz_1020\flags")

        class _ConnectedSSH:
            def __init__(self) -> None:
                self.cleared_dirs: list[str] = []

            def is_connected(self) -> bool:
                return True

            def clear_remote_directory(self, remote_dir: str) -> tuple[int, int]:
                self.cleared_dirs.append(remote_dir)
                return (2, 0)

        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        db_path = str(tmp_path / "test.db")
        import engine.config as cfg
        orig = cfg.IPC_CONFIG["db_path"]
        cfg.IPC_CONFIG["db_path"] = db_path
        try:
            state = StateManager(db_path=db_path)
            ssh = _ConnectedSSH()
            cleaner = FileCleaner(state, lambda: ssh)

            cleaner.clean_all_cache()

            assert ssh.cleared_dirs == [
                "D:/xkz_1020/workingdir",
                "D:/xkz_1020/flags",
            ]
        finally:
            cfg.IPC_CONFIG["db_path"] = orig

    def test_clean_all_cache_clears_all_configured_workstations(self, tmp_path, monkeypatch):
        """clean all cache 应按工作站配置清理所有远程缓存目录。"""
        workstations = [
            {
                "id": "WS-A",
                "host": "10.0.0.1",
                "port": 22,
                "username": "ps",
                "password": "pw",
                "working_dir": r"D:\ws_a\working",
                "scripts_dir": r"D:\ws_a\scripts",
                "ref_files_dir": r"D:\ws_a\refs",
                "scdoc_dir": r"D:\ws_a\scdoc",
                "msh_dir": r"D:\ws_a\msh",
                "result_dir": r"D:\ws_a\case",
                "flag_dir": r"D:\ws_a\flags",
                "conda_env": "pyfluent",
                "conda_exe": r"C:\conda.exe",
                "mpi_bin_dir": r"C:\mpi",
            },
            {
                "id": "WS-B",
                "host": "10.0.0.2",
                "port": 22,
                "username": "ps",
                "password": "pw",
                "working_dir": r"E:\ws_b\working",
                "scripts_dir": r"E:\ws_b\scripts",
                "ref_files_dir": r"E:\ws_b\refs",
                "scdoc_dir": r"E:\ws_b\scdoc",
                "msh_dir": r"E:\ws_b\msh",
                "result_dir": r"E:\ws_b\case",
                "flag_dir": r"E:\ws_b\flags",
                "conda_env": "pyfluent",
                "conda_exe": r"C:\conda.exe",
                "mpi_bin_dir": r"C:\mpi",
            },
        ]
        monkeypatch.setattr("engine.config.WORKSTATIONS", workstations)
        monkeypatch.setattr("executor.cleaner.WORKSTATIONS", workstations)

        class _ConnectedSSH:
            def __init__(self, workstation_id: str) -> None:
                self.workstation_id = workstation_id
                self.cleared_dirs: list[str] = []

            def is_connected(self) -> bool:
                return True

            def clear_remote_directory(self, remote_dir: str) -> tuple[int, int]:
                self.cleared_dirs.append(remote_dir)
                return (1, 0)

        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        db_path = str(tmp_path / "test.db")
        import engine.config as cfg
        orig = cfg.IPC_CONFIG["db_path"]
        cfg.IPC_CONFIG["db_path"] = db_path
        try:
            state = StateManager(db_path=db_path)
            ssh_by_id = {ws["id"]: _ConnectedSSH(str(ws["id"])) for ws in workstations}

            def get_ssh(workstation_id: str = "default") -> _ConnectedSSH:
                return ssh_by_id[workstation_id]

            cleaner = FileCleaner(state, get_ssh)

            cleaner.clean_all_cache()

            assert ssh_by_id["WS-A"].cleared_dirs == [
                "D:/ws_a/working",
                "D:/ws_a/flags",
            ]
            assert ssh_by_id["WS-B"].cleared_dirs == [
                "E:/ws_b/working",
                "E:/ws_b/flags",
            ]
        finally:
            cfg.IPC_CONFIG["db_path"] = orig

    def test_clean_all_cache_holds_ssh_lock(self, tmp_path, monkeypatch):
        """远程缓存清理应在共享 SSH 锁内执行。"""
        monkeypatch.setitem(REMOTE_CONFIG, "working_dir", r"D:\xkz_1020\workingdir")
        monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", r"D:\xkz_1020\flags")

        class _ConnectedSSH:
            def __init__(self, lock: _LockProbe) -> None:
                self.lock = lock
                self.clear_calls_saw_lock: list[bool] = []

            def is_connected(self) -> bool:
                return True

            def clear_remote_directory(self, remote_dir: str) -> tuple[int, int]:
                self.clear_calls_saw_lock.append(self.lock.held)
                return (1, 0)

        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        db_path = str(tmp_path / "test.db")
        import engine.config as cfg
        orig = cfg.IPC_CONFIG["db_path"]
        cfg.IPC_CONFIG["db_path"] = db_path
        try:
            state = StateManager(db_path=db_path)
            lock = _LockProbe()
            ssh = _ConnectedSSH(lock)
            cleaner = FileCleaner(state, lambda: ssh, ssh_lock=lock)

            cleaner.clean_all_cache()

            assert ssh.clear_calls_saw_lock == [True, True]
            assert lock.entries == 1
        finally:
            cfg.IPC_CONFIG["db_path"] = orig

    def test_clean_all_cache_fails_when_workstation_ssh_disconnected(self, tmp_path):
        """clean all cache 不应在远程工作站不可达时静默成功。"""

        class _DisconnectedSSH:
            def is_connected(self) -> bool:
                return False

        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        db_path = str(tmp_path / "test.db")
        import engine.config as cfg
        orig = cfg.IPC_CONFIG["db_path"]
        cfg.IPC_CONFIG["db_path"] = db_path
        try:
            state = StateManager(db_path=db_path)
            cleaner = FileCleaner(state, lambda: _DisconnectedSSH())

            with pytest.raises(RuntimeError, match="远程缓存清理未完成"):
                cleaner.clean_all_cache()
        finally:
            cfg.IPC_CONFIG["db_path"] = orig

    def test_remote_step_file_cleanup_holds_ssh_lock(self, tmp_path, monkeypatch):
        """远程步骤文件清理应在共享 SSH 锁内执行。"""
        scdoc_dir = tmp_path / "scdoc"
        scdoc_dir.mkdir()
        monkeypatch.setitem(LOCAL_PATHS, "scdoc_dir", str(scdoc_dir))
        monkeypatch.setitem(REMOTE_CONFIG, "scdoc_dir", r"D:\xkz_1020\scdoc")

        class _ConnectedSSH:
            def __init__(self, lock: _LockProbe) -> None:
                self.lock = lock
                self.delete_calls_saw_lock: list[bool] = []

            def is_connected(self) -> bool:
                return True

            def delete_remote_file(self, remote_path: str) -> bool:
                self.delete_calls_saw_lock.append(self.lock.held)
                return True

        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        db_path = str(tmp_path / "test.db")
        import engine.config as cfg
        orig = cfg.IPC_CONFIG["db_path"]
        cfg.IPC_CONFIG["db_path"] = db_path
        try:
            state = StateManager(db_path=db_path)
            lock = _LockProbe()
            ssh = _ConnectedSSH(lock)
            cleaner = FileCleaner(state, lambda: ssh, ssh_lock=lock)

            cleaner.clean_step_files("sc", config_name=2)

            assert ssh.delete_calls_saw_lock == [True]
            assert lock.entries == 1
        finally:
            cfg.IPC_CONFIG["db_path"] = orig

    def test_remote_step_file_cleanup_fails_when_ssh_disconnected(self, tmp_path):
        """远程步骤文件清理不可达时应失败，避免 clean 指令误报成功。"""

        class _DisconnectedSSH:
            def is_connected(self) -> bool:
                return False

        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        db_path = str(tmp_path / "test.db")
        import engine.config as cfg
        orig = cfg.IPC_CONFIG["db_path"]
        cfg.IPC_CONFIG["db_path"] = db_path
        try:
            state = StateManager(db_path=db_path)
            state.load_configs({2: [1.0, 2.0, 3.0, 4.0]})
            cleaner = FileCleaner(state, lambda: _DisconnectedSSH())

            with pytest.raises(RuntimeError, match="远程文件清理未完成"):
                cleaner.clean_step_files("meshing", config_name=2)
        finally:
            cfg.IPC_CONFIG["db_path"] = orig

    def test_remote_step_file_cleanup_uses_config_workstation(self, tmp_path, monkeypatch):
        """按构型清理远程步骤时应路由到该构型分配的工作站。"""
        ws_b = {
            "id": "WS-B",
            "host": "10.0.0.2",
            "port": 22,
            "username": "ps",
            "password": "pw",
            "working_dir": r"E:\ws_b\working",
            "scripts_dir": r"E:\ws_b\scripts",
            "ref_files_dir": r"E:\ws_b\refs",
            "scdoc_dir": r"E:\ws_b\scdoc",
            "msh_dir": r"E:\ws_b\msh",
            "result_dir": r"E:\ws_b\case",
            "flag_dir": r"E:\ws_b\flags",
            "conda_env": "pyfluent",
            "conda_exe": r"C:\conda.exe",
            "mpi_bin_dir": r"C:\mpi",
        }
        monkeypatch.setattr("executor.cleaner.get_workstation_config", lambda _wid: dict(ws_b))

        class _ConnectedSSH:
            def __init__(self) -> None:
                self.deleted: list[str] = []

            def is_connected(self) -> bool:
                return True

            def delete_remote_file(self, remote_path: str) -> bool:
                self.deleted.append(remote_path)
                return True

        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        db_path = str(tmp_path / "test.db")
        import engine.config as cfg
        orig = cfg.IPC_CONFIG["db_path"]
        cfg.IPC_CONFIG["db_path"] = db_path
        try:
            state = StateManager(db_path=db_path)
            state.load_configs({2: [1.0, 2.0, 3.0, 4.0]})
            state.set_config_workstation(2, "WS-B")
            ssh = _ConnectedSSH()
            requested_ids: list[str] = []

            def get_ssh(workstation_id: str = "default") -> _ConnectedSSH:
                requested_ids.append(workstation_id)
                return ssh

            cleaner = FileCleaner(state, get_ssh)

            cleaner.clean_step_files("meshing", config_name=2)

            assert requested_ids == ["WS-B"]
            assert ssh.deleted == ["E:/ws_b/msh/model_gen4_2.msh.h5"]
        finally:
            cfg.IPC_CONFIG["db_path"] = orig

    def test_clean_all_cache_rejects_remote_root_dirs(self, tmp_path, monkeypatch):
        """clean all cache 不应清理远程根目录。"""
        monkeypatch.setitem(REMOTE_CONFIG, "working_dir", r"D:\\")
        monkeypatch.setitem(REMOTE_CONFIG, "flag_dir", "")

        class _ConnectedSSH:
            def __init__(self) -> None:
                self.cleared_dirs: list[str] = []

            def is_connected(self) -> bool:
                return True

            def clear_remote_directory(self, remote_dir: str) -> tuple[int, int]:
                self.cleared_dirs.append(remote_dir)
                return (1, 0)

        from executor.cleaner import FileCleaner
        from engine.state_manager import StateManager

        db_path = str(tmp_path / "test.db")
        import engine.config as cfg
        orig = cfg.IPC_CONFIG["db_path"]
        cfg.IPC_CONFIG["db_path"] = db_path
        try:
            state = StateManager(db_path=db_path)
            ssh = _ConnectedSSH()
            cleaner = FileCleaner(state, lambda: ssh)

            with pytest.raises(RuntimeError, match="远程缓存清理未完成"):
                cleaner.clean_all_cache()

            assert ssh.cleared_dirs == []
        finally:
            cfg.IPC_CONFIG["db_path"] = orig


# ====================================================================
# process_utils 测试
# ====================================================================

class TestProcessUtils:
    """验证进程管理工具函数。"""

    def test_current_process_alive(self):
        """当前进程应为存活状态。"""
        from utils.process_utils import is_process_alive
        assert is_process_alive(os.getpid()) is True

    def test_invalid_pid_not_alive(self):
        """无效 PID 应返回 False。"""
        from utils.process_utils import is_process_alive
        assert is_process_alive(-1) is False
        assert is_process_alive(0) is False

    def test_pid_file_roundtrip(self, tmp_path):
        """PID 文件的写入/读取/删除往返。"""
        from utils.process_utils import read_pid_file, write_pid_file, remove_pid_file

        pid_file = str(tmp_path / "test.pid")
        write_pid_file(pid_file, 12345)
        assert read_pid_file(pid_file) == 12345

        remove_pid_file(pid_file)
        assert read_pid_file(pid_file) is None

    def test_read_nonexistent_pid_file(self, tmp_path):
        """读取不存在的 PID 文件返回 None。"""
        from utils.process_utils import read_pid_file
        assert read_pid_file(str(tmp_path / "nope.pid")) is None

    def test_read_invalid_pid_file(self, tmp_path):
        """读取内容非法的 PID 文件返回 None。"""
        from utils.process_utils import read_pid_file
        pid_file = tmp_path / "bad.pid"
        pid_file.write_text("not_a_number", encoding="utf-8")
        assert read_pid_file(str(pid_file)) is None

    def test_remove_nonexistent_pid_file_no_error(self, tmp_path):
        """删除不存在的 PID 文件不抛异常。"""
        from utils.process_utils import remove_pid_file
        remove_pid_file(str(tmp_path / "nope.pid"))  # 不应抛异常


class TestCheckIpcReady:
    """验证 IPC 端口探测。"""

    def test_port_open_returns_true(self):
        """端口已打开时返回 True。"""
        from utils.process_utils import check_ipc_ready

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            assert check_ipc_ready("127.0.0.1", port) is True

    def test_wildcard_host_uses_loopback_for_probe(self):
        """服务端监听通配地址时，客户端探测应连接本机回环地址。"""
        from utils.process_utils import check_ipc_ready

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            assert check_ipc_ready("0.0.0.0", port) is True

    def test_port_closed_returns_false(self):
        """端口未打开时返回 False。"""
        from utils.process_utils import check_ipc_ready
        # 使用一个不太可能被占用的端口
        assert check_ipc_ready("127.0.0.1", 1) is False


# ====================================================================
# excel_reader 测试
# ====================================================================

class TestExcelReader:
    """验证 Excel 读取工具。"""

    def test_read_valid_excel(self, tmp_path):
        """读取有效的 Excel 文件。"""
        try:
            import openpyxl
        except ImportError:
            pytest.skip("openpyxl 未安装")

        from utils.excel_reader import read_model_configs

        excel_file = tmp_path / "test.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        # 表头（前 2 行）
        ws.append(["Name", "P1", "P2", "P3", "P4"])
        ws.append(["", "", "", "", ""])
        # 数据
        ws.append([1, 1.0, 2.0, 3.0, 4.0])
        ws.append([2, 5.0, 6.0, 7.0, 8.0])
        wb.save(str(excel_file))
        wb.close()

        configs = read_model_configs(str(excel_file))
        assert configs == {1: [1.0, 2.0, 3.0, 4.0], 2: [5.0, 6.0, 7.0, 8.0]}

    def test_read_nonexistent_excel(self):
        """读取不存在的文件抛 FileNotFoundError。"""
        from utils.excel_reader import read_model_configs
        with pytest.raises(FileNotFoundError):
            read_model_configs("/nonexistent/path.xlsx")

    def test_read_empty_rows_stops(self, tmp_path):
        """遇到空行停止读取。"""
        try:
            import openpyxl
        except ImportError:
            pytest.skip("openpyxl 未安装")

        from utils.excel_reader import read_model_configs

        excel_file = tmp_path / "test.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Name", "P1", "P2", "P3", "P4"])
        ws.append(["", "", "", "", ""])
        ws.append([1, 1.0, 2.0, 3.0, 4.0])
        ws.append([None, None, None, None, None])  # 空行
        ws.append([99, 9.0, 9.0, 9.0, 9.0])  # 不应被读取
        wb.save(str(excel_file))
        wb.close()

        configs = read_model_configs(str(excel_file))
        assert 1 in configs
        assert 99 not in configs
