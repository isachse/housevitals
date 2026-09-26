"""Prometheus HTTP API client with timeouts and a circuit breaker.

When Prometheus fails (refused, timeout, 5xx), the client is marked down and every
query fails immediately with PrometheusUnavailableError instead of waiting for
timeouts again. After a back-off (10 s, doubling up to 60 s) the next query or
probe() is let through; a success marks Prometheus up again. State changes are
logged once, not per query.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from .errors import HomeModbusError, UnavailableError

_LOGGER = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(8.0, connect=2.0)  # per request; queries run concurrently
PROBE_TIMEOUT = httpx.Timeout(3.0, connect=2.0)
BACKOFF_S = (10, 20, 40, 60)
HINT = ("Recorded history and charts are temporarily unavailable. Live values "
        "(get_overview, read_values) still work.")


class PrometheusUnavailableError(UnavailableError):
    """Prometheus cannot be reached or failed to answer."""


class PrometheusQueryError(HomeModbusError):
    """Prometheus rejected a query (a bug here, not an outage)."""

    status = 502


@dataclass
class _State:
    available: bool | None = None  # None = not queried yet
    since: float | None = None  # wall clock of the last state change
    last_error: str | None = None
    failures: int = 0
    retry_at: float = 0.0  # monotonic


class PrometheusClient:
    def __init__(self, url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.url = url.rstrip("/")
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._state = _State()

    # ----------------------------------------------------------------- state
    @property
    def available(self) -> bool | None:
        return self._state.available

    def retry_in(self) -> float:
        return max(0.0, self._state.retry_at - time.monotonic())

    def open(self) -> bool:
        """True while queries fail fast (down and back-off not yet elapsed)."""
        return self._state.available is False and self.retry_in() > 0

    def status(self) -> dict[str, Any]:
        s = self._state
        out: dict[str, Any] = {"available": s.available, "url": self.url}
        if s.since is not None:
            out["since"] = datetime.fromtimestamp(s.since).astimezone().isoformat(timespec="seconds")
        if s.available is False:
            out["last_error"] = s.last_error
            out["retry_in_s"] = round(self.retry_in())
        return out

    def _mark_down(self, error: str) -> None:
        s = self._state
        if s.available is not False:
            _LOGGER.warning("Prometheus at %s unavailable: %s", self.url, error)
            s.since = time.time()
        s.available = False
        s.last_error = error
        s.retry_at = time.monotonic() + BACKOFF_S[min(s.failures, len(BACKOFF_S) - 1)]
        s.failures += 1

    def _mark_up(self) -> None:
        s = self._state
        if s.available is False:
            _LOGGER.warning("Prometheus at %s available again (down since %s)", self.url,
                            datetime.fromtimestamp(s.since or 0).strftime("%H:%M:%S"))
        if s.available is not True:
            s.since = time.time()
        s.available, s.last_error, s.failures, s.retry_at = True, None, 0, 0.0

    def unavailable_error(self) -> PrometheusUnavailableError:
        status = self.status()
        details = {"history_available": False, "hint": HINT}
        if "since" in status:
            details["unavailable_since"] = status["since"]
        return PrometheusUnavailableError(
            f"Prometheus not reachable at {self.url}: {self._state.last_error}",
            details=details, retry_after=max(1.0, self.retry_in()))

    # ----------------------------------------------------------------- requests
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:  # one pooled connection set for all queries
            self._client = httpx.AsyncClient(base_url=self.url, timeout=TIMEOUT,
                                             transport=self._transport)
        return self._client

    async def get(self, path: str, params: dict[str, Any]) -> Any:
        """GET /api/v1/<path>; returns the 'data' member of a successful answer."""
        if self.open():
            raise self.unavailable_error()
        try:
            resp = await self._http().get(f"/api/v1/{path}", params=params)
        except httpx.HTTPError as err:
            self._mark_down(_describe(err))
            raise self.unavailable_error() from err
        if resp.status_code >= 500 or resp.status_code == 429:
            self._mark_down(f"HTTP {resp.status_code}")
            raise self.unavailable_error()
        try:
            body = resp.json()
        except ValueError:
            self._mark_down(f"HTTP {resp.status_code}, not a Prometheus API answer")
            raise self.unavailable_error() from None
        self._mark_up()  # Prometheus answered, even if it rejected the query
        if resp.status_code != 200 or body.get("status") != "success":
            raise PrometheusQueryError(f"Prometheus rejected the query: {body.get('error', resp.status_code)}")
        return body["data"]

    async def probe(self) -> bool:
        """Cheap readiness check used by the background scheduler while down."""
        if self._state.available is not False or self.open():
            return bool(self._state.available)
        try:
            resp = await self._http().get("/-/ready", timeout=PROBE_TIMEOUT)
        except httpx.HTTPError as err:
            self._mark_down(_describe(err))
            return False
        if resp.status_code != 200:
            self._mark_down(f"not ready (HTTP {resp.status_code})")
            return False
        self._mark_up()
        return True

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _describe(err: Exception) -> str:
    if isinstance(err, httpx.TimeoutException):
        return f"timeout ({type(err).__name__})"
    return str(err) or type(err).__name__
