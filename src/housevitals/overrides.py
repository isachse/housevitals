"""Overrides: time-limited, allow-listed setpoint writes (the only write path).

An override is a lease: a client (e.g. an automation) asks to hold a register at a
value until a given time. The manager

* accepts only registers listed under the appliance's `overrides` in the config,
  within their bounds, for at most `max_duration_s`,
* remembers the value before the first write (the baseline) and writes it back when
  the lease expires or is released, also after a restart (leases are persisted),
* never writes a value the device already has, and limits writes per register and
  day, because controllers often keep setpoints in EEPROM,
* records (and persists) the lease before it writes, so a value on the device always
  has a lease that restores it: if the request fails or is cancelled after the write,
  or the service stops, the lease ends at once and the baseline is restored,
* checks a written value again after `override_verify_delay_s`: if the device has
  adjusted it (e.g. clamped to a limit of its own), the request is refused and the
  previous value written back, so a caller never believes in an override that has no
  effect,
* serialises requests per register only: a check on one register does not hold up
  others or the restore of ended leases,
* returns every register to its previous value when the service stops
  (`service.restore_overrides_on_stop`, default on), and on the next start after an
  unclean end (crash, power loss), so no override outlives the service,
* leaves a manual change alone: if the register no longer holds the override value
  when the lease ends, someone changed it on the device and it is not restored,
* writes `restore_value` instead of the baseline at the end if the caller asks for it
  (within the allow-list bounds), e.g. to correct a setpoint.

Writes go through the appliance's serialised Modbus client, like every read.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import ConfigError, OverrideRule
from .errors import HomeModbusError, NotFoundError, UnavailableError
from .hub import Appliance, Hub
from .modbus import ModbusReadError
from .registry import Register

_LOGGER = logging.getLogger(__name__)

CHECK_INTERVAL_S = 5.0
_OWNER = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")
_RANGES = {"int16": (-0x8000, 0x7FFF), "uint16": (0, 0xFFFF)}


class OverrideError(HomeModbusError):
    """The override request is invalid (value out of bounds, duration too long, ...)."""


class OverrideNotAllowedError(OverrideError, NotFoundError):
    """The register is not on the appliance's override allow-list."""


class OverrideConflictError(OverrideError):
    """Another owner holds the register, or a previous override is still being restored."""

    status = 409


class WriteBudgetError(OverrideError):
    """The register's writes for today are used up."""

    status = 429


class OverrideRejectedError(OverrideError):
    """The device accepted the write but adjusted the value shortly after (a limit of its
    own); the previous value has been written back."""

    status = 422


@dataclass
class Lease:
    appliance: str
    key: str
    value: Any  # requested value: scaled number or enum label
    raw: int
    baseline_value: Any
    baseline_raw: int
    owner: str
    created: float
    until: float
    reason: str | None = None
    restoring: bool = False  # ended, but the baseline could not be written yet
    last_error: str | None = None
    restore_value: Any = None  # written at the end instead of the baseline, if set
    restore_raw: int | None = None
    # while a write is in flight: the value the device held before (the previous override
    # value for a changed lease), so either one is recognised as "ours" when restoring
    pending: bool = False
    prior_raw: int | None = None

    def holds(self, raw: int) -> bool:
        """True if the device value is ours (not a manual change)."""
        return raw == self.raw or (self.prior_raw is not None and raw == self.prior_raw)

    @property
    def end_raw(self) -> int:
        return self.baseline_raw if self.restore_raw is None else self.restore_raw

    @property
    def end_value(self) -> Any:
        return self.baseline_value if self.restore_raw is None else self.restore_value

    def describe(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        return {
            "appliance": self.appliance,
            "key": self.key,
            "value": self.value,
            "baseline": self.baseline_value,
            **({"restore_value": self.restore_value} if self.restore_raw is not None else {}),
            "owner": self.owner,
            "reason": self.reason,
            "since": _iso(self.created),
            "until": _iso(self.until),
            "remaining_s": max(0, round(self.until - now)),
            "state": "restoring" if self.restoring else "active",
            **({"last_error": self.last_error} if self.last_error else {}),
        }


class OverrideManager:
    def __init__(self, hub: Hub, state_file: str | Path | None = None,
                 restore_on_stop: bool | None = None):
        self.hub = hub
        self.restore_on_stop = (hub.config.service.restore_overrides_on_stop
                                if restore_on_stop is None else restore_on_stop)
        self.tz = ZoneInfo(hub.config.service.timezone)
        self.rules: dict[tuple[str, str], tuple[Register, OverrideRule]] = {}
        for app in hub.appliances.values():
            for key, rule in app.config.overrides.items():
                self.rules[(app.name, key)] = (_check_register(app, key), rule)
        self.state_file = Path(state_file).expanduser() if state_file else None
        self.leases: dict[tuple[str, str], Lease] = {}
        self.writes_today: dict[tuple[str, str], tuple[str, int]] = {}  # -> (local date, count)
        self.writes_total: dict[tuple[str, str], int] = {}  # since start, for metrics
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._task: asyncio.Task | None = None
        self._load()

    @property
    def enabled(self) -> bool:
        return bool(self.rules)

    # ------------------------------------------------------------------ requests
    async def apply(self, appliance: str, key: str, value: Any, owner: str, *,
                    until: str | None = None, duration_s: float | None = None,
                    reason: str | None = None, restore_value: Any = None) -> dict[str, Any]:
        """Hold `key` at `value` until `until` (or for `duration_s`); at the end write the
        baseline, or `restore_value` if given."""
        app = self.hub.get(appliance)
        reg, rule = self._rule(app, key)
        _check_owner(owner)
        raw = _encode(reg, rule, value)
        restore_raw = None if restore_value is None else _encode(reg, rule, restore_value)
        now = time.time()
        end = self._end_time(now, until, duration_s, rule)
        k = (app.name, key)
        async with self._lock(k):
            lease = self.leases.get(k)
            if lease is not None and lease.restoring:
                raise OverrideConflictError(
                    f"{app.name}/{key}: the previous override is still being restored "
                    f"({lease.last_error}); try again later", details={"override": lease.describe()})
            if lease is not None and lease.owner != owner:
                raise OverrideConflictError(f"{app.name}/{key} is held by '{lease.owner}'",
                                            details={"override": lease.describe()})
            current = await app.read_now(reg)
            written = current["raw"] != raw
            if written:
                self._charge(k, rule)
            new = lease is None
            undo = None if new else (lease.value, lease.raw)
            if new:
                lease = Lease(app.name, key, _label(reg, rule, raw), raw, current["value"],
                              current["raw"], owner, now, end, reason)
                self.leases[k] = lease
            if written:
                # the lease exists (on disk) before the device changes
                lease.prior_raw, lease.pending = current["raw"], True
                lease.value, lease.raw = _label(reg, rule, raw), raw
                self._save()
                try:
                    await app.write(reg, raw)
                    await self._verify_settled(app, reg, rule, k, raw, current)
                except OverrideRejectedError:  # the previous value is back on the device
                    if new:
                        del self.leases[k]
                    else:
                        (lease.value, lease.raw), lease.pending, lease.prior_raw = undo, False, None
                    self._save()
                    raise
                except BaseException:  # failed or cancelled: unknown what the device holds
                    lease.until, lease.pending = min(lease.until, time.time()), False
                    self._save()
                    _LOGGER.warning("Override %s/%s interrupted after the write; ending it, "
                                    "the baseline will be restored", *k)
                    raise
            lease.value, lease.raw, lease.until, lease.reason = _label(reg, rule, raw), raw, end, reason
            lease.pending, lease.prior_raw = False, None
            if restore_raw is not None:
                lease.restore_value, lease.restore_raw = _label(reg, rule, restore_raw), restore_raw
            self._save()
        _LOGGER.info("Override %s/%s = %s until %s by %s (%s)", app.name, key, lease.value,
                     _iso(end), owner, "written" if written else "already set")
        return {**lease.describe(), "written": written}

    async def release(self, appliance: str, key: str, owner: str) -> dict[str, Any]:
        """End an override early and restore the baseline."""
        app = self.hub.get(appliance)
        k = (app.name, key)
        async with self._lock(k):
            lease = self.leases.get(k)
            if lease is None:
                raise NotFoundError(f"No active override for {app.name}/{key}")
            if lease.owner != owner:
                raise OverrideConflictError(f"{app.name}/{key} is held by '{lease.owner}'",
                                            details={"override": lease.describe()})
            lease.until = min(lease.until, time.time())
            outcome = await self._restore(lease)
        return {**lease.describe(), "outcome": outcome}

    def status(self, appliance: str | None = None) -> dict[str, Any]:
        apps = [self.hub.get(appliance)] if appliance else list(self.hub.appliances.values())
        names = {a.name for a in apps}
        today = self._today()
        allowed: dict[str, dict[str, Any]] = {}
        for (name, key), (reg, rule) in self.rules.items():
            if name not in names:
                continue
            date, count = self.writes_today.get((name, key), (today, 0))
            entry: dict[str, Any] = {"label": reg.label, "unit": reg.unit,
                                     "max_duration_s": rule.max_duration_s,
                                     "max_writes_per_day": rule.max_writes_per_day,
                                     "writes_today": count if date == today else 0}
            if rule.values:
                entry["values"] = sorted(rule.values)
            else:
                entry["min"], entry["max"] = rule.min, rule.max
            allowed.setdefault(name, {})[key] = entry
        now = time.time()
        return {"overrides": [l.describe(now) for l in self.leases.values() if l.appliance in names],
                "allowed": allowed}

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.restore_on_stop and self.leases:
            # Leases left from the previous run: it did not end cleanly (a clean stop
            # restores them). End them now; the first check restores the values.
            now = time.time()
            for lease in self.leases.values():
                lease.until = min(lease.until, now)
            self._save()
            _LOGGER.warning("%d override(s) left from an unclean stop; restoring them",
                            len(self.leases))
        if self._task is None and (self.rules or self.leases):
            self._task = asyncio.create_task(self.run(), name="overrides")

    async def stop(self) -> None:
        """Stop checking and, with restore_on_stop, end every active override: the
        registers return to their previous (or restore_value) values. A value that
        cannot be written now (device down) stays persisted and is restored on the next
        start. Without restore_on_stop, leases stay in place across a restart."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.restore_on_stop and self.leases:
            await self.restore_all("service stopping")

    async def restore_all(self, reason: str) -> dict[str, str]:
        """End every override now and restore its register. Returns {appliance/key: outcome}."""
        outcomes = {}
        now = time.time()
        for k, lease in list(self.leases.items()):
            async with self._lock(k):
                if self.leases.get(k) is not lease:
                    continue
                lease.until = min(lease.until, now)
                outcomes["/".join(k)] = await self._restore(lease)
        _LOGGER.info("Overrides ended (%s): %s", reason,
                     ", ".join(f"{k} {v}" for k, v in outcomes.items()) or "none")
        return outcomes

    async def run(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                _LOGGER.exception("Unexpected error while checking overrides")
            await asyncio.sleep(CHECK_INTERVAL_S)

    async def check(self) -> None:
        """Restore every override that has ended (or whose restore failed before)."""
        now = time.time()
        for lease in list(self.leases.values()):
            if lease.until > now and not lease.restoring:
                continue
            app = self.hub.appliances.get(lease.appliance)
            if app is not None and app.fail_fast():
                continue  # appliance is down; retried after its back-off
            k = (lease.appliance, lease.key)
            if self._lock(k).locked():
                continue  # a request is working on it; next round
            async with self._lock(k):
                if self.leases.get(k) is lease:
                    await self._restore(lease)

    # ------------------------------------------------------------------ internals
    async def _verify_settled(self, app: Appliance, reg: Register, rule: OverrideRule,
                              k: tuple[str, str], raw: int, previous: dict[str, Any]) -> None:
        """Read the register again after the settle delay. If the device has adjusted the
        value, write the previous one back and refuse the request."""
        await asyncio.sleep(self.hub.config.service.override_verify_delay_s)
        settled = await app.read_now(reg)
        if settled["raw"] == raw:
            return
        requested = _label(reg, rule, raw)
        restored = False
        if settled["raw"] != previous["raw"]:
            self._count(k)  # restoring is never refused by the budget
            await app.write(reg, previous["raw"])
            restored = True
        _LOGGER.warning("Override %s/%s = %s refused: the device changed it to %s; %s %s",
                        *k, requested, settled["value"], "restored" if restored else "kept",
                        previous["value"])
        raise OverrideRejectedError(
            f"{k[0]}/{k[1]}: the device accepted {requested} but changed it to "
            f"{settled['value']} within {self.hub.config.service.override_verify_delay_s:g} s "
            f"(a limit of the device); {previous['value']} was written back",
            details={"requested": requested, "device_value": settled["value"],
                     "restored": previous["value"]})

    async def _restore(self, lease: Lease) -> str:
        """Write the baseline back. Called with the register's lock held. Returns the outcome."""
        k = (lease.appliance, lease.key)
        app = self.hub.appliances.get(lease.appliance)
        reg = app.profile.registers.get(lease.key) if app else None
        if app is None or reg is None:
            _LOGGER.warning("Dropping override %s/%s: appliance or register no longer configured", *k)
            del self.leases[k]
            self._save()
            return "dropped"
        try:
            current = await app.read_now(reg)
            if not lease.holds(current["raw"]):
                outcome = "kept_manual_change"
                _LOGGER.warning("Override %s/%s ended, but the device now holds %s instead of %s: "
                                "changed manually, not restored", *k, current["value"], lease.value)
            elif current["raw"] != lease.end_raw:
                self._count(k)
                await app.write(reg, lease.end_raw)
                outcome = "restored"
                _LOGGER.info("Override %s/%s ended, %s %s", *k,
                             "restored" if lease.restore_raw is None else "set restore_value",
                             lease.end_value)
            else:
                outcome = "unchanged"
        except (UnavailableError, ModbusReadError) as err:
            if not lease.restoring:
                _LOGGER.warning("Override %s/%s ended, restore failed (will retry): %s", *k, err)
            lease.restoring, lease.last_error = True, str(err)
            self._save()
            return "restore_pending"
        del self.leases[k]
        self._save()
        return outcome

    def _rule(self, app: Appliance, key: str) -> tuple[Register, OverrideRule]:
        entry = self.rules.get((app.name, key))
        if entry is None:
            allowed = sorted(k for (a, k) in self.rules if a == app.name)
            raise OverrideNotAllowedError(
                f"{app.name}/{key} cannot be overridden. Allowed: {', '.join(allowed) or 'none'}")
        return entry

    def _end_time(self, now: float, until: str | None, duration_s: float | None,
                  rule: OverrideRule) -> float:
        if (until is None) == (duration_s is None):
            raise OverrideError("Pass exactly one of 'until' or 'duration_s'")
        if duration_s is not None:
            end = now + float(duration_s)
        else:
            end = _parse_until(until, self.tz, now)
        if end <= now:
            raise OverrideError(f"The override must end in the future (until {_iso(end)})")
        if end - now > rule.max_duration_s:
            raise OverrideError(
                f"Overrides of this register may last at most {rule.max_duration_s / 3600:g} h "
                f"(requested until {_iso(end)})")
        return end

    def _lock(self, k: tuple[str, str]) -> asyncio.Lock:
        return self._locks.setdefault(k, asyncio.Lock())

    def _today(self) -> str:
        return datetime.now(self.tz).date().isoformat()

    def _charge(self, k: tuple[str, str], rule: OverrideRule) -> None:
        today = self._today()
        date, count = self.writes_today.get(k, (today, 0))
        if date != today:
            count = 0
        if count >= rule.max_writes_per_day:
            raise WriteBudgetError(
                f"{k[0]}/{k[1]}: {count} of {rule.max_writes_per_day} writes for today are used",
                retry_after=_seconds_to_midnight(self.tz))
        self._count(k)

    def _count(self, k: tuple[str, str]) -> None:
        today = self._today()
        date, count = self.writes_today.get(k, (today, 0))
        self.writes_today[k] = (today, (count if date == today else 0) + 1)
        self.writes_total[k] = self.writes_total.get(k, 0) + 1

    def _load(self) -> None:
        if self.state_file is None or not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            for item in data.get("leases", []):
                lease = Lease(**item)
                if lease.pending:  # stopped during a write: end it, the baseline is restored
                    lease.until, lease.pending = min(lease.until, time.time()), False
                    _LOGGER.warning("Override %s/%s was interrupted during a write; ending it",
                                    lease.appliance, lease.key)
                self.leases[(lease.appliance, lease.key)] = lease
            for name, (date, count) in data.get("writes_today", {}).items():
                app, key = name.split("/", 1)
                self.writes_today[(app, key)] = (date, int(count))
        except (OSError, ValueError, TypeError) as err:
            raise ConfigError(f"Cannot read override state {self.state_file}: {err}") from err
        if self.leases:
            _LOGGER.info("Loaded %d override(s) from %s", len(self.leases), self.state_file)

    def _save(self) -> None:
        if self.state_file is None:
            return
        data = {
            "leases": [asdict(l) for l in self.leases.values()],
            "writes_today": {f"{a}/{k}": list(v) for (a, k), v in self.writes_today.items()},
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_file)


def _check_register(app: Appliance, key: str) -> Register:
    reg = app.profile.registers.get(key)
    where = f"Device '{app.name}', override '{key}'"
    if reg is None:
        raise ConfigError(f"{where}: unknown register for profile '{app.profile.name}'")
    if reg.register_type != "holding" or reg.count != 1 or reg.bit is not None \
            or reg.data_type not in _RANGES:
        raise ConfigError(f"{where}: only single 16-bit holding registers can be overridden")
    rule = app.config.overrides[key]
    if rule.values is not None and reg.enum is None:
        raise ConfigError(f"{where}: 'values' is only for enum registers; use 'min'/'max'")
    if rule.values is None and reg.enum is not None:
        raise ConfigError(f"{where}: enum register, list allowed 'values' (label -> raw code)")
    return reg


def _check_owner(owner: str) -> None:
    if not isinstance(owner, str) or not _OWNER.match(owner):
        raise OverrideError("owner must be 1-64 characters: letters, digits and _ . : / -")


def _encode(reg: Register, rule: OverrideRule, value: Any) -> int:
    """Validate a requested value and turn it into the raw register value."""
    if rule.values is not None:
        if not isinstance(value, str) or value not in rule.values:
            raise OverrideError(f"{reg.key}: value must be one of {', '.join(sorted(rule.values))}")
        return rule.values[value]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OverrideError(f"{reg.key}: value must be a number")
    if not rule.min <= value <= rule.max:
        raise OverrideError(f"{reg.key}: value must be between {rule.min:g} and {rule.max:g}"
                            f"{' ' + reg.unit if reg.unit else ''}")
    raw = round(value / reg.scale)
    if abs(raw * reg.scale - value) > 1e-6:
        raise OverrideError(f"{reg.key}: value must be a multiple of {reg.scale:g}")
    lo, hi = _RANGES[reg.data_type]
    if not lo <= raw <= hi:
        raise OverrideError(f"{reg.key}: value out of the register's range")
    return raw


def _label(reg: Register, rule: OverrideRule, raw: int) -> Any:
    if rule.values is not None:
        return next(label for label, code in rule.values.items() if code == raw)
    value = raw * reg.scale
    return round(value, 4) if isinstance(value, float) else value


def _parse_until(value: str, tz: ZoneInfo, now: float) -> float:
    """ISO date/time (local time zone unless an offset is given) or 'HH:MM' today."""
    text = str(value).strip()
    try:
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", text):
            day = datetime.fromtimestamp(now, tz).date()
            return datetime.combine(day, dtime.fromisoformat(text.zfill(5)), tz).timestamp()
        dt = datetime.fromisoformat(text)
    except ValueError as err:
        raise OverrideError(f"Cannot parse until '{value}': use ISO date/time or HH:MM") from err
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).timestamp()


def _seconds_to_midnight(tz: ZoneInfo) -> float:
    now = datetime.now(tz)
    midnight = datetime.combine(now.date() + timedelta(days=1), dtime(0), tz)
    return (midnight - now).total_seconds()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
