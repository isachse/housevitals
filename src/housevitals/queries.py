"""Live-value queries shared by the MCP tools and the REST API.

Both front ends call these functions and only differ in how they present results
and errors (MCP: {"error": ...}; REST: HTTP status from HomeModbusError.status).
"""

from __future__ import annotations

import asyncio
from typing import Any

from .errors import HomeModbusError, NotFoundError
from .hub import Appliance
from .registry import Register

MAX_VALUES_PER_CALL = 150


class UnknownKeyError(NotFoundError):
    """A requested register key does not exist in the appliance's profile."""


def select_registers(app: Appliance, keys: list[str] | None = None, category: str | None = None,
                     search: str | None = None) -> list[Register]:
    """Registers chosen by keys, category or search term (at least one is required)."""
    if not (keys or category or search):
        raise HomeModbusError("Provide keys, category or search.")
    try:
        regs = app.profile.find(keys=keys or None, category=category or None, search=search or None)
    except KeyError as err:
        raise UnknownKeyError(f"{err.args[0]}; see list_registers.") from err
    if not regs:
        raise NotFoundError("No registers matched.")
    if len(regs) > MAX_VALUES_PER_CALL:
        raise HomeModbusError(
            f"{len(regs)} registers matched; narrow the selection to at most {MAX_VALUES_PER_CALL}.")
    return regs


async def read_values(app: Appliance, regs: list[Register], lang: str = "en") -> dict[str, Any]:
    """Current values (from the cache when fresh) with labels in `lang`."""
    values = await app.read(regs)
    known = any(v.get("value") is not None for v in values.values())
    if app.up is False and not known:
        raise app.unavailable_error()  # nothing cached to fall back to
    result: dict[str, Any] = {
        "appliance": app.name,
        "profile": app.profile.name,
        "values": {reg.key: {"label": reg.display_label(lang), **values[reg.key]} for reg in regs},
    }
    if app.up is False:
        result.update(app.availability())
        result["warning"] = ("Appliance not reachable; showing the last known values "
                             "(stale: true, see age_s).")
    elif not known and all("error" in v for v in values.values()):
        result["error"] = next(iter(values.values()))["error"]
    return result


async def overview_all(apps: list[Appliance], lang: str = "en") -> dict[str, dict[str, Any]]:
    """Overview of several appliances; one that is down never fails the others."""

    async def one(app: Appliance) -> dict[str, Any]:
        try:
            return await overview(app, lang)
        except HomeModbusError as err:
            return {"appliance": app.name, **err.to_dict()}

    results = await asyncio.gather(*(one(a) for a in apps))
    return {r["appliance"]: r for r in results}


async def overview(app: Appliance, lang: str = "en") -> dict[str, Any]:
    return await read_values(app, app.profile.find(summary_only=True), lang)


def describe_registers(app: Appliance, category: str | None = None, search: str | None = None,
                       lang: str = "en") -> list[dict[str, Any]]:
    return [
        {**r.describe(lang), "poll_group": app.group_by_key.get(r.key)}
        for r in app.profile.find(category=category or None, search=search or None)
    ]
