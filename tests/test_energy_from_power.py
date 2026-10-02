"""Derived data points: counters integrated from power by the service, and the energy
statistics that use them for devices whose counters are not updated."""

import json
from datetime import datetime

import httpx
import pytest

from housevitals.config import DeviceConfig, ServerConfig, ServiceConfig
from housevitals.history import History
from housevitals.hub import CacheEntry, Hub
from housevitals.metrics import instrument_plan
from housevitals.registry import DERIVED, load_profile
from test_history import TZ, FakePrometheus

NOW = datetime(2026, 9, 29, 6, 0, tzinfo=TZ)
DAY = datetime(2026, 9, 28, 0, 0, tzinfo=TZ).timestamp()
H = 3600
COUNTERS = (("electricity_total", 18134), ("electricity_heating", 13229), ("electricity_dhw", 4905),
            ("heat_delivered_total", 65555), ("heat_delivered_heating", 50064),
            ("heat_delivered_dhw", 15491))


def _hub(tmp_path=None, energy_from_power=True) -> Hub:
    service = ServiceConfig(prometheus_url="http://prom",
                            derived_state_file=str(tmp_path / "derived.json") if tmp_path else None)
    return Hub(ServerConfig(devices=[DeviceConfig(name="hp", host="127.0.0.1", profile="neo",
                                                  energy_from_power=energy_from_power)], service=service))


def _sample(app, t, power_w, thermal_kw, demand):
    """What a fast poll leaves in the cache."""
    app.cache["electrical_power"] = CacheEntry({"value": power_w, "unit": "W"}, t)
    app.cache["thermal_power"] = CacheEntry({"value": thermal_kw, "unit": "kW"}, t)
    app.cache["compressor_demand"] = CacheEntry({"value": {0: "none", 20: "heating", 30: "dhw"}[demand],
                                                 "raw": demand}, t)
    app.derived_values.update(app, t)


def _run_day(app, gap=None):
    """28.09.: heating 06-09 h (2 kW el, 8 kW th), hot water 18-19 h (2.5 kW el, 7.5 kW th),
    a sample every 15 s."""
    t = DAY
    while t <= DAY + 24 * H:
        hour = (t - DAY) / H
        if not (gap and gap[0] <= hour < gap[1]):
            heating, dhw = 6 <= hour < 9, 18 <= hour < 19
            _sample(app, t, 2000 if heating else 2500 if dhw else 0, 8.0 if heating else 7.5 if dhw else 0,
                    20 if heating else 30 if dhw else 0)
        t += 15


def _value(app, key):
    return app.cache[key].data["value"]


def test_profile_declares_derived_points():
    neo = load_profile("neo")
    rule = neo.derived["electricity_dhw_from_power"]
    assert rule.integrate == "electrical_power" and rule.when_key == "compressor_demand" and rule.when_raw == 30
    assert rule.replaces == "electricity_dhw" and neo.derived["heat_delivered_total_from_power"].scale == 1
    reg = neo.registers["electricity_dhw_from_power"]
    assert reg.register_type == DERIVED and reg.is_counter and "address" not in reg.describe()
    assert load_profile("sungrow_sh").derived == {}


@pytest.mark.parametrize("item, message", [
    ({"key": "x", "unit": "kWh"}, "needs an operation"),
    ({"key": "x", "integrate": "electrical_power", "average": True}, "unknown option"),
    ({"key": "x", "integrate": "no_such_power"}, "unknown register"),
    ({"key": "x", "integrate": "electrical_power", "replaces": "nope"}, "unknown register"),
    ({"key": "x", "integrate": "compressor_demand"}, "not a numeric measurement"),
    ({"key": "x", "integrate": "electrical_power", "when": {"key": "compressor_demand", "raw": "dhw"}},
     "integer"),
    ({"key": "electrical_power", "integrate": "electrical_power"}, "already used"),
])
def test_derived_rules_are_validated(item, message):
    from housevitals.registry import _add_derived

    with pytest.raises(ValueError, match=message):
        _add_derived(load_profile("neo"), item)


def test_integration_per_state():
    app = _hub().get("hp")
    _run_day(app)
    assert _value(app, "electricity_total_from_power") == pytest.approx(3 * 2 + 1 * 2.5, abs=0.02)
    assert _value(app, "electricity_heating_from_power") == pytest.approx(6.0, abs=0.02)
    assert _value(app, "electricity_dhw_from_power") == pytest.approx(2.5, abs=0.02)
    assert _value(app, "heat_delivered_total_from_power") == pytest.approx(31.5, abs=0.05)
    assert app.status[DERIVED].last_success == DAY + 24 * H


def test_gaps_add_nothing_and_are_reported():
    app = _hub().get("hp")
    _run_day(app, gap=(6, 8))  # two hours of the heating run not polled (outage)
    assert _value(app, "electricity_heating_from_power") == pytest.approx(2.0, abs=0.02)  # not extrapolated
    gaps = app.derived_values.describe("hp")["electricity_heating_from_power"]["uncovered_s"]
    assert gaps == pytest.approx(2 * H, abs=30)


def test_a_stale_sample_is_not_counted_twice():
    app = _hub().get("hp")
    _sample(app, DAY, 2000, 8.0, 20)
    _sample(app, DAY + 15, 2000, 8.0, 20)
    once = _value(app, "electricity_total_from_power")
    app.derived_values.update(app, DAY + 30)  # a poll in which the power read failed: same entry
    assert _value(app, "electricity_total_from_power") == once == pytest.approx(2 * 15 / 3600, abs=1e-4)


def test_counters_survive_a_restart(tmp_path):
    hub = _hub(tmp_path)
    _run_day(hub.get("hp"))
    hub.derived.save()
    state = json.loads((tmp_path / "derived.json").read_text())
    assert state["counters"]["hp"]["electricity_total_from_power"]["value"] == pytest.approx(8.5, abs=0.02)

    again = _hub(tmp_path).get("hp")
    _sample(again, DAY + 30 * H, 2000, 8.0, 20)  # first sample after the restart: nothing added
    _sample(again, DAY + 30 * H + 36, 2000, 8.0, 20)
    assert _value(again, "electricity_total_from_power") == pytest.approx(8.5 + 0.02, abs=0.02)


async def test_derived_values_are_never_read_over_modbus():
    app = _hub().get("hp")
    reg = app.profile.registers["electricity_total_from_power"]
    calls = []

    async def no_modbus(regs):
        calls.append(regs)
        raise AssertionError("Modbus read")

    app.client.read = no_modbus
    result = await app.read([reg])
    assert result[reg.key]["value"] is None and "next poll" in result[reg.key]["error"] and not calls
    _sample(app, DAY, 1000, 4.0, 20)
    assert (await app.read([reg]))[reg.key]["value"] == 0
    with pytest.raises(ValueError, match="derived"):
        await app.read_now(reg)


# ---------------------------------------------------------------- energy statistics
def _history(prom: FakePrometheus, energy_from_power: bool) -> History:
    hub = _hub(energy_from_power=energy_from_power)
    history = History(hub, "http://prom", "Europe/Berlin", transport=httpx.MockTransport(prom.handler))
    history.now = lambda: NOW
    return history


def _recorded_day(prom: FakePrometheus):
    """The counters as Prometheus records them: device counters frozen, derived ones growing."""
    hub = _hub()
    app = hub.get("hp")
    names = {s.reg.key: inst.prometheus_name for inst in instrument_plan(hub) for s in inst.series}
    t = DAY - 60
    while t < DAY + 24 * H + 60:
        if t >= DAY:
            hour = (t - DAY) / H
            heating, dhw = 6 <= hour < 9, 18 <= hour < 19
            _sample(app, t, 2000 if heating else 2500 if dhw else 0, 8.0 if heating else 7.5 if dhw else 0,
                    20 if heating else 30 if dhw else 0)
        for key, value in COUNTERS:
            prom.add(names[key], "hp", [(t, value)])
            derived = f"{key}_from_power"
            prom.add(names[derived], "hp", [(t, app.cache[derived].data["value"] if derived in app.cache else 0.0)])
        t += 15


def _day_28(result: dict) -> dict:
    return next(p for p in result["appliances"]["hp"]["periods"] if p["start"].startswith("2026-09-28"))


async def test_energy_uses_the_derived_counters():
    prom = FakePrometheus()
    _recorded_day(prom)
    history = _history(prom, energy_from_power=True)
    result = await history.energy([history.hub.get("hp")], "day", "2026-09-28", "2026-09-29")
    assert result["appliances"]["hp"]["energy_source"] == "integrated power"
    day = _day_28(result)
    kwh = day["kWh"]
    # reported under the device counters' keys, so callers and derived figures are unchanged
    assert kwh["electricity_total"] == pytest.approx(8.5, abs=0.05)
    assert kwh["electricity_heating"] == pytest.approx(6.0, abs=0.05)
    assert kwh["heat_delivered_total"] == pytest.approx(31.5, abs=0.1)
    assert not any(k.endswith("_from_power") for k in kwh)
    assert day["derived"]["spf"] == pytest.approx(31.5 / 8.5, abs=0.01)
    assert day["derived"]["spf_heating"] == pytest.approx(4.0, abs=0.01)
    assert day["derived"]["spf_dhw"] == pytest.approx(3.0, abs=0.01)
    # one instant query per boundary for all counters, no power integration at query time
    assert not any("sum_over_time" in q for q in prom.queries)


async def test_counters_stay_the_default():
    prom = FakePrometheus()
    _recorded_day(prom)
    history = _history(prom, energy_from_power=False)
    result = await history.energy([history.hub.get("hp")], "day", "2026-09-28", "2026-09-29")
    assert result["appliances"]["hp"]["energy_source"] == "counters"
    kwh = _day_28(result)["kWh"]
    assert kwh["electricity_total"] == 0  # frozen device counter, as before
    assert kwh["electricity_total_from_power"] == pytest.approx(8.5, abs=0.05)  # shown alongside
