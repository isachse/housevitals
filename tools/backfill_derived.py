"""Backfill derived counters (e.g. energy integrated from power) from recorded history.

Derived data points are computed by the service from the moment they exist. To give
them the history before that, this tool integrates the recorded source values from
Prometheus exactly like the service does (trapezoid between samples, `when` state at
the earlier sample, gaps longer than max_gap_s add nothing) and writes

* an OpenMetrics file with the counter every 30 s, for `promtool tsdb
  create-blocks-from openmetrics`, labelled like the live series, and
* the service's derived state file with the final values, so the live counters
  continue where the backfill ends (no drop that Prometheus would read as a reset).

Run it while the service still runs the version without the derived points, then
create the block, move it into Prometheus' data directory and restart the service:

    .venv/bin/python tools/backfill_derived.py --config devices.json --out /tmp/derived.om
    promtool tsdb create-blocks-from openmetrics --max-block-duration=2400h /tmp/derived.om /tmp/blocks
    mv /tmp/blocks/* /opt/homebrew/var/prometheus/   # Prometheus picks it up within a minute
    launchctl kickstart -k gui/$(id -u)/local.housevitals
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from housevitals.config import DEFAULT_DERIVED_STATE_FILE, load_config_file  # noqa: E402
from housevitals.derived import PRECISION  # noqa: E402
from housevitals.hub import Hub  # noqa: E402
from housevitals.metrics import instrument_plan  # noqa: E402

STEP_S = 15  # query resolution (the fast poll interval)
OUT_STEP_S = 30  # resolution of the backfilled counter (< lookback-delta of 1 min)
CHUNK_S = 10_000 * STEP_S  # Prometheus returns at most 11 000 points per series


class Prometheus:
    def __init__(self, url: str):
        self.client = httpx.Client(base_url=url.rstrip("/"), timeout=60)

    def query(self, query: str, at: float | None = None) -> list[dict]:
        params = {"query": query, **({"time": at} if at else {})}
        return self.client.get("/api/v1/query", params=params).raise_for_status().json()["data"]["result"]

    def series(self, query: str, start: float, end: float) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []
        t = start
        while t < end:
            stop = min(end, t + CHUNK_S)
            r = self.client.get("/api/v1/query_range", params={
                "query": query, "start": t, "end": stop, "step": STEP_S}).raise_for_status().json()
            for res in r["data"]["result"]:
                points += [(float(ts), float(v)) for ts, v in res["values"]]
            t = stop + STEP_S
        return sorted(set(points))


def integrate(power: list[tuple[float, float]], state: dict[float, float] | None, rule) -> tuple[list, float, float]:
    """Cumulative counter [(ts, value)] at every power sample; (series, final, uncovered_s)."""
    value, uncovered, out = 0.0, 0.0, []
    prev = None
    for ts, p in power:
        if prev is not None:
            t0, p0 = prev
            dt = ts - t0
            if dt > rule.max_gap_s:
                uncovered += dt
            elif state is None or state.get(t0) == rule.when_raw:
                value += (p0 + p) / 2 * rule.scale * dt / 3600
        out.append((ts, value))
        prev = (ts, p)
    return out, value, uncovered


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--prometheus", default="http://127.0.0.1:9090")
    ap.add_argument("--out", required=True, help="OpenMetrics file to write")
    ap.add_argument("--state-file", help=f"derived state to write (default: service setting or {DEFAULT_DERIVED_STATE_FILE})")
    ap.add_argument("--force", action="store_true", help="overwrite an existing state file")
    ap.add_argument("--until", help="end of the backfill (ISO time; default now)")
    ap.add_argument("--no-state", action="store_true",
                    help="do not write the state file (the service already continues its counters)")
    args = ap.parse_args()

    config = load_config_file(args.config)
    state_file = Path(args.state_file or config.service.derived_state_file or DEFAULT_DERIVED_STATE_FILE).expanduser()
    if state_file.exists() and not args.force and not args.no_state:
        sys.exit(f"{state_file} exists (the service already keeps derived counters); use --force to replace it")
    hub = Hub(config)
    names = {(s.appliance.name, s.reg.key): inst.prometheus_name for inst in instrument_plan(hub) for s in inst.series}
    prom = Prometheus(args.prometheus)
    until = datetime.fromisoformat(args.until).timestamp() if args.until else time.time()
    end = math.floor(until / OUT_STEP_S) * OUT_STEP_S - OUT_STEP_S
    families: dict[str, list[str]] = {}  # OpenMetrics: the samples of a metric stay together
    state: dict[str, dict] = {"counters": {}}
    for app in hub.appliances.values():
        if not app.profile.derived:
            continue

        def point(key: str) -> str:
            return f'max by (appliance) ({names[(app.name, key)]}{{appliance="{app.name}"}})'

        labels_of = prom.query(f'last_over_time({names[(app.name, next(iter(app.profile.derived.values())).integrate)]}'
                               f'{{appliance="{app.name}"}}[10m])')
        if not labels_of:
            print(f"{app.name}: no recent samples, skipped", file=sys.stderr)
            continue
        labels = {k: v for k, v in labels_of[-1]["metric"].items() if k != "__name__"}
        label_text = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        first = prom.query(f"min(min_over_time(timestamp({point(next(iter(app.profile.derived.values())).integrate)})[400d:1h]))")
        start = math.floor(float(first[0]["value"][1]) / STEP_S) * STEP_S if first else end
        cache: dict[str, list] = {}
        for rule in app.profile.derived.values():
            if rule.integrate not in cache:
                cache[rule.integrate] = prom.series(point(rule.integrate), start, end)
            states = None
            if rule.when_key:
                if rule.when_key not in cache:
                    cache[rule.when_key] = prom.series(point(rule.when_key), start, end)
                states = dict(cache[rule.when_key])
            series, final, uncovered = integrate(cache[rule.integrate], states, rule)
            metric = names[(app.name, rule.key)]
            # never above the first live sample after the backfill (no drop = no reset)
            live = prom.query(f'min(min_over_time({metric}{{appliance="{app.name}"}}[1h] offset -1h))', end + 60)
            cap = float(live[0]["value"][1]) if live else math.inf
            lines = families.setdefault(metric, [])
            next_out = 0.0
            for ts, v in series:
                if ts >= next_out:
                    lines.append(f"{metric}{{{label_text}}} {min(round(v, PRECISION), cap)} {int(ts)}")
                    next_out = ts + OUT_STEP_S
            state["counters"].setdefault(app.name, {})[rule.key] = {"value": final, "uncovered_s": uncovered}
            print(f"{app.name}/{rule.key}: {final:.2f} kWh since "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(start))}, gaps {uncovered / 3600:.1f} h")
    lines = [line for metric, samples in sorted(families.items()) for line in (f"# TYPE {metric} gauge", *samples)]
    lines.append("# EOF")
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    if not args.no_state:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, indent=1), encoding="utf-8")
        print(f"wrote {state_file}")


if __name__ == "__main__":
    main()
