from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_ROOT / "start_autofluid.bat"


def test_default_client_launch_does_not_depend_on_windows_terminal() -> None:
    content = LAUNCHER.read_text(encoding="utf-8")

    assert 'where wt.exe' not in content
    assert 'wt.exe -w new new-tab --title "AutoFluid Client"' not in content
    assert '"%PS_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%CLIENT_PS1%"' in content


def test_worker_launch_uses_cmd_start_for_separate_console() -> None:
    content = LAUNCHER.read_text(encoding="utf-8")

    assert 'wt.exe -w new new-tab --title "AutoFluid LocalWorker"' not in content
    assert (
        'start "AutoFluid LocalWorker" /D "%PROJECT_DIR%" "%PS_EXE%" '
        '-NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%WORKER_PS1%"'
    ) in content


def test_worker_only_argument_enables_worker_launch() -> None:
    content = LAUNCHER.read_text(encoding="utf-8")

    assert 'if /I "%~1"=="--worker-only" (' in content
    assert 'set "START_CLIENT=0"\n    set "START_WORKER=1"' in content
