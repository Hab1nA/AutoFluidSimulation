from __future__ import annotations


class InfrastructureUnavailableError(ConnectionError):
    """Raised when SSH/tunnel infrastructure is unavailable, not when work failed."""
