import base64

from utils.ssh_client import RemoteWorkstation, _PS_EXE


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
        calls.append((command, timeout))
        return ("", "", 0)

    host.ensure_connected = lambda: True  # type: ignore[method-assign]
    host.exec_command = fake_exec  # type: ignore[method-assign]

    command = (
        r'call "C:\Program Files\Anaconda3\Scripts\activate.bat" '
        r'&& python "D:\work dir\run.py" --msg "hello world"'
    )
    flag_file = r"D:\flags\task done.flag"

    assert host.exec_background(command, flag_file) is True
    assert len(calls) == 2

    launch_cmd = calls[1][0]
    assert launch_cmd.startswith(f'"{_PS_EXE}" -NoProfile -EncodedCommand ')
    encoded = launch_cmd.split(" -EncodedCommand ", 1)[1]
    ps_script = base64.b64decode(encoded).decode("utf-16-le")
    assert "$inner = '\"{0} && echo done > \"\"{1}\"\"\"' -f $c, $f" in ps_script
    assert '\\"' not in ps_script
