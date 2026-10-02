"""Import hourly weather history for the house's location from the Open-Meteo archive.

The archive (reanalysis, https://open-meteo.com/en/docs/historical-weather-api) has
hourly values for any place, usually up to a few days ago. With them, recorded data
can be related to the weather of the past years, e.g. heating hours vs. outdoor
temperature or PV yield vs. solar radiation. The location is the `forecast` section
of devices.json (or --latitude/--longitude). Written as OpenMetrics for `promtool tsdb
create-blocks-from openmetrics`, one sample per hour, with source="open-meteo-archive":

    temperature_2m        -> housevitals_weather_temperature_celsius     (at the hour)
    precipitation         -> housevitals_weather_precipitation_mm        (preceding hour)
    rain                  -> housevitals_weather_rain_mm                 (preceding hour)
    snowfall              -> housevitals_weather_snowfall_cm             (preceding hour)
    shortwave_radiation   -> housevitals_weather_radiation_watts_per_m2  (mean of the preceding hour)
    cloud_cover           -> housevitals_weather_cloud_cover_percent     (at the hour)

Query over windows of at least an hour, e.g. daily mean temperature
avg_over_time(housevitals_weather_temperature_celsius[1d]), rain per day
sum_over_time(housevitals_weather_rain_mm[1d]).

    .venv/bin/python tools/import_weather_history.py --config devices.json --start 2024-01-01 --out /tmp/weather.om
    promtool tsdb create-blocks-from openmetrics --max-block-duration=8760h /tmp/weather.om /tmp/weather-blocks
    mv /tmp/weather-blocks/* /opt/homebrew/var/prometheus/
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import httpx

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
VARIABLES = {
    "temperature_2m": "housevitals_weather_temperature_celsius",
    "precipitation": "housevitals_weather_precipitation_mm",
    "rain": "housevitals_weather_rain_mm",
    "snowfall": "housevitals_weather_snowfall_cm",
    "shortwave_radiation": "housevitals_weather_radiation_watts_per_m2",
    "cloud_cover": "housevitals_weather_cloud_cover_percent",
}


def fetch(latitude: float, longitude: float, start: dt.date, end: dt.date,
          transport: httpx.BaseTransport | None = None) -> dict[str, dict[int, float]]:
    """{metric: {unix_time: value}}, fetched a year at a time."""
    series: dict[str, dict[int, float]] = {m: {} for m in VARIABLES.values()}
    with httpx.Client(timeout=120, transport=transport) as client:
        chunk = start
        while chunk <= end:
            chunk_end = min(end, dt.date(chunk.year, 12, 31))
            r = client.get(ARCHIVE_URL, params={
                "latitude": latitude, "longitude": longitude, "timezone": "UTC",
                "start_date": chunk.isoformat(), "end_date": chunk_end.isoformat(),
                "hourly": ",".join(VARIABLES)})
            body = r.json()
            if r.status_code != 200 or body.get("error"):
                raise RuntimeError(f"Open-Meteo archive: {body.get('reason', r.status_code)}")
            hourly = body["hourly"]
            times = [int(dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).timestamp())
                     for t in hourly["time"]]
            for variable, metric in VARIABLES.items():
                for t, v in zip(times, hourly.get(variable, [])):
                    if v is not None:
                        series[metric][t] = float(v)
            chunk = chunk_end + dt.timedelta(days=1)
    return {m: pts for m, pts in series.items() if pts}


def openmetrics(series: dict[str, dict[int, float]]) -> str:
    labels = 'instance="housevitals",job="housevitals",source="open-meteo-archive"'
    lines: list[str] = []
    for metric, points in sorted(series.items()):
        lines.append(f"# TYPE {metric} gauge")
        lines += [f"{metric}{{{labels}}} {v:g} {t}" for t, v in sorted(points.items())]
    return "\n".join([*lines, "# EOF"]) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="devices.json with a forecast section (location)")
    ap.add_argument("--latitude", type=float)
    ap.add_argument("--longitude", type=float)
    ap.add_argument("--start", required=True, type=dt.date.fromisoformat)
    ap.add_argument("--end", type=dt.date.fromisoformat, help="default: yesterday")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    lat, lon = args.latitude, args.longitude
    if (lat is None or lon is None) and args.config:
        forecast = json.loads(args.config.read_text(encoding="utf-8")).get("forecast") or {}
        lat = forecast.get("latitude") if lat is None else lat
        lon = forecast.get("longitude") if lon is None else lon
    if lat is None or lon is None:
        ap.error("pass --config with a forecast section, or --latitude and --longitude")
    end = args.end or dt.date.today() - dt.timedelta(days=1)
    if args.start > end:
        ap.error("--start must not be after --end")
    series = fetch(float(lat), float(lon), args.start, end)
    args.out.write_text(openmetrics(series), encoding="utf-8")
    for metric, points in sorted(series.items()):
        first, last = min(points), max(points)
        print(f"{metric}: {len(points)} hours, {dt.datetime.fromtimestamp(first):%Y-%m-%d %H:%M} "
              f"to {dt.datetime.fromtimestamp(last):%Y-%m-%d %H:%M}")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
