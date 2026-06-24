"""Shared postprocess output path resolution."""
from __future__ import annotations

from collections.abc import Mapping

from engine.config import ENGINE_CONFIG


def _normalize_remote_path(value: object) -> str:
    return str(value or "").replace("\\", "/").rstrip("/")


def resolve_postprocess_paths(
    config: Mapping[str, object],
    *,
    allow_config_override: bool = True,
    metrics_fallback_to_output_subdir: bool = False,
) -> dict[str, str]:
    """Resolve workstation-local postprocess export directories.

    Priority is workstation postprocess_* override, then global ENGINE_CONFIG,
    then legacy workstation result/animation fields for compatibility.
    """
    config_output_dir = config.get("postprocess_output_dir") if allow_config_override else None
    output_dir = _normalize_remote_path(
        config_output_dir
        or ENGINE_CONFIG.get("postprocess_output_dir")
        or config.get("result_dir")
        or config.get("working_dir")
    )

    config_animation_dir = (
        config.get("postprocess_animation_dir") if allow_config_override else None
    )
    animation_dir = _normalize_remote_path(
        config_animation_dir
        or ENGINE_CONFIG.get("postprocess_animation_dir")
        or config.get("animation_dir")
    )

    config_metrics_dir = config.get("postprocess_metrics_dir") if allow_config_override else None
    metrics_source = (
        config_metrics_dir
        or ENGINE_CONFIG.get("postprocess_metrics_dir")
        or config.get("postprocess_output_dir")
        or ENGINE_CONFIG.get("postprocess_output_dir")
        or config.get("result_dir")
    )
    if not metrics_source and metrics_fallback_to_output_subdir and output_dir:
        metrics_source = f"{output_dir}/metrics"
    metrics_dir = _normalize_remote_path(metrics_source)

    return {
        "output_dir": output_dir,
        "animation_dir": animation_dir,
        "metrics_dir": metrics_dir,
    }
