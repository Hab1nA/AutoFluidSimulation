import base64
import hashlib
import socket
from unittest.mock import patch

import pytest

import utils.ssh_client as ssh_client_module
from utils.ssh_client import RemoteWorkstation


def test_connect_uses_password_auth_when_password_is_configured(monkeypatch):
    calls: list[dict[str, object]] = []

    class _Transport:
        def set_keepalive(self, _seconds: int) -> None:
            pass

    class _SSHClient:
        def set_missing_host_key_policy(self, _policy: object) -> None:
            pass

        def connect(self, **kwargs: object) -> None:
            calls.append(kwargs)

        def get_transport(self):
            return _Transport()

        def open_sftp(self):
            return object()

    class _Paramiko:
        SSHException = Exception
        SSHClient = _SSHClient

        class AutoAddPolicy:
            pass

    monkeypatch.setattr(ssh_client_module, "paramiko", _Paramiko)

    host = RemoteWorkstation("127.0.0.1", 22, "user", "secret")

    assert host.connect() is True
    assert calls == [{
        "hostname": "127.0.0.1",
        "port": 22,
        "username": "user",
        "password": "secret",
        "key_filename": None,
        "timeout": 10,
        "look_for_keys": False,
        "allow_agent": False,
    }]


def test_connect_allows_key_auth_when_password_is_empty(monkeypatch):
    calls: list[dict[str, object]] = []

    class _Transport:
        def set_keepalive(self, _seconds: int) -> None:
            pass

    class _SSHClient:
        def set_missing_host_key_policy(self, _policy: object) -> None:
            pass

        def connect(self, **kwargs: object) -> None:
            calls.append(kwargs)

        def get_transport(self):
            return _Transport()

        def open_sftp(self):
            return object()

    class _Paramiko:
        SSHException = Exception
        SSHClient = _SSHClient

        class AutoAddPolicy:
            pass

    monkeypatch.setattr(ssh_client_module, "paramiko", _Paramiko)

    host = RemoteWorkstation("127.0.0.1", 22, "user", "")

    assert host.connect() is True
    assert calls == [{
        "hostname": "127.0.0.1",
        "port": 22,
        "username": "user",
        "password": None,
        "key_filename": None,
        "timeout": 10,
        "look_for_keys": True,
        "allow_agent": True,
    }]


def test_connect_uses_configured_key_file(monkeypatch):
    calls: list[dict[str, object]] = []

    class _Transport:
        def set_keepalive(self, _seconds: int) -> None:
            pass

    class _SSHClient:
        def set_missing_host_key_policy(self, _policy: object) -> None:
            pass

        def connect(self, **kwargs: object) -> None:
            calls.append(kwargs)

        def get_transport(self):
            return _Transport()

        def open_sftp(self):
            return object()

    class _Paramiko:
        SSHException = Exception
        SSHClient = _SSHClient

        class AutoAddPolicy:
            pass

    monkeypatch.setattr(ssh_client_module, "paramiko", _Paramiko)

    host = RemoteWorkstation(
        "127.0.0.1",
        22,
        "user",
        "",
        key_filename=r"C:\Users\XKZ\.ssh\id_ed25519",
    )

    assert host.connect() is True
    assert calls == [{
        "hostname": "127.0.0.1",
        "port": 22,
        "username": "user",
        "password": None,
        "key_filename": r"C:\Users\XKZ\.ssh\id_ed25519",
        "timeout": 10,
        "look_for_keys": True,
        "allow_agent": True,
    }]


def test_connect_supports_none_auth(monkeypatch):
    calls: list[tuple[str, object]] = []

    class _Transport:
        def __init__(self, addr: tuple[str, int]) -> None:
            calls.append(("transport_init", addr))

        def start_client(self, timeout: int) -> None:
            calls.append(("start_client", timeout))

        def auth_none(self, username: str) -> None:
            calls.append(("auth_none", username))

        def set_keepalive(self, _seconds: int) -> None:
            calls.append(("set_keepalive", _seconds))

        def open_session(self, timeout: int | None = None):
            calls.append(("open_session", timeout))
            return object()

    class _SSHClient:
        def __init__(self) -> None:
            self.transport = None

        def set_missing_host_key_policy(self, _policy: object) -> None:
            pass

        def connect(self, **kwargs: object) -> None:
            raise AssertionError(f"none auth should not call SSHClient.connect: {kwargs}")

        def get_transport(self):
            calls.append(("get_transport", None))
            return self.transport

        def _transport(self, transport):
            self.transport = transport

        def open_sftp(self):
            calls.append(("open_sftp", None))
            return object()

    class _Paramiko:
        SSHException = Exception
        SSHClient = _SSHClient
        Transport = _Transport

        class AutoAddPolicy:
            pass

    monkeypatch.setattr(ssh_client_module, "paramiko", _Paramiko)

    host = RemoteWorkstation("127.0.0.1", 22, "user", "", auth_method="none")

    assert host.connect() is True
    assert ("transport_init", ("127.0.0.1", 22)) in calls
    assert ("start_client", 10) in calls
    assert ("auth_none", "user") in calls
    assert ("open_sftp", None) in calls


def test_connect_closes_none_auth_transport_on_failure(monkeypatch):
    calls: list[tuple[str, object]] = []

    class _Transport:
        def __init__(self, addr: tuple[str, int]) -> None:
            calls.append(("transport_init", addr))

        def start_client(self, timeout: int) -> None:
            calls.append(("start_client", timeout))

        def auth_none(self, username: str) -> None:
            calls.append(("auth_none", username))
            raise _Paramiko.SSHException("none auth rejected")

        def close(self) -> None:
            calls.append(("close", None))

    class _SSHClient:
        def set_missing_host_key_policy(self, _policy: object) -> None:
            pass

        def open_sftp(self):
            raise AssertionError("open_sftp should not be reached")

    class _Paramiko:
        SSHException = Exception
        SSHClient = _SSHClient
        Transport = _Transport

        class AutoAddPolicy:
            pass

    monkeypatch.setattr(ssh_client_module, "paramiko", _Paramiko)

    host = RemoteWorkstation("127.0.0.1", 22, "user", "", auth_method="none")

    assert host.connect() is False
    assert ("auth_none", "user") in calls
    assert ("close", None) in calls


def _decode_encoded_command(script: str) -> str:
    encoded_command = script.split("-EncodedCommand ", 1)[1].split(" ", 1)[0]
    return base64.b64decode(encoded_command).decode("utf-16le")


def test_build_background_cmd_script_writes_done_or_error_flag():
    script = RemoteWorkstation._build_background_cmd_script(
        r'"C:\ProgramData\anaconda3\Scripts\conda.exe" run -n pyfluent python run.py',
        r"D:/flags/job.done",
        r"D:/flags/job.log",
    )
    assert r'>> "D:\flags\job.log" 2>&1' in script
    assert r'set "AF_PID_FILE=D:\flags\job.done.pid"' in script
    ps_script = _decode_encoded_command(script)
    assert "Start-Process -FilePath 'cmd.exe'" in ps_script
    assert r'''-ArgumentList '/d','/s','/c','"C:\ProgramData\anaconda3\Scripts\conda.exe" run -n pyfluent python run.py' ''' in ps_script
    assert "AF_CMD" not in script
    assert r'echo done > "D:\flags\job.done"' in script
    assert r'echo error %AF_EXIT% > "D:\flags\job.done.error"' in script
    assert r'del /f /q "%AF_PID_FILE%"' in script
    assert "-WindowStyle Hidden" in ps_script
    # working_dir 未指定时不应包含 cd /d
    assert "cd /d" not in script


def test_build_background_cmd_script_interactive_calls_command_directly():
    script = RemoteWorkstation._build_background_cmd_script(
        r'conda run python script.py',
        r"D:/flags/job.done",
        r"D:/flags/job.log",
        interactive=True,
    )
    assert "call conda run python script.py" in script
    assert "Start-Process" not in script
    assert "wmic process where" in script
    assert "AF_WRAPPER_PID" in script
    assert "ParentProcessId" not in script
    assert r'> "%AF_PID_FILE%"' in script


def test_build_background_cmd_script_with_working_dir():
    script = RemoteWorkstation._build_background_cmd_script(
        r'conda run python script.py',
        r"D:/flags/job.done",
        r"D:/flags/job.log",
        working_dir=r"D:\xkz_1020",
    )
    assert r'cd /d "D:\xkz_1020"' in script
    assert r'echo done > "D:\flags\job.done"' in script


def test_exec_background_uses_scheduled_task_and_writes_wrapper_script():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    calls: list[tuple[str, int]] = []
    deleted: list[str] = []
    written: list[tuple[str, str]] = []

    def fake_exec(command: str, timeout: int = 30):
        # exec_command returns: (stdout, stderr, exit_code)
        calls.append((command, timeout))
        return ("", "", 0)

    command = (
        r'call "C:\Program Files\Anaconda3\Scripts\activate.bat" '
        r'&& python "D:\work dir\run.py" --msg "hello world"'
    )
    flag_file = r"D:\flags\task done.flag"

    with patch.object(host, "ensure_connected", return_value=True):
        with patch.object(host, "exec_command", side_effect=fake_exec):
            with patch.object(
                host,
                "delete_remote_file",
                side_effect=lambda path: deleted.append(path) or True,
            ):
                with patch.object(host, "_ensure_remote_dir") as ensure_dir:
                    with patch.object(
                        host,
                        "_write_remote_text_file",
                        side_effect=lambda path, content: written.append((path, content)),
                    ):
                        result, task_name = host.exec_background(command, flag_file)
                        assert result is True
                        assert task_name.startswith("AutoFluid_")

    assert len(calls) == 3
    assert calls[0][0].startswith('schtasks /Create /TN "AutoFluid_')
    assert " /IT" not in calls[0][0]
    assert calls[0][1] == 30
    assert calls[1][0].startswith('schtasks /Run /TN "AutoFluid_')
    assert calls[1][1] == 30
    assert calls[2][0].startswith('schtasks /Change /TN "AutoFluid_')
    assert calls[2][0].endswith('" /DISABLE')
    assert calls[2][1] == 30
    ensure_dir.assert_any_call("D:/flags")
    assert len(written) == 1
    script_path, script_content = written[0]
    assert script_path.startswith("D:/flags/autofluid_bg_")
    assert r'set "AF_PID_FILE=D:\flags\autofluid_bg_' in script_content
    assert command in _decode_encoded_command(script_content)
    assert r'echo done > "D:\flags\task done.flag"' in script_content
    assert 'schtasks /Delete /TN "AutoFluid_' in script_content
    assert deleted == [flag_file, f"{flag_file}.error"]


def test_exec_background_interactive_creates_interactive_scheduled_task():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    calls: list[tuple[str, int]] = []
    written: list[tuple[str, str]] = []

    def fake_exec(command: str, timeout: int = 30):
        calls.append((command, timeout))
        return ("", "", 0)

    with patch.object(host, "ensure_connected", return_value=True):
        with patch.object(host, "exec_command", side_effect=fake_exec):
            with patch.object(host, "delete_remote_file", return_value=True):
                with patch.object(host, "_ensure_remote_dir"):
                    with patch.object(
                        host,
                        "_write_remote_text_file",
                        side_effect=lambda path, content: written.append((path, content)),
                    ):
                        result, task_name = host.exec_background(
                            r"conda run python meshing.py",
                            r"D:/flags/job.done",
                            interactive=True,
                        )

    assert result is True
    assert calls[0][0].endswith(" /IT")
    assert calls[1][0].startswith('schtasks /Run /TN "AutoFluid_')
    assert calls[2][0].startswith('schtasks /Change /TN "AutoFluid_')
    assert calls[2][0].endswith('" /DISABLE')
    assert "call conda run python meshing.py" in written[0][1]
    assert "Start-Process" not in written[0][1]
    assert host._task_pid_files[task_name].startswith("D:/flags/autofluid_bg_")
    assert host._task_pid_files[task_name].endswith(".pid")


def test_exec_background_deletes_created_task_when_run_fails():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    calls: list[tuple[str, int]] = []

    def fake_exec(command: str, timeout: int = 30):
        calls.append((command, timeout))
        if command.startswith('schtasks /Run /TN "AutoFluid_'):
            return ("", "run failed", 1)
        return ("", "", 0)

    with patch.object(host, "ensure_connected", return_value=True):
        with patch.object(host, "exec_command", side_effect=fake_exec):
            with patch.object(host, "delete_remote_file", return_value=True):
                with patch.object(host, "_ensure_remote_dir"):
                    with patch.object(host, "_write_remote_text_file"):
                        result, task_name = host.exec_background(
                            r"conda run python meshing.py",
                            r"D:/flags/job.done",
                            interactive=True,
                        )

    assert result is False
    assert task_name.startswith("AutoFluid_")
    assert calls[-1] == (f'schtasks /Delete /TN "{task_name}" /F', 15)


def test_solver_background_task_is_disabled_after_run():
    """Solver 计划任务启动后立即禁用，避免跨 23:59 二次触发。"""
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    calls: list[tuple[str, int]] = []
    written: list[tuple[str, str]] = []

    def fake_exec(command: str, timeout: int = 30):
        calls.append((command, timeout))
        return ("", "", 0)

    with patch.object(host, "ensure_connected", return_value=True):
        with patch.object(host, "exec_command", side_effect=fake_exec):
            with patch.object(host, "delete_remote_file", return_value=True):
                with patch.object(host, "_ensure_remote_dir"):
                    with patch.object(
                        host,
                        "_write_remote_text_file",
                        side_effect=lambda path, content: written.append((path, content)),
                    ):
                        result, _ = host.exec_background(
                            r"conda run -n pyfluent python batch_solver_gen4.py 7",
                            r"D:/flags/solver_done_7.txt",
                            working_dir=r"D:\xkz_1020\workingdir",
                            interactive=True,
                        )

    assert result is True
    assert calls[0][0].startswith('schtasks /Create /TN "AutoFluid_')
    assert calls[0][0].endswith(" /IT")
    assert calls[1][0].startswith('schtasks /Run /TN "AutoFluid_')
    assert calls[2][0].startswith('schtasks /Change /TN "AutoFluid_')
    assert calls[2][0].endswith('" /DISABLE')
    assert "call conda run -n pyfluent python batch_solver_gen4.py 7" in written[0][1]
    assert r'cd /d "D:\xkz_1020\workingdir"' in written[0][1]
    assert 'schtasks /Delete /TN "AutoFluid_' in written[0][1]


def test_wait_for_flag_returns_false_immediately_when_error_flag_exists():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    flag_file = r"D:/flags/job.done"
    checked: list[str] = []
    deleted: list[str] = []

    def fake_check(remote_path: str) -> bool:
        checked.append(remote_path)
        return remote_path == f"{flag_file}.error"

    with patch.object(host, "check_remote_file", side_effect=fake_check):
        with patch.object(
            host,
            "delete_remote_file",
            side_effect=lambda path: deleted.append(path) or True,
        ):
            with patch(
                "utils.ssh_client.time.sleep",
                side_effect=AssertionError("error flag should stop polling immediately"),
            ):
                assert host.wait_for_flag(flag_file, timeout=60, poll_interval=10) is False

    assert checked == [f"{flag_file}.error"]
    assert deleted == [f"{flag_file}.error"]


def test_kill_remote_task_kills_recorded_child_pid():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    host._task_pid_files["AutoFluid_job"] = r"D:/flags/autofluid_bg_job.pid"
    calls: list[tuple[str, int]] = []
    deleted: list[str] = []

    def fake_exec(command: str, timeout: int = 30):
        calls.append((command, timeout))
        if command == r'cmd /c type "D:\flags\autofluid_bg_job.pid"':
            return ("4321\r\n", "", 0)
        return ("", "", 0)

    with patch.object(host, "ensure_connected", return_value=True):
        with patch.object(host, "exec_command", side_effect=fake_exec):
            with patch.object(
                host,
                "delete_remote_file",
                side_effect=lambda path: deleted.append(path) or True,
            ):
                assert host.kill_remote_task("AutoFluid_job") is True

    assert calls == [
        (r'cmd /c type "D:\flags\autofluid_bg_job.pid"', 15),
        ("taskkill /PID 4321 /T /F", 60),
        ('schtasks /End /TN "AutoFluid_job"', 15),
        ('schtasks /Delete /TN "AutoFluid_job" /F', 15),
    ]
    assert deleted == [r"D:/flags/autofluid_bg_job.pid"]


def test_cleanup_remote_task_entry_deletes_task_without_reading_pid():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    calls: list[tuple[str, int]] = []
    deleted: list[str] = []

    def fake_exec(command: str, timeout: int = 30):
        calls.append((command, timeout))
        return ("", "", 0)

    with patch.object(host, "ensure_connected", return_value=True):
        with patch.object(host, "exec_command", side_effect=fake_exec):
            with patch.object(
                host,
                "_read_remote_pid_file",
                side_effect=AssertionError("completed cleanup must not read pid"),
            ):
                with patch.object(
                    host,
                    "delete_remote_file",
                    side_effect=lambda path: deleted.append(path) or True,
                ):
                    assert host.cleanup_remote_task_entry(
                        "AutoFluid_done",
                        r"D:/flags/autofluid_bg_done.pid",
                    ) is True

    assert calls == [('schtasks /Delete /TN "AutoFluid_done" /F', 15)]
    assert deleted == [r"D:/flags/autofluid_bg_done.pid"]


def test_upload_file_applies_sftp_channel_timeout():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    timeouts: list[float | None] = []

    class _Channel:
        def settimeout(self, timeout):
            timeouts.append(timeout)

    class _SFTP:
        def __init__(self) -> None:
            self.channel = _Channel()
            self.uploads: list[tuple[str, str]] = []

        def get_channel(self):
            return self.channel

        def stat(self, remote_path: str):
            return object()

        def put(self, local_path: str, remote_path: str, callback=None):
            if callback is not None:
                callback(4, 4)
            self.uploads.append((local_path, remote_path))

    sftp = _SFTP()
    host._sftp = sftp

    with patch.object(host, "ensure_connected", return_value=True):
        assert host.upload_file("local.scdoc", "D:/remote/model.scdoc", timeout=9) is True

    assert sftp.uploads == [("local.scdoc", "D:/remote/model.scdoc")]
    assert timeouts[0] == pytest.approx(9, abs=1e-3)
    assert timeouts[1] is None


def test_upload_file_uses_timeout_as_total_retry_budget(monkeypatch):
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    timeouts: list[float | None] = []

    class _Channel:
        def settimeout(self, timeout):
            timeouts.append(timeout)

    class _SFTP:
        def __init__(self) -> None:
            self.channel = _Channel()
            self.attempts = 0

        def get_channel(self):
            return self.channel

        def stat(self, remote_path: str):
            return object()

        def put(self, local_path: str, remote_path: str, callback=None):
            self.attempts += 1
            if self.attempts == 1:
                raise socket.timeout("first attempt timed out")

    host._sftp = _SFTP()
    monotonic_values = iter([100.0, 100.0, 105.0, 105.0])
    monkeypatch.setattr(
        ssh_client_module.time,
        "monotonic",
        lambda: next(monotonic_values),
    )
    monkeypatch.setattr(ssh_client_module.time, "sleep", lambda seconds: None)

    with patch.object(host, "ensure_connected", return_value=True):
        with patch.object(host, "disconnect", return_value=None):
            assert host.upload_file(
                "local.scdoc",
                "D:/remote/model.scdoc",
                max_retries=2,
                timeout=9,
            ) is True

    assert timeouts == [9, None, 4, None]


def test_upload_file_applies_timeout_to_remote_directory_check():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    timeouts: list[float | None] = []

    class _Channel:
        def settimeout(self, timeout):
            timeouts.append(timeout)

    class _SFTP:
        def __init__(self) -> None:
            self.channel = _Channel()
            self.put_called = False

        def get_channel(self):
            return self.channel

        def stat(self, remote_path: str):
            assert remote_path == "D:/remote"
            raise socket.timeout("remote dir stat hung")

        def put(self, local_path: str, remote_path: str, callback=None):
            self.put_called = True

    sftp = _SFTP()
    host._sftp = sftp

    with patch.object(host, "ensure_connected", return_value=True):
        with patch.object(host, "disconnect", return_value=None):
            assert host.upload_file(
                "local.scdoc",
                "D:/remote/model.scdoc",
                max_retries=1,
                timeout=9,
            ) is False

    assert sftp.put_called is False
    assert timeouts[0] == pytest.approx(9, abs=1e-3)
    assert timeouts[1] is None


def test_get_remote_file_size_applies_sftp_channel_timeout():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    timeouts: list[float | None] = []

    class _StatResult:
        st_size = 42

    class _Channel:
        def settimeout(self, timeout):
            timeouts.append(timeout)

    class _SFTP:
        def __init__(self) -> None:
            self.channel = _Channel()

        def get_channel(self):
            return self.channel

        def stat(self, remote_path: str):
            assert remote_path == "D:/remote/model.scdoc"
            return _StatResult()

    host._sftp = _SFTP()

    with patch.object(host, "ensure_connected", return_value=True):
        assert host.get_remote_file_size("D:/remote/model.scdoc", timeout=11) == 42

    assert timeouts == [11, None]


def test_get_remote_file_size_returns_none_when_sftp_stat_times_out():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    timeouts: list[float | None] = []

    class _Channel:
        def settimeout(self, timeout):
            timeouts.append(timeout)

    class _SFTP:
        def __init__(self) -> None:
            self.channel = _Channel()

        def get_channel(self):
            return self.channel

        def stat(self, remote_path: str):
            raise socket.timeout("hung stat")

    host._sftp = _SFTP()

    with patch.object(host, "ensure_connected", return_value=True):
        assert host.get_remote_file_size("D:/remote/model.scdoc", timeout=11) is None

    assert timeouts == [11, None]


def test_read_remote_text_file_applies_timeout_and_decodes_utf8():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    timeouts: list[float | None] = []

    class _Channel:
        def settimeout(self, timeout):
            timeouts.append(timeout)

    class _RemoteFile:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return "剩余时间".encode("utf-8")

    class _SFTP:
        def __init__(self) -> None:
            self.channel = _Channel()
            self.opened: list[tuple[str, str]] = []

        def get_channel(self):
            return self.channel

        def open(self, remote_path: str, mode: str):
            self.opened.append((remote_path, mode))
            return _RemoteFile()

    sftp = _SFTP()
    host._sftp = sftp

    with patch.object(host, "ensure_connected", return_value=True):
        assert host.read_remote_text_file("D:/flags/progress.json", timeout=11) == "剩余时间"

    assert sftp.opened == [("D:/flags/progress.json", "rb")]
    assert timeouts == [11, None]


def test_get_remote_file_hashes_uses_cmd_batch_and_parses_certutil_output():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    written: list[tuple[str, str]] = []
    deleted: list[str] = []
    commands: list[tuple[str, int]] = []

    def fake_exec(command: str, timeout: int = 30):
        commands.append((command, timeout))
        return (
            "__AF_HASH_BEGIN__0\r\n"
            "MD5 的 D:/remote/alpha.txt 哈希:\r\n"
            "0123456789ABCDEF0123456789ABCDEF\r\n"
            "CertUtil: -hashfile 命令成功完成。\r\n"
            "__AF_HASH_END__0\r\n"
            "__AF_HASH_MISSING__1\r\n",
            "",
            0,
        )

    with patch.object(host, "ensure_connected", return_value=True):
        with patch.object(host, "exec_command", side_effect=fake_exec):
            with patch.object(
                host,
                "_write_remote_text_file",
                side_effect=lambda path, content: written.append((path, content)),
            ):
                with patch.object(
                    host,
                    "delete_remote_file",
                    side_effect=lambda path: deleted.append(path) or True,
                ):
                    hashes = host.get_remote_file_hashes(
                        r"D:\remote", ["alpha.txt", "missing.txt"]
                    )

    assert hashes == {
        "alpha.txt": "0123456789abcdef0123456789abcdef",
        "missing.txt": None,
    }
    assert len(written) == 1
    script_path, script_content = written[0]
    assert script_path.startswith("C:/Windows/Temp/_af_hash_files_")
    assert script_path.endswith(".bat")
    assert deleted == [script_path]
    cmd_script_path = script_path.replace("/", "\\")
    assert commands == [(f'cmd /c "{cmd_script_path}"', 60)]
    assert "powershell" not in commands[0][0].lower()
    assert 'certutil -hashfile "D:/remote/alpha.txt" MD5' in script_content
    assert '__AF_HASH_MISSING__1' in script_content


def test_get_remote_file_hashes_raises_when_cmd_batch_fails_and_cleans_up():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    written: list[str] = []
    deleted: list[str] = []

    with patch.object(host, "ensure_connected", return_value=True):
        with patch.object(host, "exec_command", return_value=("", "access denied", 1)):
            with patch.object(
                host,
                "_write_remote_text_file",
                side_effect=lambda path, content: written.append(path),
            ):
                with patch.object(
                    host,
                    "delete_remote_file",
                    side_effect=lambda path: deleted.append(path) or True,
                ):
                    with pytest.raises(ConnectionError, match="批量获取远程文件哈希失败"):
                        host.get_remote_file_hashes(r"D:\remote", ["alpha.txt"])

    assert len(written) == 1
    assert deleted == written


def test_get_remote_file_hashes_raises_when_cmd_batch_output_is_invalid():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")

    with patch.object(host, "ensure_connected", return_value=True):
        with patch.object(
            host,
            "exec_command",
            return_value=("__AF_HASH_BEGIN__0\r\ninvalid\r\n__AF_HASH_END__0\r\n", "", 0),
        ):
            with patch.object(host, "_write_remote_text_file"):
                with patch.object(host, "delete_remote_file", return_value=True):
                    with pytest.raises(ConnectionError, match="批量获取远程文件哈希失败"):
                        host.get_remote_file_hashes(r"D:\remote", ["alpha.txt"])


def test_get_remote_combined_file_hash_uses_cmd_batch_hashes():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    expected = hashlib.md5(
        b"alpha.txt:0123456789abcdef0123456789abcdef|missing.txt:MISSING"
    ).hexdigest()

    with patch.object(
        host,
        "_get_remote_file_hashes_via_cmd",
        return_value={
            "alpha.txt": "0123456789abcdef0123456789abcdef",
            "missing.txt": None,
        },
    ) as batch_hashes:
        combined = host.get_remote_combined_file_hash(
            r"D:\remote", ["alpha.txt", "missing.txt"]
        )

    assert combined == expected
    batch_hashes.assert_called_once_with(r"D:\remote", ["alpha.txt", "missing.txt"])
