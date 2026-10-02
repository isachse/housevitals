"""The Open-Meteo archive importer (tools/import_weather_history.py)."""

import datetime as dt
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import import_weather_history as wx  # noqa: E402


def _archive(request: httpx.Request) -> httpx.Response:
    start = dt.date.fromisoformat(request.url.params["start_date"])
    end = dt.date.fromisoformat(request.url.params["end_date"])
    hours = []
    t = dt.datetime.combine(start, dt.time())
    while t.date() <= end:
        hours.append(t.strftime("%Y-%m-%dT%H:%M"))
        t += dt.timedelta(hours=1)
    n = len(hours)
    return httpx.Response(200, json={"hourly": {
        "time": hours, "temperature_2m": [5.0] * n, "precipitation": [0.2] * n, "rain": [0.2] * n,
        "snowfall": [0.0] * n, "shortwave_radiation": [None] + [100.0] * (n - 1), "cloud_cover": [50] * n}})


def test_fetch_per_year_and_openmetrics():
    calls = []

    def handler(request):
        calls.append((request.url.params["start_date"], request.url.params["end_date"]))
        return _archive(request)

    series = wx.fetch(52.5, 13.4, dt.date(2024, 12, 31), dt.date(2025, 1, 1), httpx.MockTransport(handler))
    assert calls == [("2024-12-31", "2024-12-31"), ("2025-01-01", "2025-01-01")]  # a request per year
    assert len(series["housevitals_weather_temperature_celsius"]) == 48
    assert len(series["housevitals_weather_radiation_watts_per_m2"]) == 46  # missing values skipped
    first = min(series["housevitals_weather_temperature_celsius"])
    assert first == int(dt.datetime(2024, 12, 31, tzinfo=dt.timezone.utc).timestamp())  # times are UTC
    text = wx.openmetrics(series)
    assert text.endswith("# EOF\n") and text.count("# TYPE") == 6
    assert 'housevitals_weather_rain_mm{instance="housevitals",job="housevitals",source="open-meteo-archive"} 0.2 ' in text


def test_archive_error_is_raised():
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": True, "reason": "bad date"}))
    with pytest.raises(RuntimeError, match="bad date"):
        wx.fetch(52.5, 13.4, dt.date(2024, 1, 1), dt.date(2024, 1, 2), transport)


def test_location_from_config(tmp_path, monkeypatch):
    cfg = tmp_path / "devices.json"
    cfg.write_text('{"forecast": {"latitude": 52.5, "longitude": 13.4}}')
    seen = {}

    def fake_fetch(lat, lon, start, end):
        seen.update(lat=lat, lon=lon)
        return {"housevitals_weather_temperature_celsius": {0: 1.0}}

    monkeypatch.setattr(wx, "fetch", fake_fetch)
    out = tmp_path / "w.om"
    assert wx.main(["--config", str(cfg), "--start", "2024-01-01", "--end", "2024-01-02", "--out", str(out)]) == 0
    assert seen == {"lat": 52.5, "lon": 13.4} and out.read_text().endswith("# EOF\n")
