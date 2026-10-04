"""Tenant insights: billing span, estimates from operating hours, degree days, report."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from housevitals.config import ConfigError, DeviceConfig, InsightsConfig, ServerConfig, ServiceConfig
from housevitals.history import History
from housevitals.hub import Hub
from housevitals.insights import (
    Insights, InsightsError, _pv_share, billing_span, daily_means, degree_days, dhw_litres,
    rate_factor, value_at,
)

TZ = ZoneInfo("Europe/Berlin")
H = 3600


def test_billing_span_whole_months():
    now = datetime(2026, 10, 4, 15, 0, tzinfo=TZ)
    assert billing_span("", "", now) == (datetime(2025, 10, 1, tzinfo=TZ), now)
    assert billing_span("2025-03-17", "2026-01-01", now) == (
        datetime(2025, 3, 1, tzinfo=TZ), datetime(2026, 1, 1, tzinfo=TZ))
    # an end inside a month includes that month, never beyond now
    assert billing_span("2026-01-01", "2026-02-10", now)[1] == datetime(2026, 3, 1, tzinfo=TZ)
    assert billing_span("2026-09-01", "2027-01-01", now)[1] == now
    with pytest.raises(InsightsError):
        billing_span("2023-01-01", "", now)  # more than 24 months
    with pytest.raises(InsightsError):
        billing_span("2026-11-01", "", now)


def test_value_at_interpolates_log_gaps():
    series = [(0, 10.0), (H, 11.0), (2 * H, 12.0), (102 * H, 112.0)]
    assert value_at(series, -1) == (None, False)
    assert value_at(series, H + 60) == (11.0, False)
    # 50 h into a 100 h gap: half of its 100 hours
    assert value_at(series, 52 * H) == (pytest.approx(62.0), True)
    assert value_at(series, 102 * H) == (112.0, False)
    assert value_at(series, 200 * H) == (None, False)  # long after the last sample


def test_rate_factor_uses_common_samples_after_start():
    hours = [(t * H, 100 + t * 0.5) for t in range(0, 40)]
    counter = [(t * H, 7.0 + t) for t in range(10, 50)]  # recorded from t = 10 h
    kwh, hrs = rate_factor(counter, hours, since=10 * H)
    assert (kwh, hrs) == (29.0, 14.5)  # t = 10..39
    assert rate_factor(counter, hours[:12], since=10 * H) is None  # under MIN_FACTOR_HOURS


def test_degree_days_and_litres():
    day = datetime(2026, 1, 5, tzinfo=TZ).timestamp()
    pts = [(day + i * H, 0.0) for i in range(24)] + [(day + 86400 + i * H, 16.0) for i in range(24)]
    pts += [(day + 2 * 86400 + i * H, 5.0) for i in range(10)]  # too few hours: dropped
    means = daily_means(pts, TZ)
    assert list(means.values()) == [0.0, 16.0]
    assert degree_days(means, 20, 15) == (20.0, 1)  # 16 °C is above the heating limit
    cfg = InsightsConfig(dhw_loss_share=0.3)
    assert dhw_litres(46.52, cfg) == pytest.approx(700, rel=1e-3)  # 1.163 Wh/(l K) x 40 K


def test_pv_share():
    assert _pv_share({"total_pv_energy": 300, "total_export_energy": 100, "total_import_energy": 800}) == 0.2
    assert _pv_share({"total_pv_energy": 300, "total_export_energy": None, "total_import_energy": 8}) is None


def test_insights_config_validation():
    assert InsightsConfig.from_dict({}).consumption_share == 0.7
    for bad in ({"consumption_share": 0}, {"living_area_m2": -5}, {"grid_price_eur_per_kwh": -1},
                {"dhw_temperature_c": 5}, {"unknown": 1}):
        with pytest.raises(ConfigError):
            InsightsConfig.from_dict(bad)


# --------------------------------------------------------------------------- report
NOW = datetime(2026, 3, 15, 12, 0, tzinfo=TZ)
JAN, FEB, MAR = (datetime(2026, m, 1, tzinfo=TZ) for m in (1, 2, 3))


def _series(start, end, fn, step=H):
    t, out = start.timestamp(), []
    while t <= end.timestamp():
        v = fn(t)
        if v is not None:
            out.append([t, str(v)])
        t += step
    return out


class FakeHistory(History):
    """History with canned energy() results and hourly series instead of Prometheus.

    Heat pump "hp": heating 0.5 operating hours per clock hour from Dec 2025 on, no hot
    water hours; its counters are recorded from 1 Feb with 2 kWh electricity and 8 kWh
    heat per operating hour. Weather: 0 °C in January, 10 °C in February."""

    def __init__(self):
        devices = [DeviceConfig(name="hp", host="127.0.0.1", profile="neo"),
                   DeviceConfig(name="inverter", host="127.0.0.1", profile="sungrow_sh")]
        super().__init__(Hub(ServerConfig(devices=devices, service=ServiceConfig(prometheus_url="http://prom"))),
                         "http://prom", "Europe/Berlin")
        self.now = lambda: NOW

    async def energy(self, apps, period="day", start="", end=""):
        assert period == "month"
        hp = {"start": FEB.isoformat(timespec="seconds"),
              "kWh": {"electricity_heating": 672.0, "heat_delivered_heating": 2688.0,
                      "electricity_dhw": 0.0, "heat_delivered_dhw": 0.0}}
        inv = {"start": JAN.isoformat(timespec="seconds"),
               "kWh": {"total_pv_energy": 100.0, "total_export_energy": 0.0, "total_import_energy": 900.0}}
        return {"appliances": {"hp": {"periods": [hp]}, "inverter": {"periods": [inv]}}}

    async def query_range(self, query, start, end, step):
        assert step == H
        m0 = FEB.timestamp()
        if "rkm_heating_hours" in query:
            values = _series(start, end, lambda t: 100 + (t - JAN.timestamp()) / H * 0.5)
        elif "rkm_dhw_hours" in query:
            values = []
        elif "weather_temperature" in query:
            values = _series(start, end, lambda t: 0.0 if t < m0 else 10.0)
        else:
            per_h = {"electricity_heating": 1.0, "heat_delivered_heating": 4.0}.get(
                next((k for k in ("electricity_heating", "heat_delivered_heating", "electricity_dhw",
                                  "heat_delivered_dhw") if k in query), ""), 0.0)
            values = _series(start, end, lambda t: None if t < m0 else (t - m0) / H * per_h)
        return [{"metric": {}, "values": values}] if values else []


async def test_report_estimates_before_measurement():
    report = await Insights(FakeHistory(), InsightsConfig()).report("2026-01-01", "2026-03-01")
    jan, feb = report["months"]
    assert report["measured_from"]["hp"] == FEB.isoformat(timespec="seconds")
    assert report["factors"]["hp"]["heating"] == {
        "electricity_kwh_per_h": 2.0, "heat_kwh_per_h": 8.0, "measured_hours": 510.0,  # 1 Feb to now (15 Mar 12:00)
        "source": "own"}
    assert report["factors"]["hp"]["dhw"] is None  # no hot water hours anywhere

    # January: 31 days x 12 operating hours, estimated with the February factor
    assert jan["heating"] == {"electricity_kwh": 744.0, "heat_kwh": 2976.0, "source": "estimated"}
    assert jan["dhw"]["missing"] == ["hp"]  # no hours and no factor
    assert jan["pv_share"] == 0.1
    assert jan["weather"] == {"mean_temperature_c": 0.0, "degree_days": 620.0, "heating_days": 31,
                              "days_with_data": 31}
    # February: measured counters only
    assert feb["heating"] == {"electricity_kwh": 672.0, "heat_kwh": 2688.0, "source": "measured"}
    assert feb["dhw"] == {"electricity_kwh": 0.0, "heat_kwh": 0.0, "source": "none"}
    assert feb["pv_share"] is None

    total = report["total"]
    assert total["heating"]["heat_kwh"] == 5664.0
    assert total["heating"]["spf"] == 4.0
    assert total["heating"]["estimated_heat_share"] == pytest.approx(2976 / 5664, abs=0.01)
    assert total["dhw"]["complete"] is False
    assert total["pv_share"] is None  # February has heat pump electricity but no PV figures
    assert report["weather"]["degree_days"] == 900.0
    assert report["weather"]["heating_saving_per_kelvin"] == pytest.approx(59 / 900, abs=1e-3)


async def test_page_and_endpoint_are_served():
    import httpx

    from housevitals.service import build_app

    devices = [DeviceConfig(name="hp", host="127.0.0.1", profile="neo")]
    app = build_app(ServerConfig(devices=devices, service=ServiceConfig(prometheus_url="http://prom")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        page = await client.get("/insights")
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]
        assert 'fetch("api/v1/insights' in page.text
        spec = (await client.get("/openapi.json")).json()
        assert "/api/v1/insights" in spec["paths"]


async def test_report_for_a_period_before_the_measurement():
    """Last year's bill: the counters were only recorded after the period, the factors
    still come from that measured time."""
    report = await Insights(FakeHistory(), InsightsConfig()).report("2026-01-01", "2026-02-01")
    (jan,) = report["months"]
    assert report["factors"]["hp"]["heating"]["source"] == "own"
    assert jan["heating"] == {"electricity_kwh": 744.0, "heat_kwh": 2976.0, "source": "estimated"}
