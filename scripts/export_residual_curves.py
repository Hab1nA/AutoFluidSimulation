from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import NamedTuple, Protocol


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import WORKSTATIONS, reload_config_from_toml  # noqa: E402
from utils.ssh_client import RemoteWorkstation  # noqa: E402


DEFAULT_WORKSTATION_IDS = ("WS-A", "WS-B", "WS-C", "WS-D")
REMOTE_EXPORT_SUBDIR = "residual_exports"
LOCAL_EXPORT_SUBDIR = "residual_curves"


class ResidualSsh(Protocol):
    def connect(self) -> bool: ...

    def disconnect(self) -> None: ...

    def list_remote_directory(self, remote_dir: str) -> list[str]: ...

    def check_remote_file(
        self,
        remote_path: str,
        *,
        timeout: float | None = None,
        quiet: bool = False,
    ) -> bool: ...

    def exec_command(self, command: str, timeout: int = 30) -> tuple[str, str, int]: ...

    def download_file(
        self,
        remote_path: str,
        local_path: str,
        max_retries: int = 3,
        *,
        timeout: float | None = None,
    ) -> bool: ...


class RemoteSolverDataFile(NamedTuple):
    config_id: str
    remote_path: str


class ResidualExportResult(NamedTuple):
    workstation_id: str
    config_id: str
    remote_path: str
    local_png_path: str
    local_csv_path: str
    status: str
    detail: str = ""


SshFactory = Callable[..., ResidualSsh]


def _normalize_remote_path(value: object) -> str:
    return str(value or "").replace("\\", "/").rstrip("/")


def _workstation_port(workstation: Mapping[str, object]) -> int:
    raw_port = workstation.get("port", 22)
    try:
        return int(str(raw_port or 22))
    except ValueError:
        return 22


def parse_config_id(filename: str) -> str | None:
    match = re.fullmatch(r"model_gen4_(\d+)\.dat\.h5", filename.strip())
    if match is None:
        return None
    return match.group(1)


def _sort_data_files(results: Iterable[RemoteSolverDataFile]) -> list[RemoteSolverDataFile]:
    return sorted(results, key=lambda item: (int(item.config_id), item.config_id))


def collect_solver_data_files(
    ssh: ResidualSsh,
    result_dir: str,
) -> list[RemoteSolverDataFile]:
    normalized_result_dir = _normalize_remote_path(result_dir)
    results: list[RemoteSolverDataFile] = []
    for entry_name in ssh.list_remote_directory(normalized_result_dir):
        config_id = parse_config_id(entry_name)
        if config_id is None:
            continue
        remote_path = f"{normalized_result_dir}/model_gen4_{config_id}.dat.h5"
        if ssh.check_remote_file(remote_path, quiet=True):
            results.append(RemoteSolverDataFile(config_id, remote_path))
    return _sort_data_files(results)


def _make_ssh_client(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    key_filename: str | None,
    auth_method: str,
) -> ResidualSsh:
    return RemoteWorkstation(
        host=host,
        port=port,
        username=username,
        password=password,
        key_filename=key_filename,
        auth_method=auth_method,
    )


def _local_output_paths(
    downloads_dir: Path,
    workstation_id: str,
    config_id: str,
) -> tuple[Path, Path]:
    target_dir = downloads_dir / LOCAL_EXPORT_SUBDIR / workstation_id
    return (
        target_dir / f"model_gen4_{config_id}_residuals.png",
        target_dir / f"model_gen4_{config_id}_residuals.csv",
    )


def _remote_export_paths(result_dir: str, config_id: str) -> tuple[str, str, str]:
    export_dir = f"{_normalize_remote_path(result_dir)}/{REMOTE_EXPORT_SUBDIR}"
    return (
        export_dir,
        f"{export_dir}/model_gen4_{config_id}_residuals.png",
        f"{export_dir}/model_gen4_{config_id}_residuals.csv",
    )


def _quote_command_token(value: str) -> str:
    stripped = value.strip()
    if not stripped or stripped.startswith('"') or stripped.startswith("'"):
        return stripped
    if any(char.isspace() for char in stripped):
        return f'"{stripped}"'
    return stripped


def _remote_export_code() -> str:
    return r'''
import csv
import math
import sys
from pathlib import Path

try:
    import h5py
except ImportError as exc:
    raise SystemExit(f"missing h5py: {exc}") from exc

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError as exc:
    raise SystemExit(f"missing matplotlib: {exc}") from exc

VARIABLES = [
    "continuity",
    "x-velocity",
    "y-velocity",
    "z-velocity",
    "energy",
    "k",
    "omega",
    "fmean",
    "fvar",
]


def scaled_residual_values(data):
    raw_values = data[:, 0]
    scale_values = None
    if data.ndim == 2 and data.shape[1] > 1:
        scale_values = data[:, 1]

    values = []
    for index, raw_value in enumerate(raw_values):
        raw = float(raw_value)
        scaled = raw
        if scale_values is not None:
            scale = float(scale_values[index])
            if math.isfinite(scale) and scale > 0:
                scaled = raw / scale
        values.append(scaled)
    return values


def main(dat_path: str, png_path: str, csv_path: str, config_id: str) -> None:
    source = Path(dat_path)
    png = Path(png_path)
    csv_file = Path(csv_path)
    png.parent.mkdir(parents=True, exist_ok=True)
    csv_file.parent.mkdir(parents=True, exist_ok=True)
    series = {}
    with h5py.File(source, "r") as h5:
        base = h5.get("results/residuals/phase-1")
        if base is None:
            raise SystemExit(f"missing residual group: {source}")
        for variable in VARIABLES:
            if variable not in base:
                continue
            group = base[variable]
            iterations = group["iterations"][:]
            data = group["data"][:]
            if data.ndim != 2 or data.shape[1] < 1:
                continue
            name = variable.replace("-", "_")
            series[name] = (
                [float(value) for value in iterations],
                scaled_residual_values(data),
            )
    if not series:
        raise SystemExit(f"no residual series found: {source}")

    reference_iterations = next(iter(series.values()))[0]
    with csv_file.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = ["iteration", *series.keys()]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for index, iteration in enumerate(reference_iterations):
            row = {"iteration": iteration}
            for name, (_, values) in series.items():
                row[name] = values[index] if index < len(values) else ""
            writer.writerow(row)

    plt.figure(figsize=(10, 6), dpi=140)
    for name, (iterations, values) in series.items():
        xs = []
        ys = []
        for iteration, value in zip(iterations, values):
            if math.isfinite(value) and value > 0:
                xs.append(iteration)
                ys.append(value)
        if xs:
            plt.plot(xs, ys, linewidth=1.0, label=name)
    plt.yscale("log")
    plt.xlabel("Iteration")
    plt.ylabel("Scaled residual (data[:, 0] / data[:, 1], log scale)")
    plt.title(f"model_gen4_{config_id} residuals")
    plt.grid(True, which="both", alpha=0.25)
    plt.legend(fontsize=8, ncol=2)
    plt.tight_layout()
    plt.savefig(str(png))
    plt.close()
    print(f"exported {png} and {csv_file}")
'''


def _build_remote_export_command(
    *,
    python_command: str,
    dat_path: str,
    png_path: str,
    csv_path: str,
    config_id: str,
) -> str:
    code = base64.b64encode(_remote_export_code().encode("utf-8")).decode("ascii")
    payload = base64.b64encode(
        json.dumps(
            {
                "dat_path": dat_path,
                "png_path": png_path,
                "csv_path": csv_path,
                "config_id": config_id,
            },
            ensure_ascii=True,
        ).encode("utf-8")
    ).decode("ascii")
    launcher = (
        "import base64,json;"
        "ns={};"
        f"exec(base64.b64decode('{code}').decode('utf-8'),ns);"
        f"ns['main'](**json.loads(base64.b64decode('{payload}').decode('utf-8')))"
    )
    return f'{python_command} -c "{launcher}"'


def _build_remote_probe_command(python_command: str) -> str:
    return (
        f'{python_command} -c "import h5py; import matplotlib; '
        "matplotlib.use('Agg'); print('ok')\""
    )


def _remote_python_candidates(
    workstation: Mapping[str, object],
    requested_python: str,
) -> list[str]:
    requested = str(requested_python or "auto").strip()
    if requested and requested.lower() != "auto":
        return [requested]

    candidates: list[str] = []

    def add(candidate: str) -> None:
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    conda_exe = str(workstation.get("conda_exe") or "").strip()
    conda_env = str(workstation.get("conda_env") or "").strip()

    add("python")
    if conda_env:
        add(_quote_command_token(fr"C:\ProgramData\anaconda3\envs\{conda_env}\python.exe"))
    if conda_exe and conda_env:
        add(f"{_quote_command_token(conda_exe)} run -n {conda_env} python")
    if conda_env:
        add(f"conda run -n {conda_env} python")
    add("py -3")
    add(_quote_command_token(r"C:\ProgramData\anaconda3\python.exe"))
    return candidates


def _select_remote_python(
    ssh: ResidualSsh,
    workstation: Mapping[str, object],
    *,
    requested_python: str,
    timeout: int,
) -> tuple[str | None, str]:
    failures: list[str] = []
    for candidate in _remote_python_candidates(workstation, requested_python):
        stdout, stderr, exit_code = ssh.exec_command(
            _build_remote_probe_command(candidate),
            timeout=timeout,
        )
        if exit_code == 0:
            return candidate, stdout.strip()
        detail = (stderr or stdout or f"exit code {exit_code}").strip()
        failures.append(f"{candidate}: {detail}")
    return None, "; ".join(failures)


def download_workstation_residual_curves(
    workstation: Mapping[str, object],
    downloads_dir: Path,
    *,
    ssh_factory: SshFactory = _make_ssh_client,
    remote_python: str = "auto",
    overwrite: bool = False,
    dry_run: bool = False,
    command_timeout: int = 180,
) -> list[ResidualExportResult]:
    workstation_id = str(workstation.get("id") or "unknown")
    result_dir = _normalize_remote_path(workstation.get("result_dir"))
    if not result_dir:
        return [
            ResidualExportResult(
                workstation_id,
                "",
                "",
                "",
                "",
                "skipped_no_result_dir",
                "workstation has no result_dir",
            )
        ]

    ssh = ssh_factory(
        host=str(workstation.get("host", "")),
        port=_workstation_port(workstation),
        username=str(workstation.get("username", "")),
        password=str(workstation.get("password", "")),
        key_filename=str(workstation.get("key_filename") or "") or None,
        auth_method=str(workstation.get("auth_method") or "password"),
    )
    if not ssh.connect():
        return [
            ResidualExportResult(
                workstation_id,
                "",
                "",
                "",
                "",
                "connect_failed",
                f"failed to connect to {workstation.get('host')}:{workstation.get('port', 22)}",
            )
        ]

    results: list[ResidualExportResult] = []
    try:
        data_files = collect_solver_data_files(ssh, result_dir)
        if not data_files:
            results.append(
                ResidualExportResult(
                    workstation_id,
                    "",
                    "",
                    "",
                    "",
                    "skipped_empty",
                    f"no model_gen4_<config_id>.dat.h5 files under {result_dir}",
                )
            )
            return results

        selected_python: str | None = None
        if not dry_run:
            selected_python, python_detail = _select_remote_python(
                ssh,
                workstation,
                requested_python=remote_python,
                timeout=min(command_timeout, 60),
            )
            if selected_python is None:
                detail = f"no remote Python with h5py/matplotlib found: {python_detail}"
                return [
                    ResidualExportResult(
                        workstation_id,
                        "",
                        "",
                        "",
                        "",
                        "python_unavailable",
                        detail,
                    )
                ]

        for data_file in data_files:
            local_png_path, local_csv_path = _local_output_paths(
                downloads_dir,
                workstation_id,
                data_file.config_id,
            )
            if (
                local_png_path.exists()
                and local_csv_path.exists()
                and not overwrite
            ):
                results.append(
                    ResidualExportResult(
                        workstation_id,
                        data_file.config_id,
                        data_file.remote_path,
                        str(local_png_path),
                        str(local_csv_path),
                        "skipped_exists",
                    )
                )
                continue

            _, remote_png_path, remote_csv_path = _remote_export_paths(
                result_dir,
                data_file.config_id,
            )
            if dry_run:
                results.append(
                    ResidualExportResult(
                        workstation_id,
                        data_file.config_id,
                        data_file.remote_path,
                        str(local_png_path),
                        str(local_csv_path),
                        "dry_run",
                    )
                )
                continue

            command = _build_remote_export_command(
                python_command=selected_python or remote_python,
                dat_path=data_file.remote_path,
                png_path=remote_png_path,
                csv_path=remote_csv_path,
                config_id=data_file.config_id,
            )
            stdout, stderr, exit_code = ssh.exec_command(
                command,
                timeout=command_timeout,
            )
            if exit_code != 0:
                detail = (stderr or stdout or f"exit code {exit_code}").strip()
                results.append(
                    ResidualExportResult(
                        workstation_id,
                        data_file.config_id,
                        data_file.remote_path,
                        str(local_png_path),
                        str(local_csv_path),
                        "export_failed",
                        detail,
                    )
                )
                continue

            png_ok = ssh.download_file(remote_png_path, str(local_png_path))
            csv_ok = ssh.download_file(remote_csv_path, str(local_csv_path))
            results.append(
                ResidualExportResult(
                    workstation_id,
                    data_file.config_id,
                    data_file.remote_path,
                    str(local_png_path),
                    str(local_csv_path),
                    "downloaded" if png_ok and csv_ok else "download_failed",
                    "" if png_ok and csv_ok else "one or more output files failed to download",
                )
            )
    finally:
        ssh.disconnect()
    return results


def _selected_workstations(
    workstations: Iterable[Mapping[str, object]],
    workstation_ids: Iterable[str],
) -> list[Mapping[str, object]]:
    wanted = set(workstation_ids)
    return [
        workstation
        for workstation in workstations
        if str(workstation.get("id") or "") in wanted
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export residual curve PNG/CSV files from existing remote "
            "model_gen4_<config_id>.dat.h5 solver results into ~/Downloads."
        )
    )
    parser.add_argument(
        "--workstation",
        action="append",
        dest="workstations",
        help="Workstation id to include. Can be repeated. Defaults to WS-A/B/C/D.",
    )
    parser.add_argument(
        "--downloads-dir",
        type=Path,
        default=Path.home() / "Downloads",
        help="Local download directory. Defaults to ~/Downloads.",
    )
    parser.add_argument(
        "--remote-python",
        default="auto",
        help=(
            "Remote Python command with h5py and matplotlib available. "
            "Defaults to auto, which probes python and the workstation conda_env."
        ),
    )
    parser.add_argument(
        "--command-timeout",
        type=int,
        default=180,
        help="Timeout in seconds for each remote residual export command.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing local PNG/CSV files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List residual files that would be exported without running remote Python.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reload_config_from_toml()
    workstation_ids = tuple(args.workstations or DEFAULT_WORKSTATION_IDS)
    workstations = _selected_workstations(WORKSTATIONS, workstation_ids)
    found_ids = {str(workstation.get("id") or "") for workstation in workstations}
    missing_ids = [item for item in workstation_ids if item not in found_ids]

    all_results: list[ResidualExportResult] = []
    for workstation in workstations:
        all_results.extend(
            download_workstation_residual_curves(
                workstation,
                args.downloads_dir,
                remote_python=str(args.remote_python),
                overwrite=bool(args.overwrite),
                dry_run=bool(args.dry_run),
                command_timeout=int(args.command_timeout),
            )
        )

    for workstation_id in missing_ids:
        all_results.append(
            ResidualExportResult(
                workstation_id,
                "",
                "",
                "",
                "",
                "skipped_missing_config",
                "workstation id not found in autofluid_config.toml",
            )
        )

    for result in all_results:
        label = result.workstation_id
        if result.config_id:
            label = f"{label} config {result.config_id}"
        detail = f" ({result.detail})" if result.detail else ""
        target = ""
        if result.local_png_path:
            target = f" -> {result.local_png_path}"
        print(f"[{result.status}] {label}{target}{detail}")

    failures = {"connect_failed", "export_failed", "download_failed"}
    failures.add("python_unavailable")
    return 1 if any(result.status in failures for result in all_results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
