from __future__ import annotations

import csv
import importlib.util
from pathlib import Path
import sys
import types
from typing import Any


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "export_residual_curves.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "export_residual_curves_under_test",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeSsh:
    def __init__(
        self,
        *,
        entries: list[str],
        available: set[str],
        failing_probe_prefixes: tuple[str, ...] = (),
    ) -> None:
        self.entries = entries
        self.available = available
        self.failing_probe_prefixes = failing_probe_prefixes
        self.downloads: list[tuple[str, str]] = []
        self.commands: list[str] = []
        self.probes: list[str] = []
        self.connected = False
        self.disconnected = False

    def connect(self) -> bool:
        self.connected = True
        return True

    def disconnect(self) -> None:
        self.disconnected = True

    def list_remote_directory(self, remote_dir: str) -> list[str]:
        assert remote_dir == "D:/case"
        return self.entries

    def check_remote_file(self, remote_path: str, **_: Any) -> bool:
        return remote_path in self.available

    def exec_command(self, command: str, **_: Any) -> tuple[str, str, int]:
        self.commands.append(command)
        if "import h5py" in command and "import matplotlib" in command:
            self.probes.append(command)
            if command.startswith(self.failing_probe_prefixes):
                return ("", "missing h5py: No module named 'h5py'", 1)
            return ("deps ok", "", 0)
        return ("export ok", "", 0)

    def download_file(self, remote_path: str, local_path: str, **_: Any) -> bool:
        self.downloads.append((remote_path, local_path))
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(b"png" if local_path.endswith(".png") else b"csv")
        return True


class _FakeArray:
    def __init__(self, values: list[Any]) -> None:
        self.values = values
        self.ndim = 2 if values and isinstance(values[0], list) else 1
        self.shape = (
            (len(values), len(values[0]))
            if self.ndim == 2 and values
            else (len(values),)
        )

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, tuple):
            rows, column = key
            selected_rows = self.values[rows]
            return [row[column] for row in selected_rows]
        if key == slice(None) and self.ndim == 2:
            return self
        return self.values[key]


class _FakeH5File:
    def __init__(self, *_: Any) -> None:
        self.store = {
            "results/residuals/phase-1": {
                "continuity": {
                    "iterations": _FakeArray([1.0, 2.0, 3.0]),
                    "data": _FakeArray(
                        [
                            [10.0, 10.0, 0.0, 0.0],
                            [5.0, 10.0, 0.0, 0.0],
                            [2.0, 10.0, 0.0, 0.0],
                        ]
                    ),
                },
                "x-velocity": {
                    "iterations": _FakeArray([1.0, 2.0, 3.0]),
                    "data": _FakeArray(
                        [
                            [100000.0, 1000000.0, 0.0, 0.0],
                            [50000.0, 1000000.0, 0.0, 0.0],
                            [1.0, 0.0, 0.0, 0.0],
                        ]
                    ),
                },
            }
        }

    def __enter__(self) -> "_FakeH5File":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def get(self, key: str) -> Any:
        return self.store.get(key)


def test_collect_solver_data_files_from_result_directory() -> None:
    module = _load_module()
    ssh = _FakeSsh(
        entries=[
            "model_gen4_7.dat.h5",
            "model_gen4_7.cas.h5",
            "model_gen4_8.dat.h5",
            "notes.txt",
        ],
        available={
            "D:/case/model_gen4_7.dat.h5",
            "D:/case/model_gen4_7.cas.h5",
            "D:/case/model_gen4_8.dat.h5",
        },
    )

    results = module.collect_solver_data_files(ssh, r"D:\case")

    assert results == [
        module.RemoteSolverDataFile("7", "D:/case/model_gen4_7.dat.h5"),
        module.RemoteSolverDataFile("8", "D:/case/model_gen4_8.dat.h5"),
    ]


def test_remote_export_code_avoids_future_annotations_for_old_workstation_python() -> None:
    module = _load_module()

    assert "from __future__ import annotations" not in module._remote_export_code()


def test_remote_export_code_saves_png_with_string_path_for_old_matplotlib() -> None:
    module = _load_module()

    assert "plt.savefig(str(png))" in module._remote_export_code()


def test_remote_export_code_writes_scaled_residuals_to_csv(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module = _load_module()
    fake_h5py = types.SimpleNamespace(File=_FakeH5File)

    class _FakePyplot:
        def figure(self, **_: Any) -> None: ...
        def plot(self, *_: Any, **__: Any) -> None: ...
        def yscale(self, *_: Any, **__: Any) -> None: ...
        def xlabel(self, *_: Any, **__: Any) -> None: ...
        def ylabel(self, *_: Any, **__: Any) -> None: ...
        def title(self, *_: Any, **__: Any) -> None: ...
        def grid(self, *_: Any, **__: Any) -> None: ...
        def legend(self, *_: Any, **__: Any) -> None: ...
        def tight_layout(self) -> None: ...

        def savefig(self, path: str) -> None:
            Path(path).write_bytes(b"png")

        def close(self) -> None: ...

    fake_pyplot = _FakePyplot()
    fake_matplotlib = types.ModuleType("matplotlib")
    fake_matplotlib.use = lambda *_: None  # type: ignore[attr-defined]
    fake_pyplot_module = types.ModuleType("matplotlib.pyplot")
    for name in (
        "figure",
        "plot",
        "yscale",
        "xlabel",
        "ylabel",
        "title",
        "grid",
        "legend",
        "tight_layout",
        "savefig",
        "close",
    ):
        setattr(fake_pyplot_module, name, getattr(fake_pyplot, name))
    monkeypatch.setitem(sys.modules, "h5py", fake_h5py)
    monkeypatch.setitem(sys.modules, "matplotlib", fake_matplotlib)
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", fake_pyplot_module)
    namespace: dict[str, Any] = {}
    exec(module._remote_export_code(), namespace)

    csv_path = tmp_path / "residuals.csv"
    namespace["main"](
        str(tmp_path / "model_gen4_1.dat.h5"),
        str(tmp_path / "residuals.png"),
        str(csv_path),
        "1",
    )

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert [row["continuity"] for row in rows] == ["1.0", "0.5", "0.2"]
    assert [row["x_velocity"] for row in rows] == ["0.1", "0.05", "1.0"]


def test_download_workstation_residual_curves_falls_back_to_conda_python(
    tmp_path: Path,
) -> None:
    module = _load_module()
    ssh = _FakeSsh(
        entries=["model_gen4_7.dat.h5"],
        available={"D:/case/model_gen4_7.dat.h5"},
        failing_probe_prefixes=(
            "python -c",
            r"C:\ProgramData\anaconda3\envs\pyfluent\python.exe -c",
        ),
    )

    results = module.download_workstation_residual_curves(
        {
            "id": "WS-B",
            "host": "127.0.0.1",
            "port": 22,
            "username": "ps",
            "password": "",
            "result_dir": r"D:\case",
            "conda_exe": r"C:\ProgramData\anaconda3\Scripts\conda.exe",
            "conda_env": "pyfluent",
        },
        tmp_path,
        ssh_factory=lambda **_: ssh,
    )

    assert [(result.config_id, result.status) for result in results] == [
        ("7", "downloaded"),
    ]
    assert len(ssh.probes) == 3
    assert ssh.probes[0].startswith("python -c")
    assert ssh.probes[2].startswith(r"C:\ProgramData\anaconda3\Scripts\conda.exe run -n pyfluent python -c")
    assert ssh.commands[-1].startswith(r"C:\ProgramData\anaconda3\Scripts\conda.exe run -n pyfluent python -c")


def test_download_workstation_residual_curves_runs_remote_export_and_downloads_outputs(
    tmp_path: Path,
) -> None:
    module = _load_module()
    ssh = _FakeSsh(
        entries=["model_gen4_7.dat.h5"],
        available={"D:/case/model_gen4_7.dat.h5"},
    )

    results = module.download_workstation_residual_curves(
        {
            "id": "WS-A",
            "host": "127.0.0.1",
            "port": 22,
            "username": "ps",
            "password": "",
            "result_dir": r"D:\case",
        },
        tmp_path,
        ssh_factory=lambda **_: ssh,
    )

    assert [(result.config_id, result.status) for result in results] == [
        ("7", "downloaded"),
    ]
    assert ssh.commands[0].startswith('python -c "')
    assert "import h5py" in ssh.commands[0]
    assert ssh.commands[1].startswith('python -c "')
    assert "base64.b64decode" in ssh.commands[1]
    assert ssh.downloads == [
        (
            "D:/case/residual_exports/model_gen4_7_residuals.png",
            str(tmp_path / "residual_curves" / "WS-A" / "model_gen4_7_residuals.png"),
        ),
        (
            "D:/case/residual_exports/model_gen4_7_residuals.csv",
            str(tmp_path / "residual_curves" / "WS-A" / "model_gen4_7_residuals.csv"),
        ),
    ]
    assert ssh.connected is True
    assert ssh.disconnected is True
