import hashlib
from unittest.mock import patch

import pytest

from utils.ssh_client import RemoteWorkstation


def test_build_background_cmd_script_writes_done_or_error_flag():
    script = RemoteWorkstation._build_background_cmd_script(
        r'"C:\ProgramData\anaconda3\Scripts\conda.exe" run -n pyfluent python run.py',
        r"D:/flags/job.done",
        r"D:/flags/job.log",
    )
    assert r'>> "D:\flags\job.log" 2>&1' in script
    assert r'echo done > "D:\flags\job.done"' in script
    assert r'echo error %AF_EXIT% > "D:\flags\job.done.error"' in script
    # working_dir 未指定时不应包含 cd /d
    assert "cd /d" not in script


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
            with patch.object(host, "delete_remote_file", return_value=True):
                with patch.object(host, "_ensure_remote_dir") as ensure_dir:
                    with patch.object(
                        host,
                        "_write_remote_text_file",
                        side_effect=lambda path, content: written.append((path, content)),
                    ):
                        result, task_name = host.exec_background(command, flag_file)
                        assert result is True
                        assert task_name.startswith("AutoFluid_")

    assert len(calls) == 2
    assert calls[0][0].startswith('schtasks /Create /TN "AutoFluid_')
    assert calls[0][1] == 30
    assert calls[1][0].startswith('schtasks /Run /TN "AutoFluid_')
    assert calls[1][1] == 30
    ensure_dir.assert_any_call("D:/flags")
    assert len(written) == 1
    script_path, script_content = written[0]
    assert script_path.startswith("D:/flags/autofluid_bg_")
    assert command in script_content
    assert r'echo done > "D:\flags\task done.flag"' in script_content
    assert 'schtasks /Delete /TN "AutoFluid_' in script_content


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
    assert timeouts == [9, None]


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
    assert commands == [(f'cmd /c "{script_path.replace("/", "\\")}"', 60)]
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
