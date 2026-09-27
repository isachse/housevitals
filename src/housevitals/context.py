"""The shared services of one process, created once and used by MCP, REST and metrics."""

from __future__ import annotations

from dataclasses import dataclass

from .charts import ChartService
from .config import ServerConfig
from .history import History
from .hub import Hub
from .overrides import OverrideManager


@dataclass
class Services:
    config: ServerConfig
    hub: Hub
    history: History | None = None  # None without service.prometheus_url
    charts: ChartService | None = None
    overrides: OverrideManager | None = None  # None without any override allow-list

    @classmethod
    def create(cls, config: ServerConfig, hub: Hub | None = None,
               history: History | None = None,
               overrides: OverrideManager | None = None,
               control: bool = True) -> Services:
        """control=False (stdio direct mode) never manages overrides; only the
        long-running service does."""
        hub = hub or Hub(config)
        svc = config.service
        if history is None and svc.prometheus_url:
            history = History(hub, svc.prometheus_url, svc.timezone)
        charts = ChartService(hub, history) if history is not None else None
        if overrides is None and control and any(d.overrides for d in config.devices):
            overrides = OverrideManager(hub, svc.override_state_file)
        return cls(config, hub, history, charts, overrides)

    async def close(self) -> None:
        if self.overrides is not None:
            await self.overrides.stop()
        await self.hub.stop()
        if self.history is not None:
            await self.history.close()
