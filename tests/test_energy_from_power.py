"""Energy statistics from integrated power for devices whose counters are not updated."""

from datetime import datetime

import httpx
import pytest

from housevitals.config import DeviceConfig, ServerConfig, ServiceConfig
from housevitals.history import History
from housevitals.hub import Hub
from housevitals.registry import load_profile
from test_history import TZ, FakePrometheus

NOW = datetime(2026, 9, 29, 6, 0, tzinfo=TZ)
DAY = datetime(2026, 9, 28, 0, 0, tzinfo=TZ).timestamp()
H = 3600


def _history(prom: FakePrometheus, energy_from_power: bool) -> History:
    config = ServerConfig(
        devices=[DeviceConfig(name="hp", host="127.0.0.1", profile="neo", energy_from_power=energy_from_power)],
        service=ServiceConfig(prometheus_url="http://prom"))
    history = History(Hub(config), "http://prom", "Europe/Berlin", transport=httpx.MockTransport(prom.handler))
    history.now = lambda: NOW
    return history


def _day(prom: FakePrometheus, gap: tuple[float, float] | None = None):
    """28.09.: heating 06-09 h (2 kW el, 8 kW th), hot water 18-19 h (2.5 kW el, 7.5 kW th).
    Counters stay frozen all day, as on the real device. Samples every 15 s."""
    t = DAY - 60
    while t < DAY + 24 * H + 60:
        hour = (t - DAY) / H
        if not (gap and gap[0] <= hour < gap[1]):
            heating, dhw = 6 <= hour < 9, 18 <= hour < 19
            prom.add("housevitals_electrical_power_watts", "hp", [(t, 2000 if heating else 2500 if dhw else 0)])
            prom.add("housevitals_thermal_power_kW", "hp", [(t, 8.0 if heating else 7.5 if dhw else 0)])
            prom.add("housevitals_compressor_demand", "hp", [(t, 20 if heating else 30 if dhw else 0)])
            for key, value in (("electricity", 18134), ("electricity_heating", 13229), ("electricity_dhw", 4905),
                               ("heat_delivered", 65555), ("heat_delivered_heating", 50064),
                               ("heat_delivered_dhw", 15491)):
                prom.add(f"housevitals_{key}_kWh_total", "hp", [(t, value)])
        t += 15


def _day_28(result: dict) -> dict:
    return next(p for p in result["appliances"]["hp"]["periods"] if p["start"].startswith("2026-09-28"))


async def test_energy_integrated_from_power():
    prom = FakePrometheus()
    _day(prom)
    history = _history(prom, energy_from_power=True)
    result = await history.energy([history.hub.get("hp")], "day", "2026-09-28", "2026-09-29")
    assert result["appliances"]["hp"]["energy_source"] == "integrated power"
    day = _day_28(result)
    kwh = day["kWh"]
    assert kwh["electricity_total"] == pytest.approx(3 * 2 + 1 * 2.5, abs=0.05)  # 8.5 kWh
    assert kwh["electricity_heating"] == pytest.approx(6.0, abs=0.05)
    assert kwh["electricity_dhw"] == pytest.approx(2.5, abs=0.05)
    assert kwh["heat_delivered_total"] == pytest.approx(3 * 8 + 7.5, abs=0.1)  # 31.5 kWh
    assert day["derived"]["spf"] == pytest.approx(31.5 / 8.5, abs=0.01)
    assert day["derived"]["spf_heating"] == pytest.approx(4.0, abs=0.01)
    assert day["derived"]["spf_dhw"] == pytest.approx(3.0, abs=0.01)
    assert "partial" not in day


async def test_counters_stay_the_default():
    prom = FakePrometheus()
    _day(prom)
    history = _history(prom, energy_from_power=False)
    result = await history.energy([history.hub.get("hp")], "day", "2026-09-28", "2026-09-29")
    assert result["appliances"]["hp"]["energy_source"] == "counters"
    assert _day_28(result)["kWh"]["electricity_total"] == 0  # frozen counters, as before


async def test_gap_in_power_samples_marks_period_partial():
    prom = FakePrometheus()
    _day(prom, gap=(6, 8))  # two hours of the heating run not recorded
    history = _history(prom, energy_from_power=True)
    day = _day_28(await history.energy([history.hub.get("hp")], "day", "2026-09-28", "2026-09-29"))
    assert day["partial"] is True
    assert day["kWh"]["electricity_heating"] == pytest.approx(2.0, abs=0.05)  # not extrapolated


def test_profile_declares_power_integration():
    neo = load_profile("neo").power_integration
    assert neo["electricity_dhw"].power == "electrical_power" and neo["electricity_dhw"].value == 30
    assert neo["heat_delivered_total"].to_kw == 1
    assert load_profile("sungrow_sh").power_integration == {}
