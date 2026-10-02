"""Long-running service: one Modbus poller for all consumers.

Serves on one HTTP port:
    /mcp            MCP (Streamable HTTP) for Claude and other MCP clients
    /api/v1/...     REST API (OpenAPI docs at /docs), incl. the control API (overrides)
    /healthz        liveness
and pushes all polled values as OpenTelemetry metrics via OTLP.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .api import build_router, install_error_handler
from .config import DEFAULT_DERIVED_STATE_FILE, ConfigError, ServerConfig, parse_config
from .context import Services
from .metrics import setup_metrics
from .server import build_server

_LOGGER = logging.getLogger(__name__)


MIN_TOKEN_LENGTH = 16


def load_control_token(config: ServerConfig) -> str | None:
    """Bearer token for writing overrides: HOUSEVITALS_CONTROL_TOKEN, else the file in
    service.control_token_file. Never part of devices.json itself."""
    token = os.environ.get("HOUSEVITALS_CONTROL_TOKEN")
    path = config.service.control_token_file
    if not token and path:
        try:
            token = Path(path).expanduser().read_text(encoding="utf-8")
        except OSError as err:
            raise ConfigError(f"Cannot read service.control_token_file: {err}") from err
    token = (token or "").strip() or None
    if token is not None and len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"The control token must have at least {MIN_TOKEN_LENGTH} characters")
    return token


def build_app(config: ServerConfig, services: Services | None = None, metric_readers=None,
              control_token: str | None = None) -> FastAPI:
    services = services or Services.create(config)
    hub, charts, service = services.hub, services.charts, config.service
    mcp_app = build_server(services).streamable_http_app(
        streamable_http_path="/mcp",
        host=service.http_host,
        transport_security=_transport_security(service.allowed_hosts),
    )
    provider, exporter = setup_metrics(hub, metric_readers, services.overrides, services.forecast)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await hub.start()
        if services.overrides is not None:
            await services.overrides.start()
        if services.forecast is not None:
            await services.forecast.start()
        chart_task = asyncio.create_task(charts.run(), name="charts") if charts else None
        _LOGGER.info("Polling %d appliance(s); OTLP export %s; history %s; overrides %s",
                     len(hub.appliances), service.otlp_endpoint or "disabled",
                     service.prometheus_url or "disabled",
                     _overrides_mode(services, control_token))
        try:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        finally:
            if chart_task is not None:
                chart_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await chart_task
            await services.close()
            if provider is not None:
                provider.shutdown()

    app = FastAPI(
        title="HouseVitals API",
        version=__version__,
        description=(
            "Access to Brötje heat pumps and Sungrow inverters. Values come from a shared "
            "cache refreshed by a single background Modbus poller. The only write path "
            "is the control API: time-limited overrides of allow-listed setpoints."
        ),
        lifespan=lifespan,
    )
    app.state.services = services
    install_error_handler(app)

    @app.get("/healthz", tags=["service"])
    def healthz() -> dict[str, Any]:
        """Liveness plus the state of every dependency. The service stays "ok" while
        Prometheus is down: live values keep working, only history/charts degrade."""
        out: dict[str, Any] = {"status": "ok",
                               "appliances": {a.name: a.availability() for a in hub.appliances.values()}}
        if services.history is not None:
            out["history"] = services.history.prometheus.status()
        if exporter is not None:
            out["metrics_export"] = exporter.status()
        if services.overrides is not None:
            out["overrides"] = {"active": len(services.overrides.leases),
                                "control": control_token is not None}
        if charts is not None:
            cached = charts.status()
            out["charts"] = {"cached": len(cached), "outdated": sum(c["outdated"] for c in cached)}
        return out

    app.include_router(build_router(services, control_token), prefix="/api/v1")
    app.mount("/", mcp_app)  # serves /mcp; mounted last so API routes win
    return app


def _overrides_mode(services: Services, token: str | None) -> str:
    if services.overrides is None:
        return "disabled"
    return "enabled" if token else "read-only (no control token)"


def _transport_security(extra_hosts: list[str]) -> TransportSecuritySettings:
    """DNS-rebinding protection: only local host names (and configured ones)."""
    hosts = ["127.0.0.1", "localhost", "[::1]", *extra_hosts]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{h}:*" for h in hosts] + hosts,
        allowed_origins=[f"http://{h}:*" for h in hosts],
    )


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per Prometheus query
    config = parse_config(argv)
    if config.service.derived_state_file is None:
        config.service.derived_state_file = DEFAULT_DERIVED_STATE_FILE
    try:
        app = build_app(config, control_token=load_control_token(config))
    except ConfigError as err:
        sys.exit(f"housevitals: {err}")
    uvicorn.run(
        app,
        host=config.service.http_host,
        port=config.service.http_port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
