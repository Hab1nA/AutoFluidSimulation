import base64
from unittest.mock import patch

from utils.ssh_client import RemoteWorkstation

_EXPECTED_PS_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def test_build_background_ps_script_escapes_single_quotes():
    script = RemoteWorkstation._build_background_ps_script(
        "echo O'Brien",
        r"D:\flags\job's.done",
    )
    assert "$c = 'echo O''Brien'" in script
    assert "$f = 'D:\\flags\\job''s.done'" in script
    assert '\\"' not in script


def test_exec_background_generates_valid_encoded_command_with_spaces_and_quotes():
    host = RemoteWorkstation("127.0.0.1", 22, "user", "pwd")
    calls: list[tuple[str, int]] = []

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
                assert host.exec_background(command, flag_file) is True
    assert len(calls) == 1  # 仅主命令，标志文件清理已改用 SFTP

    launch_cmd = calls[0][0]
    assert launch_cmd.startswith(f'"{_EXPECTED_PS_EXE}" -NoProfile -EncodedCommand ')
    encoded = launch_cmd.split(" -EncodedCommand ", 1)[1]
    ps_script = base64.b64decode(encoded).decode("utf-16-le")
    assert "$inner = '\"{0} && echo done > \"\"{1}\"\"\"' -f $c, $f" in ps_script
    assert '\\"' not in ps_script
