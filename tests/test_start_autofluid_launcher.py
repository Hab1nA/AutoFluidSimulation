from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_ROOT / "start_autofluid.bat"


def test_default_client_launch_uses_windows_terminal_new_window() -> None:
    content = LAUNCHER.read_text(encoding="utf-8")

    assert 'where wt.exe' in content
    assert 'wt.exe -w new new-tab --title "AutoFluid Client"' not in content
    assert (
        'wt.exe -w -1 nt --title "AutoFluid Client" --startingDirectory "%PROJECT_DIR%" '
        '"%PS_EXE%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%CLIENT_PS1%"'
    ) in content


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
