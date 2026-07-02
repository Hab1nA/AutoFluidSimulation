from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "download_metrics_results.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "download_metrics_results_under_test",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeSsh:
    def __init__(self, *, entries: list[str], available: set[str]) -> None:
        self.entries = entries
        self.available = available
        self.downloads: list[tuple[str, str]] = []
        self.connected = False
        self.disconnected = False

    def connect(self) -> bool:
        self.connected = True
        return True

    def disconnect(self) -> None:
        self.disconnected = True

    def list_remote_directory(self, remote_dir: str) -> list[str]:
        assert remote_dir == "D:/metrics"
        return self.entries

    def check_remote_file(self, remote_path: str, **_: Any) -> bool:
        return remote_path in self.available

    def download_file(self, remote_path: str, local_path: str, **_: Any) -> bool:
        self.downloads.append((remote_path, local_path))
        Path(local_path).write_text("config_id,metric\n7,1.0\n", encoding="utf-8")
        return True


def test_parse_config_id_from_model_gen4_directory() -> None:
    module = _load_module()

    assert module.parse_config_id("model_gen4_7") == "7"
    assert module.parse_config_id("model_gen4_012") == "012"
    assert module.parse_config_id("model_gen4_bad") is None
    assert module.parse_config_id("other_7") is None


def test_collect_metrics_results_from_config_directories() -> None:
    module = _load_module()
    ssh = _FakeSsh(
        entries=["model_gen4_7", "logs", "model_gen4_9"],
        available={"D:/metrics/model_gen4_7/model_gen4_7.csv"},
    )

    summaries = module.collect_metrics_results(ssh, "D:\\metrics")

    assert summaries == [
        module.RemoteMetricsCsv("7", "D:/metrics/model_gen4_7/model_gen4_7.csv")
    ]


def test_default_ssh_factory_accepts_workstation_connection_kwargs(monkeypatch) -> None:
    module = _load_module()
    captured: dict[str, Any] = {}

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(module, "RemoteWorkstation", _Client)

    client = module._make_ssh_client(
        host="127.0.0.1",
        port=2222,
        username="ps",
        password="",
        key_filename=None,
        auth_method="none",
    )

    assert isinstance(client, _Client)
    assert captured == {
        "host": "127.0.0.1",
        "port": 2222,
        "username": "ps",
        "password": "",
        "key_filename": None,
        "auth_method": "none",
    }


def test_download_workstation_metrics_keeps_remote_result_filename_and_skips_existing(
    tmp_path: Path,
) -> None:
    module = _load_module()
    existing = tmp_path / "model_gen4_9.csv"
    existing.write_text("old", encoding="utf-8")
    ssh = _FakeSsh(
        entries=["model_gen4_7", "model_gen4_9"],
        available={
            "D:/metrics/model_gen4_7/model_gen4_7.csv",
            "D:/metrics/model_gen4_9/model_gen4_9.csv",
        },
    )

    results = module.download_workstation_metrics(
        {
            "id": "WS-A",
            "host": "127.0.0.1",
            "port": 22,
            "username": "ps",
            "password": "",
            "postprocess_metrics_dir": r"D:\metrics",
        },
        tmp_path,
        ssh_factory=lambda **_: ssh,
        overwrite=False,
    )

    assert [(result.config_id, result.status) for result in results] == [
        ("7", "downloaded"),
        ("9", "skipped_exists"),
    ]
    assert ssh.downloads == [
        (
            "D:/metrics/model_gen4_7/model_gen4_7.csv",
            str(tmp_path / "model_gen4_7.csv"),
        )
    ]
    assert ssh.connected is True
    assert ssh.disconnected is True
