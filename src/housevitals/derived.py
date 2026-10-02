"""Derived data points: values computed from other data points by rules in the profile.

A derived data point behaves like a register: it is cached, exported as a metric,
recorded, and readable through read_values / get_history. It is never read over
Modbus. The hub calls `update()` after every successful poll; each rule looks at the
cache entries of its sources and advances when a source has a new sample.

Operations (fixed set, declared as data in the profile, see registry.DerivedRule):

* integrate: cumulative counter. Between two consecutive samples of the source,
  value += (s0 + s1) / 2 × scale × hours, optionally only while the `when` data point
  had a given raw value at the earlier sample. Gaps longer than `max_gap_s` (outage,
  restart) add nothing; their length is reported as `uncovered_s`.

Counters are persisted (`service.derived_state_file`), so they keep growing across
restarts; a lost state file only restarts them at 0, which Prometheus' increase()
treats as a counter reset.

Derived values describe the house; they never decide or write anything.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import ConfigError
from .registry import DerivedRule

if TYPE_CHECKING:
    from .hub import Appliance

_LOGGER = logging.getLogger(__name__)

SAVE_INTERVAL_S = 60.0
# Decimals of a counter value as cached and exported. The backfill tool writes the same
# precision: rounding is monotonic, so counters never drop where backfilled and live
# samples meet (Prometheus would read any drop as a counter reset).
PRECISION = 4


@dataclass
class _Counter:
    value: float = 0.0
    uncovered_s: float = 0.0  # time between samples that was skipped (gaps)
    last_ts: float | None = None  # source sample last integrated (not persisted)
    last_power: float | None = None
    last_state_ok: bool = True


class DerivedValues:
    """Evaluates the derived data points of all appliances and keeps their state."""

    def __init__(self, state_file: str | Path | None = None):
        self.state_file = Path(state_file).expanduser() if state_file else None
        self.counters: dict[tuple[str, str], _Counter] = {}
        self._saved_at = 0.0
        self._dirty = False
        self._load()

    # ------------------------------------------------------------------ evaluation
    def update(self, app: Appliance, now: float | None = None) -> None:
        """Advance every derived point of `app` whose sources have new samples."""
        rules = app.profile.derived
        if not rules:
            return
        now = time.time() if now is None else now
        for rule in rules.values():
            counter = self.counters.setdefault((app.name, rule.key), _Counter())
            if self._integrate(app, rule, counter):
                self._dirty = True
            app.cache_derived(rule.key, {"value": round(counter.value, PRECISION),
                                         "unit": app.profile.registers[rule.key].unit}, now)
        if self._dirty and now - self._saved_at >= SAVE_INTERVAL_S:
            self.save(now)

    @staticmethod
    def _integrate(app: Appliance, rule: DerivedRule, c: _Counter) -> bool:
        entry = app.cache.get(rule.integrate)
        if entry is None or entry.ts == c.last_ts:
            return False  # no new sample
        power = entry.data.get("value")
        state_ok = True
        if rule.when_key:
            state = app.cache.get(rule.when_key)
            raw = None if state is None else state.data.get("raw", state.data.get("value"))
            state_ok = raw == rule.when_raw
        changed = False
        if not isinstance(power, (int, float)) or isinstance(power, bool):
            power = None
        if c.last_ts is not None:
            dt = entry.ts - c.last_ts
            if power is None or c.last_power is None or dt <= 0 or dt > rule.max_gap_s:
                if dt > 0:
                    c.uncovered_s += dt
                    changed = True
            elif c.last_state_ok:
                c.value += (c.last_power + power) / 2 * rule.scale * dt / 3600
                changed = True
        c.last_ts, c.last_power, c.last_state_ok = entry.ts, power, state_ok
        return changed

    def describe(self, appliance: str) -> dict[str, Any]:
        return {key: {"value": round(c.value, 4), "uncovered_s": round(c.uncovered_s)}
                for (name, key), c in self.counters.items() if name == appliance}

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        if self.state_file is None or not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            for name, points in data.get("counters", {}).items():
                for key, item in points.items():
                    self.counters[(name, key)] = _Counter(float(item["value"]),
                                                          float(item.get("uncovered_s", 0.0)))
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as err:
            raise ConfigError(f"Cannot read derived state {self.state_file}: {err}") from err
        _LOGGER.info("Loaded %d derived counter(s) from %s", len(self.counters), self.state_file)

    def save(self, now: float | None = None) -> None:
        self._saved_at = time.time() if now is None else now
        self._dirty = False
        if self.state_file is None:
            return
        data: dict[str, Any] = {"counters": {}}
        for (name, key), c in sorted(self.counters.items()):
            data["counters"].setdefault(name, {})[key] = {"value": c.value, "uncovered_s": c.uncovered_s}
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_file)
