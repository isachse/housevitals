"""MCP tools: live values, recorded history and charts of heat pumps and inverters.

Tool results are data with canonical English labels and state names; the LLM
answers in the user's language (see i18n.py). Only chart images are localized.
"""

# No "from __future__ import annotations": tool parameter types are defined locally
# and must be real objects for the MCP schema.
import argparse
import asyncio
import functools
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from urllib.parse import urlencode

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import i18n, queries
from .config import ServerConfig, parse_config
from .context import Services
from .errors import HomeModbusError

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
LANG = "en"  # language of labels in MCP text results

# Kept for callers that imported it from here.
__all__ = ["build_server", "main", "parse_config"]


def _reports_errors(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Return {"error": message} for errors the caller can act on."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except HomeModbusError as err:
            return err.to_dict()

    return wrapper


def _instructions(services: Services) -> str:
    config = services.config
    lines = []
    for d in config.devices:
        alias = f" (aliases: {', '.join(d.aliases)})" if d.aliases else ""
        kind = services.hub.appliances[d.name].profile.kind.replace("_", " ")
        lines.append(f"- {d.name}{alias}: {kind}, {d.profile} profile at {d.host}:{d.port}")
    default = (f" If appliance is omitted, '{config.default_device}' is used."
               if config.default_device else "")
    text = (
        "Read-only access over Modbus TCP to Brötje heat pumps and Sungrow hybrid inverters. "
        f"Configured appliances:\n" + "\n".join(lines) + "\n"
        "Every tool takes an optional 'appliance' argument (name or alias, case-insensitive)."
        f"{default} get_overview without appliance reports all appliances. Use "
        "list_categories / list_registers to discover data points, then read_values to "
        "fetch them. Values are already scaled; enum values are returned as text with the "
        "raw number in 'raw'. 'age_s' is the age of a reading in seconds (values come from "
        "a shared cache refreshed in the background); 'stale': true marks the last known "
        "value when a fresh reading failed. If an appliance does not answer, results carry "
        "available=false, unavailable_since and last_success, and values are the last "
        "known readings; tell the user how old they are. Without any cached value the "
        "result is an error with retry_after_s. A null value means the device reports 'not "
        "available' (e.g. sensor not connected). Labels and state names are English; "
        "answer in the user's language and translate them."
    )
    if services.history is not None:
        text += (
            " Recorded history (Prometheus): get_history (time series with min/max/avg), "
            "get_energy (kWh, self-sufficiency and heat pump performance factor per local "
            "day/week/month/year) and get_runtime (state durations, compressor starts and "
            "run lengths). Times accept ISO dates, relative values like 24h/7d, or "
            "today/yesterday. get_chart returns a pre-rendered PNG chart plus its key "
            "figures; pass lang with the user's language so the text in the image matches. "
            "Use charts when a picture helps the user, not to read exact values. "
            "If Prometheus is down, history tools return an error with "
            "history_available=false and retry_after_s, and get_chart returns the last "
            "image with stale=true, generated_at and a visible 'outdated' badge; tell the "
            "user how old it is. Live values keep working during such an outage."
        )
    return text


def build_server(source: Services | ServerConfig) -> MCPServer:
    """MCP server on top of the shared services (created from a config if needed).
    All reads go through the hub.

    In stdio mode the hub is not started: values are read on demand (and cached
    briefly) instead of by the background poller.
    """
    services = source if isinstance(source, Services) else Services.create(source)
    config, hub = services.config, services.hub
    known = "; ".join(
        f"'{d.name}' ({hub.appliances[d.name].profile.kind.replace('_', ' ')}"
        + (f", aliases: {', '.join(repr(a) for a in d.aliases)}" if d.aliases else "") + ")"
        for d in config.devices
    )
    known_desc = f"Appliance name or alias, case-insensitive. Known appliances: {known}."
    if config.default_device:
        device_desc = known_desc + f" Empty = default appliance '{config.default_device}'."
    elif len(config.devices) == 1:
        device_desc = known_desc + " May be left empty."
    else:
        device_desc = known_desc + " Required because several appliances are configured."
    # Plain (non-nullable) types: some MCP clients drop the type of Optional params.
    # The parameter is deliberately not called "device": remote MCP bridges use an
    # argument of that name to pick the target computer and strip it from the call.
    ApplianceArg = Annotated[str, Field(description=device_desc)]
    AllAppliancesArg = Annotated[str, Field(description=known_desc + " Empty = every appliance.")]

    mcp = MCPServer(name="housevitals", instructions=_instructions(services))
    tool = mcp.tool(annotations=READ_ONLY)

    @tool
    @_reports_errors
    async def list_devices() -> dict[str, Any]:
        """List the configured appliances (heat pumps, inverters) with their names,
        aliases, connection settings and poll status."""
        result = {
            "default_device": config.default_device,
            "devices": [{**a.describe(), **({"status": a.poll_status()} if hub.polling else {})}
                        for a in hub.appliances.values()],
        }
        if services.history is not None:
            result["history"] = services.history.prometheus.status()
        return result

    @tool
    @_reports_errors
    async def get_overview(appliance: AllAppliancesArg = "") -> dict[str, Any]:
        """Read the most important live values. Heat pumps: temperatures, status,
        operating mode, COP, energy counters. Inverters: PV, battery, grid and house
        load power, battery charge level and today's/total energy.

        Args:
            appliance: Name or alias. Omit for every appliance (the default appliance
                is not applied here).
        """
        if appliance or len(hub.appliances) == 1:
            return await queries.overview(hub.get(appliance), LANG)
        return {"appliances": await queries.overview_all(list(hub.appliances.values()), LANG)}

    @tool
    @_reports_errors
    async def list_categories(appliance: ApplianceArg = "") -> dict[str, Any]:
        """List the register categories of an appliance's profile with register counts."""
        app = hub.get(appliance)
        return {"appliance": app.name, "profile": app.profile.name,
                "description": app.profile.description, "categories": app.profile.categories}

    @tool
    @_reports_errors
    async def list_registers(
        appliance: ApplianceArg = "",
        category: Annotated[str, Field(description="Register category from list_categories. Empty = any.")] = "",
        search: Annotated[str, Field(description='Case-insensitive text matched against key, labels (English and German) and category, e.g. "temp", "energy", "Vorlauf". Empty = no filter.')] = "",
    ) -> dict[str, Any]:
        """List known data points (key, label, unit, Modbus address, poll group) without
        reading them. poll_group null = not recorded, read on demand only.

        Args:
            appliance: Name or alias of the appliance (see list_devices).
            category: Only registers of this category (see list_categories).
            search: Substring of key, labels or category.
        """
        app = hub.get(appliance)
        regs = queries.describe_registers(app, category, search, LANG)
        return {"appliance": app.name, "count": len(regs), "registers": regs}

    @tool
    @_reports_errors
    async def read_values(
        appliance: ApplianceArg = "",
        keys: Annotated[list[str], Field(description='Register keys from list_registers, e.g. ["flow_temperature"] or ["battery_soc"].')] = [],  # noqa: B006 - copied per call
        category: Annotated[str, Field(description="Read every register of this category.")] = "",
        search: Annotated[str, Field(description="Read every register whose key/label/category contains this text.")] = "",
    ) -> dict[str, Any]:
        """Read live values from a heat pump or inverter, selected by register keys,
        category or search term (at least one is required).

        Args:
            appliance: Name or alias of the appliance (see list_devices).
            keys: Register keys, e.g. ["flow_temperature", "outdoor_temperature"].
            category: Read every register in a category.
            search: Read every register whose key/label/category contains this text.
        """
        app = hub.get(appliance)
        return await queries.read_values(app, queries.select_registers(app, keys, category, search), LANG)

    @tool
    @_reports_errors
    async def read_raw_registers(
        address: Annotated[int, Field(description="Zero-based Modbus register address.")],
        count: Annotated[int, Field(description="Number of registers (1-125).")] = 1,
        register_type: Annotated[str, Field(description='"holding" (function 03) or "input" (function 04).')] = "holding",
        appliance: ApplianceArg = "",
    ) -> dict[str, Any]:
        """Read raw 16-bit register words for diagnostics or unmapped registers.

        Args:
            address: Zero-based Modbus register address.
            count: Number of registers (1-125).
            register_type: "holding" (function 03) or "input" (function 04).
            appliance: Name or alias of the appliance (see list_devices).
        """
        if register_type not in ("holding", "input"):
            raise HomeModbusError("register_type must be 'holding' or 'input'")
        if not 1 <= count <= 125:
            raise HomeModbusError("count must be between 1 and 125")
        app = hub.get(appliance)
        # Same client and lock as the poller: never parallel to other requests.
        words = await app.read_raw(register_type, address, count)
        return {
            "appliance": app.name,
            "register_type": register_type,
            "registers": {str(address + i): {"uint16": w, "int16": w - 0x10000 if w >= 0x8000 else w}
                          for i, w in enumerate(words)},
        }

    if services.history is not None:
        _history_tools(tool, services, ApplianceArg, AllAppliancesArg)
    if services.charts is not None:
        _chart_tool(tool, services, known_desc)
    return mcp


def _history_tools(tool, services: Services, ApplianceArg, AllAppliancesArg) -> None:
    hub, history = services.hub, services.history
    StartArg = Annotated[str, Field(
        description="Start: ISO date/time in local time (2026-09-01, 2026-09-01T06:00), "
        "relative (24h, 7d, 30m = that long ago) or today/yesterday. Empty = tool default.")]
    EndArg = Annotated[str, Field(description="End, same formats as start. Empty = now.")]

    @tool
    @_reports_errors
    async def get_history(
        appliance: ApplianceArg = "",
        keys: Annotated[list[str], Field(description='Register keys, e.g. ["flow_temperature", "outdoor_temperature"]. Only recorded values (poll_group not null) have history.')] = [],  # noqa: B006
        start: StartArg = "",
        end: EndArg = "",
        max_points: Annotated[int, Field(description="Maximum points per series (1-500). Use a small number when min/max/avg is enough.")] = 60,
    ) -> dict[str, Any]:
        """Recorded time series of one appliance: per key min, max, average, last value
        (counters also the increase) and a downsampled series. Default range: last 24 h.

        Args:
            appliance: Name or alias of the appliance (see list_devices).
            keys: Register keys from list_registers.
            start: Start of the range (default 24h ago).
            end: End of the range (default now).
            max_points: Maximum number of points per series.
        """
        app = hub.get(appliance)
        if not keys:
            raise HomeModbusError("Provide keys. Recorded keys: " + ", ".join(history.recorded_keys(app)))
        return await history.history(app, keys, start, end, max_points, LANG)

    @tool
    @_reports_errors
    async def get_energy(
        appliance: AllAppliancesArg = "",
        period: Annotated[str, Field(description='"day", "week", "month" or "year" (local calendar).')] = "day",
        start: StartArg = "",
        end: EndArg = "",
    ) -> dict[str, Any]:
        """Energy balance per calendar period from the lifetime counters: kWh per counter
        (PV, grid import/export, battery, heat pump electricity and heat), plus derived
        values: house consumption, self-sufficiency, self-consumption rate and heat pump
        performance factor (SPF). Also returns totals for the range.
        Defaults: last 7 days per day, 8 weeks per week, 12 months per month.

        Args:
            appliance: Name or alias; empty for all appliances.
            period: day, week, month or year.
            start: Start of the range.
            end: End of the range (default now).
        """
        apps = [hub.get(appliance)] if appliance else list(hub.appliances.values())
        return await history.energy(apps, period, start, end)

    @tool
    @_reports_errors
    async def get_runtime(
        appliance: ApplianceArg = "",
        key: Annotated[str, Field(description='State register, e.g. "compressor", "compressor_demand", "running_state", "battery_charging".')] = "compressor",
        start: StartArg = "",
        end: EndArg = "",
    ) -> dict[str, Any]:
        """Time spent in each state of an on/off or enum value (hours and share), number
        of starts and completed run lengths, e.g. compressor cycling or heating vs. hot
        water. Default range: today. At most 31 days.

        Args:
            appliance: Name or alias of the appliance (see list_devices).
            key: Register key of a state value.
            start: Start of the range (default today 00:00).
            end: End of the range (default now).
        """
        return await history.runtime(hub.get(appliance), key, start, end, LANG)


def _chart_tool(tool, services: Services, known_desc: str) -> None:
    charts, svc = services.charts, services.config.service
    catalog = "; ".join(
        f"{c['chart']}: {c['description']} (default {c['default_range']}, "
        f"{c['range_limits'][0]}-{c['range_limits'][1]})" for c in charts.catalog())

    @tool
    async def get_chart(
        chart: Annotated[str, Field(description=f"Chart name. {catalog}.")] = "energy_flow",
        appliance: Annotated[str, Field(description=known_desc + " Empty = the chart's default (first matching appliance, or all heat pumps for charts comparing them).")] = "",
        range: Annotated[str, Field(description="Time range ending now, e.g. 24h, 7d, 4w. Empty = chart default.")] = "",  # noqa: A002 - name shown to the model
        lang: Annotated[str, Field(description=f"Language of the text in the image, one of {', '.join(i18n.SUPPORTED)}; use the user's language. Empty = configured default.")] = "",
    ) -> list:
        """A pre-rendered PNG chart from the recorded history, plus its key figures as
        JSON and a URL of the image. Default charts are refreshed in the background
        (every 5 min for 24 h charts), so answers are instant; 'age_s' is the image age.

        Args:
            chart: One of the catalog charts (see parameter description).
            appliance: Name or alias of the appliance.
            range: Time range ending now, e.g. 24h, 7d.
            lang: Language of the chart text.
        """
        try:
            image = await charts.get(chart, appliance, range, lang or None)
        except HomeModbusError as err:
            return [json.dumps(err.to_dict(), ensure_ascii=False)]
        query = urlencode({k: v for k, v in (("appliance", appliance), ("range", range),
                                                 ("lang", image.summary["lang"])) if v})
        meta = {**image.summary, "age_s": round(image.age_s, 1),
                "url": f"http://{svc.http_host}:{svc.http_port}/api/v1/charts/{chart}.png?{query}"}
        if image.stale:
            meta["note"] = ("Outdated image: it could not be updated (see stale_reason). "
                            "It shows data up to 'end'; tell the user its age.")
        return [json.dumps(meta, ensure_ascii=False), Image(data=image.png, format="png")]


def main(argv: list[str] | None = None) -> None:
    """stdio MCP server.

    With --url / HOUSEVITALS_URL (the service's /mcp endpoint) it bridges to the
    running housevitals service, sharing its cache, history and charts. If the
    service is not reachable at start, or no URL is given, it serves directly and
    opens its own Modbus connections (needs --config / HOUSEVITALS_CONFIG).
    """
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--url", default=os.environ.get("HOUSEVITALS_URL"))
    args, rest = pre.parse_known_args(argv)
    if args.url:
        from . import proxy

        if proxy.service_reachable(args.url):
            asyncio.run(proxy.run(args.url))
            return
        logging.getLogger(__name__).warning(
            "housevitals service not reachable at %s; serving directly instead", args.url)
    build_server(Services.create(parse_config(rest), control=False)).run("stdio")


if __name__ == "__main__":
    main()
