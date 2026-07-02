from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
import re
from typing import NamedTuple, Protocol


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import WORKSTATIONS, reload_config_from_toml  # noqa: E402
from executor.postprocess_paths import resolve_postprocess_paths  # noqa: E402
from utils.ssh_client import RemoteWorkstation  # noqa: E402


DEFAULT_WORKSTATION_IDS = ("WS-A", "WS-B", "WS-C", "WS-D")


class MetricsSsh(Protocol):
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

    def download_file(
        self,
        remote_path: str,
        local_path: str,
        max_retries: int = 3,
        *,
        timeout: float | None = None,
    ) -> bool: ...


class RemoteMetricsCsv(NamedTuple):
    config_id: str
    remote_path: str


class DownloadResult(NamedTuple):
    workstation_id: str
    config_id: str
    remote_path: str
    local_path: str
    status: str
    detail: str = ""


SshFactory = Callable[..., MetricsSsh]


def _normalize_remote_path(value: object) -> str:
    return str(value or "").replace("\\", "/").rstrip("/")


def _workstation_port(workstation: Mapping[str, object]) -> int:
    raw_port = workstation.get("port", 22)
    try:
        return int(str(raw_port or 22))
    except ValueError:
        return 22


def parse_config_id(dirname: str) -> str | None:
    match = re.fullmatch(r"model_gen4_(\d+)", dirname.strip())
    if match is None:
        return None
    return match.group(1)


def _sort_config_ids(results: Iterable[RemoteMetricsCsv]) -> list[RemoteMetricsCsv]:
    return sorted(results, key=lambda item: (int(item.config_id), item.config_id))


def collect_metrics_results(
    ssh: MetricsSsh,
    metrics_dir: str,
) -> list[RemoteMetricsCsv]:
    normalized_metrics_dir = _normalize_remote_path(metrics_dir)
    results: list[RemoteMetricsCsv] = []
    for entry_name in ssh.list_remote_directory(normalized_metrics_dir):
        config_id = parse_config_id(entry_name)
        if config_id is None:
            continue
        filename = f"model_gen4_{config_id}.csv"
        remote_path = f"{normalized_metrics_dir}/model_gen4_{config_id}/{filename}"
        if ssh.check_remote_file(remote_path, quiet=True):
            results.append(RemoteMetricsCsv(config_id, remote_path))
    return _sort_config_ids(results)


def _make_ssh_client(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    key_filename: str | None,
    auth_method: str,
) -> MetricsSsh:
    return RemoteWorkstation(
        host=host,
        port=port,
        username=username,
        password=password,
        key_filename=key_filename,
        auth_method=auth_method,
    )


def _local_metrics_path(downloads_dir: Path, config_id: str) -> Path:
    return downloads_dir / f"model_gen4_{config_id}.csv"


def download_workstation_metrics(
    workstation: Mapping[str, object],
    downloads_dir: Path,
    *,
    ssh_factory: SshFactory = _make_ssh_client,
    overwrite: bool = False,
    dry_run: bool = False,
) -> list[DownloadResult]:
    workstation_id = str(workstation.get("id") or "unknown")
    metrics_dir = resolve_postprocess_paths(workstation)["metrics_dir"]
    if not metrics_dir:
        return [
            DownloadResult(
                workstation_id,
                "",
                "",
                "",
                "skipped_no_metrics_dir",
                "workstation has no postprocess metrics directory",
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
    results: list[DownloadResult] = []
    if not ssh.connect():
        return [
            DownloadResult(
                workstation_id,
                "",
                "",
                "",
                "connect_failed",
                f"failed to connect to {workstation.get('host')}:{workstation.get('port', 22)}",
            )
        ]

    try:
        downloads_dir.mkdir(parents=True, exist_ok=True)
        metrics_results = collect_metrics_results(ssh, str(metrics_dir))
        if not metrics_results:
            results.append(
                DownloadResult(
                    workstation_id,
                    "",
                    "",
                    "",
                    "skipped_empty",
                    f"no model_gen4_<config_id>.csv files under {metrics_dir}",
                )
            )
            return results

        for metrics_result in metrics_results:
            local_path = _local_metrics_path(downloads_dir, metrics_result.config_id)
            if local_path.exists() and not overwrite:
                results.append(
                    DownloadResult(
                        workstation_id,
                        metrics_result.config_id,
                        metrics_result.remote_path,
                        str(local_path),
                        "skipped_exists",
                    )
                )
                continue
            if dry_run:
                results.append(
                    DownloadResult(
                        workstation_id,
                        metrics_result.config_id,
                        metrics_result.remote_path,
                        str(local_path),
                        "dry_run",
                    )
                )
                continue
            ok = ssh.download_file(metrics_result.remote_path, str(local_path))
            results.append(
                DownloadResult(
                    workstation_id,
                    metrics_result.config_id,
                    metrics_result.remote_path,
                    str(local_path),
                    "downloaded" if ok else "download_failed",
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
            "Download model_gen4_<config_id>.csv from WS-A/B/C/D postprocess "
            "metrics directories into the local Downloads folder."
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
        "--overwrite",
        action="store_true",
        help="Overwrite existing model_gen4_<config_id>.csv files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be downloaded without writing local CSV files.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reload_config_from_toml()
    workstation_ids = tuple(args.workstations or DEFAULT_WORKSTATION_IDS)
    workstations = _selected_workstations(WORKSTATIONS, workstation_ids)
    found_ids = {str(workstation.get("id") or "") for workstation in workstations}
    missing_ids = [item for item in workstation_ids if item not in found_ids]

    all_results: list[DownloadResult] = []
    for workstation in workstations:
        all_results.extend(
            download_workstation_metrics(
                workstation,
                args.downloads_dir,
                overwrite=bool(args.overwrite),
                dry_run=bool(args.dry_run),
            )
        )

    for workstation_id in missing_ids:
        all_results.append(
            DownloadResult(
                workstation_id,
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
        target = f" -> {result.local_path}" if result.local_path else ""
        print(f"[{result.status}] {label}{target}{detail}")

    failures = {"connect_failed", "download_failed"}
    return 1 if any(result.status in failures for result in all_results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
