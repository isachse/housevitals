"""Error types shared by all layers.

Every error a caller can cause or should know about derives from HomeModbusError.
MCP tools return it as {"error": message, **details}; the REST API maps `status` to
the HTTP status code and `retry_after` to a Retry-After header. No layer decides by
matching error message text.
"""

from __future__ import annotations

from typing import Any


class HomeModbusError(Exception):
    """An error reported to the caller (MCP tool result or HTTP response)."""

    status = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.details = details or {}
        self.retry_after = retry_after

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": str(self), **self.details}
        if self.retry_after is not None:
            out["retry_after_s"] = round(self.retry_after)
        return out


class NotFoundError(HomeModbusError):
    """Unknown appliance, register, chart, ..."""

    status = 404


class UnavailableError(HomeModbusError):
    """A backend (appliance, Prometheus) cannot be reached right now."""

    status = 503
