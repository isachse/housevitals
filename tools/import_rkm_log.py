"""Import the operating log from the micro-SD card of a Brötje NEO-RKM into Prometheus.

The NEO-RKM writes one file per day (`YYYYMMDD`, in folders per year) with a line
`id;unix_time;value` for seven values every hour, plus one set after every start of
the module. The values are lifetime counters of the heat pump that are not available
over Modbus. Checked against the recorded compressor runs (see README):

    3375  compressor starts           -> housevitals_rkm_compressor_starts_total
    3171  operating hours, hot water  -> housevitals_rkm_dhw_hours_total
    3172  operating hours, heating    -> housevitals_rkm_heating_hours_total
    3173  operating hours, total      -> housevitals_rkm_operating_hours_total (3171 + 3172)
    241, 3188, 3189 (meaning unknown) -> housevitals_rkm_log_value{id="…"}

Hours count whole hours. The module's clock may be off (one was 48 min fast); pass
--clock-offset-min to correct it, otherwise the times are taken as written.

The tool writes an OpenMetrics file for `promtool tsdb create-blocks-from openmetrics`.
The metric names are separate from everything the service exports, so an import never
mixes with live series. Samples are hourly: query them over windows of at least an
hour, e.g. starts per day = max_over_time(x[1d]) - max_over_time(x[1d] offset 1d).

    .venv/bin/python tools/import_rkm_log.py --appliance heatpump2 --out /tmp/rkm.om ~/Downloads/2024 ~/Downloads/2025
    promtool tsdb create-blocks-from openmetrics --max-block-duration=8760h /tmp/rkm.om /tmp/rkm-blocks
    mv /tmp/rkm-blocks/* /opt/homebrew/var/prometheus/
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

NAMED = {  # log id -> metric (lifetime counters)
    "3375": "housevitals_rkm_compressor_starts_total",
    "3171": "housevitals_rkm_dhw_hours_total",
    "3172": "housevitals_rkm_heating_hours_total",
    "3173": "housevitals_rkm_operating_hours_total",
}
UNKNOWN = "housevitals_rkm_log_value"
FILE_NAME = re.compile(r"^\d{8}$")
_LABEL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def read_logs(paths: list[Path]) -> tuple[dict[str, dict[int, int]], list[str], list[dt.date]]:
    """{id: {unix_time: value}} from all day files under paths; (series, problems, days)."""
    series: dict[str, dict[int, int]] = {}
    problems: list[str] = []
    days: list[dt.date] = []
    files = sorted(f for p in paths for f in ([p] if p.is_file() else p.iterdir()) if FILE_NAME.match(f.name))
    for f in files:
        days.append(dt.date(int(f.name[:4]), int(f.name[4:6]), int(f.name[6:])))
        for n, line in enumerate(f.read_text(encoding="ascii", errors="replace").splitlines(), 1):
            if not line.strip():
                continue
            parts = line.strip().split(";")
            if len(parts) != 3 or not all(p.lstrip("-").isdigit() for p in parts):
                problems.append(f"{f.name}:{n}: unreadable line {line.strip()!r}")
                continue
            series.setdefault(parts[0], {})[int(parts[1])] = int(parts[2])
    return series, problems, sorted(set(days))


def check(series: dict[str, dict[int, int]]) -> list[str]:
    """Counters must not decrease; every drop is reported (it would read as a reset)."""
    problems = []
    for log_id, metric in NAMED.items():
        points = sorted(series.get(log_id, {}).items())
        for (t0, v0), (t1, v1) in zip(points, points[1:]):
            if v1 < v0:
                problems.append(f"id {log_id} ({metric}) drops {v0} -> {v1} at "
                                f"{dt.datetime.fromtimestamp(t1):%Y-%m-%d %H:%M}")
    return problems


def gaps(days: list[dt.date]) -> list[tuple[dt.date, dt.date]]:
    return [(a + dt.timedelta(days=1), b - dt.timedelta(days=1)) for a, b in zip(days, days[1:])
            if (b - a).days > 1]


def openmetrics(series: dict[str, dict[int, int]], appliance: str, offset_s: int = 0) -> str:
    """OpenMetrics text, one family per metric, samples in time order."""
    if not _LABEL.match(appliance):
        raise ValueError(f"invalid appliance name {appliance!r}")
    base = f'appliance="{appliance}",instance="housevitals",job="housevitals",source="rkm_sd"'
    families: dict[str, list[str]] = {}
    for log_id, points in sorted(series.items()):
        metric = NAMED.get(log_id, UNKNOWN)
        labels = base if log_id in NAMED else f'{base},id="{log_id}"'
        lines = families.setdefault(metric, [])
        lines += [f"{metric}{{{labels}}} {v} {t - offset_s}" for t, v in sorted(points.items())]
    out = []
    for metric, lines in sorted(families.items()):
        out += [f"# TYPE {metric} gauge", *lines]  # type metadata does not matter for the TSDB
    return "\n".join([*out, "# EOF"]) + "\n"


def monthly(series: dict[str, dict[int, int]]) -> dict[str, dict[str, int]]:
    """Increase per calendar month of the named counters (for the summary)."""
    out: dict[str, dict[str, int]] = {}
    for log_id in NAMED:
        first: dict[str, int] = {}
        last: dict[str, int] = {}
        for t, v in sorted(series.get(log_id, {}).items()):
            m = f"{dt.datetime.fromtimestamp(t):%Y-%m}"
            first.setdefault(m, v)
            last[m] = v
        for m in first:
            out.setdefault(m, {})[log_id] = last[m] - first[m]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", type=Path, help="year folders or day files from the SD card")
    ap.add_argument("--appliance", required=True, help="housevitals appliance name, e.g. heatpump2")
    ap.add_argument("--out", required=True, type=Path, help="OpenMetrics file to write")
    ap.add_argument("--clock-offset-min", type=float, default=0.0,
                    help="minutes the module's clock was fast (subtracted from every time)")
    args = ap.parse_args(argv)
    series, problems, days = read_logs([p.expanduser() for p in args.paths])
    problems += check(series)
    if not series:
        print("no log lines found", file=sys.stderr)
        return 1
    for p in problems:
        print("warning:", p, file=sys.stderr)
    args.out.write_text(openmetrics(series, args.appliance, round(args.clock_offset_min * 60)), encoding="utf-8")
    samples = sum(len(v) for v in series.values())
    print(f"{len(days)} days from {days[0]} to {days[-1]}, {samples} samples of {len(series)} values -> {args.out}")
    for a, b in gaps(days):
        print(f"  no log: {a} to {b} ({(b - a).days + 1} days)")
    print("increase per month (starts, hot water h, heating h):")
    for m, inc in sorted(monthly(series).items()):
        print(f"  {m}: {inc.get('3375', 0):4d} starts  {inc.get('3171', 0):4d} h hot water  {inc.get('3172', 0):4d} h heating")
    return 0


if __name__ == "__main__":
    sys.exit(main())
