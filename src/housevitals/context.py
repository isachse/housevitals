"""The shared services of one process, created once and used by MCP, REST and metrics."""

from __future__ import annotations

from dataclasses import dataclass

from .charts import ChartService
from .config import ServerConfig
from .history import History
from .hub import Hub


@dataclass
class Services:
    config: ServerConfig
    hub: Hub
    history: History | None = None  # None without service.prometheus_url
    charts: ChartService | None = None

    @classmethod
    def create(cls, config: ServerConfig, hub: Hub | None = None,
               history: History | None = None) -> Services:
        hub = hub or Hub(config)
        svc = config.service
        if history is None and svc.prometheus_url:
            history = History(hub, svc.prometheus_url, svc.timezone)
        charts = ChartService(hub, history) if history is not None else None
        return cls(config, hub, history, charts)

    async def close(self) -> None:
        await self.hub.stop()
        if self.history is not None:
            await self.history.close()
