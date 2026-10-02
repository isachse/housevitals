"""Central data hub: the only Modbus client per appliance, with cache and background poller.

Every consumer (MCP tools, REST API, OpenTelemetry metrics) reads through the hub.
Registers in the poll plan are refreshed in the background and served from the
cache; anything else is read on demand through the same per-device client, whose
lock guarantees that no two requests ever hit a device in parallel.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import DeviceConfig, ServerConfig, ServiceConfig
from .errors import UnavailableError
from .modbus import ModbusClient, ModbusConnectError, ModbusReadError, decode
from .derived import DerivedValues
from .registry import DERIVED, POLL_GROUPS, Profile, Register, load_profile, poll_group

_LOGGER = logging.getLogger(__name__)

# A polled value is served from the cache while younger than this many poll intervals.
STALE_AFTER_INTERVALS = 3
# While an appliance is down, the poller retries after 1, 2, 4, 8 fast intervals, then
# every MAX_RETRY_S. Requests in between never touch the device.
BACKOFF_FACTORS = (1, 2, 4, 8)
MAX_RETRY_S = 300.0
UNAVAILABLE_HINT = ("The appliance does not answer (offline, network or gateway problem). "
                    "Values shown are the last known readings, see age_s; recorded history "
                    "(get_history, get_energy) still works.")


class ApplianceUnavailableError(UnavailableError):
    """The appliance is down and there is no cached value to fall back to."""


@dataclass
class CacheEntry:
    data: dict[str, Any]  # {"value": ..., "unit"?: ..., "raw"?: ..., "error"?: ...}
    ts: float  # wall-clock time of the Modbus read


@dataclass
class GroupStatus:
    interval: float
    registers: int
    last_attempt: float | None = None
    last_success: float | None = None
    duration: float | None = None
    errors: int = 0
    last_error: str | None = None


@dataclass
class Appliance:
    config: DeviceConfig
    profile: Profile
    client: ModbusClient
    service: ServiceConfig
    groups: dict[str, list[Register]] = field(default_factory=dict)
    status: dict[str, GroupStatus] = field(default_factory=dict)
    cache: dict[str, CacheEntry] = field(default_factory=dict)
    group_by_key: dict[str, str] = field(default_factory=dict)
    up: bool | None = None  # None = not polled yet
    on_fast_poll: Callable[[], None] | None = None  # e.g. wakes the chart scheduler
    last_error: str | None = None
    since: float | None = None  # wall clock of the last up/down change
    last_success: float | None = None  # wall clock of the last answered request
    failures: int = 0  # consecutive failed attempts while down
    retry_at: float = 0.0  # monotonic; before this, requests fail fast
    written_at: dict[str, float] = field(default_factory=dict)  # key -> wall clock of last write
    derived_values: DerivedValues | None = None  # evaluates the profile's derived points

    @classmethod
    def create(cls, config: DeviceConfig, service: ServiceConfig) -> Appliance:
        profile = load_profile(config.profile, config.zones)
        unknown = [k for k in config.extra_keys if k not in profile.registers]
        if unknown:
            _LOGGER.warning("%s: unknown extra_keys ignored: %s", config.name, ", ".join(unknown))
        extra = frozenset(config.extra_keys)
        app = cls(
            config=config,
            profile=profile,
            client=ModbusClient(
                config.host, config.port, config.unit_id, config.timeout,
                min_request_interval=config.min_request_interval,
            ),
            service=service,
        )
        intervals = {
            "fast": config.poll_interval or service.poll_fast,
            "slow": max(service.poll_slow, config.poll_interval or 0),
            "static": service.poll_static,
        }
        intervals[DERIVED] = intervals["fast"]  # computed after polls, never read over Modbus
        for reg in profile.registers.values():
            group = DERIVED if reg.register_type == DERIVED else poll_group(reg, extra)
            if group:
                app.groups.setdefault(group, []).append(reg)
                app.group_by_key[reg.key] = group
        for group, regs in app.groups.items():
            app.status[group] = GroupStatus(interval=intervals[group], registers=len(regs))
        return app

    @property
    def name(self) -> str:
        return self.config.name

    def max_age(self, reg: Register) -> float:
        group = self.group_by_key.get(reg.key)
        if group is None:
            return self.service.on_demand_ttl
        return self.status[group].interval * STALE_AFTER_INTERVALS

    def fresh(self, reg: Register, now: float | None = None) -> CacheEntry | None:
        entry = self.cache.get(reg.key)
        if entry is None:
            return None
        now = time.time() if now is None else now
        return entry if now - entry.ts <= self.max_age(reg) else None

    # ----------------------------------------------------------------- availability
    def retry_in(self) -> float:
        return max(0.0, self.retry_at - time.monotonic())

    def fail_fast(self) -> bool:
        """True while down and the back-off has not elapsed: don't touch the device."""
        return self.up is False and self.retry_in() > 0

    def _mark_down(self, error: str) -> None:
        if self.up is not False:
            _LOGGER.warning("%s unavailable: %s", self.name, error)
            self.since = time.time()
        self.up, self.last_error = False, error
        interval = self.status["fast"].interval if "fast" in self.status else self.service.poll_fast
        if self.failures < len(BACKOFF_FACTORS):
            wait = min(MAX_RETRY_S, interval * BACKOFF_FACTORS[self.failures])
        else:
            wait = MAX_RETRY_S
        self.retry_at = time.monotonic() + wait
        self.failures += 1

    def _mark_up(self) -> None:
        if self.up is False:
            _LOGGER.warning("%s available again (down since %s)", self.name, _iso(self.since or 0))
        if self.up is not True:
            self.since = time.time()
        self.up, self.last_error, self.failures, self.retry_at = True, None, 0, 0.0
        self.last_success = time.time()

    def availability(self) -> dict[str, Any]:
        """Machine-readable availability, attached to answers while the appliance is down."""
        out: dict[str, Any] = {"available": self.up}
        if self.since is not None:
            key = "unavailable_since" if self.up is False else "available_since"
            out[key] = _iso(self.since)
        if self.up is False:
            out["last_error"] = self.last_error
            out["last_success"] = _iso(self.last_success) if self.last_success else None
            out["retry_in_s"] = round(self.retry_in())
        return out

    def unavailable_error(self) -> ApplianceUnavailableError:
        details = {"appliance": self.name, **self.availability(), "hint": UNAVAILABLE_HINT}
        details.pop("retry_in_s", None)
        return ApplianceUnavailableError(f"{self.name} is not reachable: {self.last_error}",
                                         details=details, retry_after=max(1.0, self.retry_in()))

    def _store(self, results: dict[str, dict[str, Any]], ts: float,
               started: float | None = None) -> set[str]:
        """Cache read results. A failed register keeps its last good value (which then
        ages and is reported as stale) instead of being overwritten with null. A read
        that started before the register was last written is outdated and dropped, so
        a poll that overlaps an override cannot bring back the previous value.
        Returns the keys whose read failed."""
        failed = set()
        for key, data in results.items():
            if started is not None and self.written_at.get(key, 0.0) >= started:
                continue
            if "error" in data:
                failed.add(key)
                previous = self.cache.get(key)
                if previous is not None and previous.data.get("value") is not None:
                    continue
            self.cache[key] = CacheEntry(data, ts)
        return failed

    async def poll(self, group: str) -> None:
        if group == DERIVED:  # computed, never read over Modbus
            if self.derived_values is not None:
                self.derived_values.update(self)
            return
        regs = self.groups[group]
        status = self.status[group]
        started = time.monotonic()
        status.last_attempt = read_started = time.time()
        try:
            results = await self.client.read(regs)
        except ModbusReadError as err:
            status.errors += 1
            status.last_error = str(err)
            _LOGGER.debug("%s: poll %s failed: %s", self.name, group, err)
            if isinstance(err, ModbusConnectError):
                self._mark_down(str(err))  # logged once per outage
            return
        finally:
            status.duration = time.monotonic() - started
        self._store(results, time.time(), read_started)
        status.last_success = time.time()
        status.last_error = None
        self._mark_up()
        if self.derived_values is not None:
            self.derived_values.update(self)
        if group == "fast" and self.on_fast_poll is not None:
            self.on_fast_poll()

    async def run(self) -> None:
        """Poll all groups forever, each on its own interval (static first).

        While the appliance is down only the fast group is tried, after the back-off;
        once it answers again, every group is refreshed at once.
        """
        due = {g: 0.0 for g in POLL_GROUPS if g in self.groups}
        probe = "fast" if "fast" in due else next(iter(due))
        while True:
            if self.up is False:
                await asyncio.sleep(max(0.2, self.retry_in()))
                try:
                    await self.poll(probe)
                except Exception:
                    _LOGGER.exception("%s: unexpected error polling %s", self.name, probe)
                if self.up:
                    due = dict.fromkeys(due, 0.0)
                continue
            now = time.monotonic()
            for group in (g for g in ("static", "slow", "fast") if g in due):
                if due[group] <= now:
                    try:
                        await self.poll(group)
                    except Exception:  # keep polling even after unexpected errors
                        _LOGGER.exception("%s: unexpected error polling %s", self.name, group)
                    due[group] = now + self.status[group].interval
            await asyncio.sleep(max(0.2, min(due.values()) - time.monotonic()))

    async def read(self, regs: list[Register]) -> dict[str, dict[str, Any]]:
        """Values for regs: fresh cache entries, the rest read now (serialised).

        If the device is unreachable, the last known value is returned with
        "stale": true instead of failing.
        """
        now = time.time()
        # derived points come from the cache only (computed after polls)
        missing = [r for r in regs if r.register_type != DERIVED and self.fresh(r, now) is None]
        stale_keys: set[str] = {r.key for r in regs if r.register_type == DERIVED
                                and r.key in self.cache and self.fresh(r, now) is None}
        error: str | None = None
        if missing and self.fail_fast():  # known outage: answer from the cache at once
            error = self.last_error
            stale_keys |= {r.key for r in missing}
        elif missing:
            try:
                read_started = time.time()
                results = await self.client.read(missing)
                stale_keys |= self._store(results, time.time(), read_started)
                self._mark_up()
            except ModbusReadError as err:
                error = str(err)
                stale_keys |= {r.key for r in missing}
                if isinstance(err, ModbusConnectError):
                    self._mark_down(error)
        now = time.time()
        out: dict[str, dict[str, Any]] = {}
        for reg in regs:
            entry = self.cache.get(reg.key)
            if entry is None:
                out[reg.key] = {"value": None, "error": error or (
                    "computed after the next poll" if reg.register_type == DERIVED else "no data")}
                continue
            data = {**entry.data, "age_s": round(now - entry.ts, 1)}
            # While the appliance is down every value is a last known one, however young.
            if (reg.key in stale_keys or self.up is False) and data.get("value") is not None:
                data["stale"] = True
            out[reg.key] = data
        return out

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "aliases": self.config.aliases,
            "type": self.profile.kind.replace("_", " "),
            "description": self.config.description,
            "host": self.config.host,
            "port": self.config.port,
            "unit_id": self.config.unit_id,
            "profile": self.profile.name,
            "profile_description": self.profile.description,
            "zones": self.config.zones if self.profile.name == "iwr" else None,
            "register_count": len(self.profile.registers),
        }

    async def read_raw(self, register_type: str, address: int, count: int) -> list[int]:
        """Raw words, through the same serialised client (fails fast while down)."""
        if self.fail_fast():
            raise self.unavailable_error()
        try:
            words = await self.client.read_raw(register_type, address, count)
        except ModbusConnectError as err:
            self._mark_down(str(err))
            raise self.unavailable_error() from err
        except ModbusReadError:
            self._mark_up()  # an exception response still proves the device answers
            raise
        self._mark_up()
        return words

    def cache_derived(self, key: str, data: dict[str, Any], ts: float) -> None:
        """Store a derived value (called by DerivedValues after a poll)."""
        self.cache[key] = CacheEntry(data, ts)
        status = self.status.get(DERIVED)
        if status is not None:
            status.last_attempt = status.last_success = ts

    async def read_now(self, reg: Register) -> dict[str, Any]:
        """Read one register from the device, bypassing the cache (fails fast while down).
        The result always carries "raw"."""
        if reg.register_type == DERIVED:
            raise ValueError(f"{reg.key} is derived, not a device register")
        read_started = time.time()
        words = await self.read_raw(reg.register_type, reg.address, reg.count)
        value, raw = decode(reg, words)
        self._store({reg.key: self.client._result(reg, words)}, time.time(), read_started)
        return {"value": value, "raw": raw}

    async def write(self, reg: Register, raw: int) -> dict[str, Any]:
        """Write one holding register through the serialised client and verify it by
        reading it back. Only the override manager calls this."""
        if self.fail_fast():
            raise self.unavailable_error()
        try:
            words = await self.client.write_register(reg.address, raw)
        except ModbusConnectError as err:
            self._mark_down(str(err))
            raise self.unavailable_error() from err
        except ModbusReadError:
            self._mark_up()
            raise
        self._mark_up()
        self.written_at[reg.key] = time.time()
        value, read_back = decode(reg, words)
        self._store({reg.key: self.client._result(reg, words)}, time.time())
        if read_back != raw:
            raise ModbusReadError(
                f"{self.name}: {reg.key} reads back {read_back} after writing {raw}; "
                "the device did not accept the value")
        return {"value": value, "raw": read_back}

    def poll_status(self) -> dict[str, Any]:
        now = time.time()

        def age(ts: float | None) -> float | None:
            return None if ts is None else round(now - ts, 1)

        return {
            "up": self.up,
            **{k: v for k, v in self.availability().items() if k != "available"},
            "groups": {
                g: {
                    "interval_s": s.interval,
                    "registers": s.registers,
                    "last_success_age_s": age(s.last_success),
                    "duration_s": None if s.duration is None else round(s.duration, 3),
                    "errors": s.errors,
                    "last_error": s.last_error,
                }
                for g, s in self.status.items()
            },
        }


class Hub:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.appliances: dict[str, Appliance] = {
            d.name: Appliance.create(d, config.service) for d in config.devices
        }
        self._tasks: list[asyncio.Task] = []
        # Set after every successful fast poll of any appliance.
        self.polled = asyncio.Event()
        self.derived = DerivedValues(config.service.derived_state_file)
        for app in self.appliances.values():
            app.on_fast_poll = self.polled.set
            app.derived_values = self.derived

    def get(self, name: str | None) -> Appliance:
        """Resolve a name or alias (raises ConfigError)."""
        return self.appliances[self.config.resolve(name).name]

    @property
    def polling(self) -> bool:
        return bool(self._tasks)

    async def start(self) -> None:
        for app in self.appliances.values():
            self._tasks.append(asyncio.create_task(app.run(), name=f"poll-{app.name}"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        if self.derived.counters:
            self.derived.save()
        for app in self.appliances.values():
            await app.client.close()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
