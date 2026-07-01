from __future__ import annotations

"""Canonical log path helpers for AutoFluid processes."""

import os


def get_log_root() -> str:
    """Return the configured root directory for all diagnostic logs."""
    try:
        from engine.config import LOCAL_PATHS

        root = str(LOCAL_PATHS.get("log_dir") or "")
    except (ImportError, AttributeError):
        root = ""
    if root:
        return root
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def get_machine_log_scope() -> str:
    """Return the machine scope for logs produced by the current process."""
    if os.environ.get("AUTOFLUID_SERVER_MODE", "").strip().lower() == "server":
        return "server"
    return "local"


def session_log_dir(process_type: str, timestamp: str) -> str:
    """Return the structured session log directory."""
    return os.path.join(
        get_log_root(),
        get_machine_log_scope(),
        "sessions",
        process_type,
        timestamp,
    )


def service_log_dir(component: str) -> str:
    """Return the structured service log directory for a component."""
    return os.path.join(get_log_root(), get_machine_log_scope(), "services", component)


def service_log_file(component: str, filename: str) -> str:
    """Return the structured service log file path for a component."""
    return os.path.join(service_log_dir(component), filename)


def tunnel_log_dir(kind: str) -> str:
    """Return the local tunnel log directory for a tunnel kind.

    .. note:: Reserved API — currently unused in production but kept for
              future tunnel logging support.
    """
    return os.path.join(get_log_root(), "local", "tunnels", kind)


def export_log_dir() -> str:
    """Return the local TUI export log directory.

    .. note:: Reserved API — currently unused in production but kept for
              future TUI export log support.
    """
    return os.path.join(get_log_root(), "local", "exports")
