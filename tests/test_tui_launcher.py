from __future__ import annotations

from utils.tui_launcher import find_rust_tui_binary


def test_find_rust_tui_binary_uses_single_tui_binary(tmp_path, monkeypatch) -> None:
    project_dir = tmp_path
    release_dir = project_dir / "autofluid-tui" / "target" / "release"
    release_dir.mkdir(parents=True)
    tui_bin = release_dir / "autofluid-tui.exe"
    tui_bin.write_text("", encoding="utf-8")
    monkeypatch.delenv("WT_SESSION", raising=False)

    assert find_rust_tui_binary(str(project_dir)) == str(tui_bin)


def test_find_rust_tui_binary_ignores_removed_console_binary_name(
    tmp_path, monkeypatch
) -> None:
    project_dir = tmp_path
    release_dir = project_dir / "autofluid-tui" / "target" / "release"
    release_dir.mkdir(parents=True)
    removed_console_bin = release_dir / "autofluid-tui-console.exe"
    removed_console_bin.write_text("", encoding="utf-8")
    monkeypatch.setenv("WT_SESSION", "test-session")

    assert find_rust_tui_binary(str(project_dir)) is None
