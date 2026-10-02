"""Import the history of a Sungrow plant from iSolarCloud "Plant report" CSV exports.

iSolarCloud exports a yearly plant report with a row every 15 minutes (local time,
daylight saving time included) and the plant's lifetime counters. They are the
inverter's own counters, so they continue exactly where housevitals' live series
start (checked: total yield, feed-in and purchased energy are identical at the same
time). The tool writes them as OpenMetrics for `promtool tsdb create-blocks-from
openmetrics`, under the live metric names, so energy statistics, charts and Grafana
reach back into the imported years:

    Total yield(kWh)               -> housevitals_pv_energy_kWh_total
    Total feed-in(kWh)             -> housevitals_export_energy_kWh_total
    Total purchased energy(kWh)    -> housevitals_import_energy_kWh_total
    Total load consumption(kWh)    -> housevitals_load_energy_kWh_total (no live counter)

Counters must not decrease, or Prometheus reads a reset. The reports contain two
kinds of drops, both repaired before writing: a single sample below both neighbours
(a glitch) is dropped; a lasting step down (the inverter corrected its counter, the
live counter continues from the corrected value) is kept by lowering every earlier
sample by the step. Increases per period stay exact and the series still ends at the
live value; only absolute values before a correction are lower than in the report.

The series carry source="sungrow_portal" (all queries combine the series of an
appliance) and end before the first live sample, read from Prometheus, so imported
and live samples never overlap. Times that do not exist (the hour skipped when daylight
saving time starts) are dropped; an hour that exists twice is taken once.

    .venv/bin/python tools/import_sungrow_report.py --out /tmp/sungrow.om ~/Downloads/Plant\\ report_*.csv
    promtool tsdb create-blocks-from openmetrics --max-block-duration=8760h /tmp/sungrow.om /tmp/sungrow-blocks
    mv /tmp/sungrow-blocks/* /opt/homebrew/var/prometheus/
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

COLUMNS = {
    "Total yield(kWh)": "housevitals_pv_energy_kWh_total",
    "Total feed-in(kWh)": "housevitals_export_energy_kWh_total",
    "Total purchased energy(kWh)": "housevitals_import_energy_kWh_total",
    "Total load consumption(kWh)": "housevitals_load_energy_kWh_total",
}


def local_ts(text: str, tz: ZoneInfo) -> int | None:
    """Unix time of a local 'YYYY-MM-DD HH:MM', None if that time does not exist (DST gap)."""
    naive = dt.datetime.strptime(text, "%Y-%m-%d %H:%M")
    aware = naive.replace(tzinfo=tz)
    back = dt.datetime.fromtimestamp(aware.timestamp(), tz).replace(tzinfo=None)
    return int(aware.timestamp()) if back == naive else None


def read_reports(paths: list[Path], tz: ZoneInfo) -> tuple[dict[str, dict[int, float]], list[str]]:
    """{metric: {unix_time: value}} from the CSV files; (series, problems)."""
    series: dict[str, dict[int, float]] = {}
    problems: list[str] = []
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as f:
            title = f.readline().strip()
            if not title.startswith("Plant report"):
                problems.append(f"{path.name}: not a plant report ({title[:40]!r})")
                continue
            reader = csv.DictReader(f)
            missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                problems.append(f"{path.name}: missing columns {missing}")
                continue
            for row in reader:
                ts = local_ts(row["Time"], tz)
                if ts is None:
                    continue  # the hour skipped when daylight saving time starts
                for column, metric in COLUMNS.items():
                    value = (row[column] or "").strip()
                    if value:
                        series.setdefault(metric, {}).setdefault(ts, float(value))
    return series, problems


def make_monotonic(points: dict[int, float]) -> tuple[dict[int, float], list[str]]:
    """Repair drops (see module doc). Returns the repaired points and what was done."""
    items = sorted(points.items())
    notes: list[str] = []
    cleaned = []
    for i, (t, v) in enumerate(items):  # glitches: below the previous and the next sample
        if 0 < i < len(items) - 1 and v < items[i - 1][1] and items[i + 1][1] >= items[i - 1][1]:
            notes.append(f"glitch {v:g} dropped at {dt.datetime.fromtimestamp(t):%Y-%m-%d %H:%M}")
            continue
        cleaned.append((t, v))
    shift = 0.0
    out: list[tuple[int, float]] = []
    for i in range(len(cleaned) - 1, -1, -1):  # backwards: lower everything before a step down
        t, v = cleaned[i]
        if i < len(cleaned) - 1 and v + shift > out[-1][1]:
            step = v + shift - out[-1][1]
            shift -= step
            if step >= 0.15:  # 0.1 kWh steps are rounding noise of the report
                notes.append(f"correction -{step:g} kWh at {dt.datetime.fromtimestamp(cleaned[i + 1][0]):%Y-%m-%d %H:%M}")
        out.append((t, round(v + shift, 3)))
    return dict(reversed(out)), notes


def drops(series: dict[str, dict[int, float]]) -> list[str]:
    out = []
    for metric, points in series.items():
        items = sorted(points.items())
        for (t0, v0), (t1, v1) in zip(items, items[1:]):
            if v1 < v0:
                out.append(f"{metric} drops {v0} -> {v1} at {dt.datetime.fromtimestamp(t1):%Y-%m-%d %H:%M}")
    return out


def first_live(prometheus: str, metric: str, appliance: str) -> float | None:
    """Time of the first live sample of metric (any series of the appliance), None if none."""
    query = (f'min(min_over_time(timestamp({metric}{{appliance="{appliance}",'
             f'source!="sungrow_portal"}})[3650d:15m]))')
    r = httpx.get(f"{prometheus.rstrip('/')}/api/v1/query", params={"query": query}, timeout=120)
    r.raise_for_status()
    result = r.json()["data"]["result"]
    return float(result[0]["value"][1]) if result else None


def openmetrics(series: dict[str, dict[int, float]], appliance: str,
                until: dict[str, float | None]) -> tuple[str, dict[str, int]]:
    labels = (f'appliance="{appliance}",instance="housevitals",job="housevitals",kind="inverter",'
              f'profile="sungrow_sh",source="sungrow_portal"')
    lines: list[str] = []
    counts: dict[str, int] = {}
    for metric, points in sorted(series.items()):
        end = until.get(metric)
        samples = [(t, v) for t, v in sorted(points.items()) if end is None or t < end]
        counts[metric] = len(samples)
        lines.append(f"# TYPE {metric} gauge")
        lines += [f"{metric}{{{labels}}} {v:g} {t}" for t, v in samples]
    return "\n".join([*lines, "# EOF"]) + "\n", counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path, help="iSolarCloud plant report CSV files")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--appliance", default="inverter")
    ap.add_argument("--timezone", default="Europe/Berlin", help="time zone of the report times")
    ap.add_argument("--prometheus", default="http://127.0.0.1:9090",
                    help="to end each series before its first live sample; '' to skip")
    args = ap.parse_args(argv)
    series, problems = read_reports([f.expanduser() for f in args.files], ZoneInfo(args.timezone))
    for metric in list(series):
        series[metric], notes = make_monotonic(series[metric])
        problems += [f"{metric}: {n}" for n in notes]
    problems += drops(series)  # must be empty now
    for p in problems:
        print("warning:", p, file=sys.stderr)
    if not series:
        print("no values found", file=sys.stderr)
        return 1
    until = {m: first_live(args.prometheus, m, args.appliance) if args.prometheus else None for m in series}
    text, counts = openmetrics(series, args.appliance, until)
    args.out.write_text(text, encoding="utf-8")
    for metric, n in counts.items():
        points = sorted(series[metric].items())
        end = until.get(metric)
        print(f"{metric}: {n} samples, {dt.datetime.fromtimestamp(points[0][0]):%Y-%m-%d %H:%M} "
              f"({points[0][1]:g} kWh) to {'first live sample ' + dt.datetime.fromtimestamp(end).strftime('%Y-%m-%d %H:%M') if end else 'end of report'}")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
