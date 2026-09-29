"""A data point stored as several series (a label such as the host name changed)."""

import re
import sys
from datetime import datetime
from pathlib import Path

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from housevitals.config import DeviceConfig, ServerConfig, ServiceConfig
from housevitals.hub import Hub
from housevitals.metrics import setup_metrics
from test_history import TZ, FakePrometheus, _history

NOW = datetime(2026, 9, 29, 6, 0, tzinfo=TZ)
MIDNIGHT = datetime(2026, 9, 28, 0, 0, tzinfo=TZ).timestamp()
SWITCH = datetime(2026, 9, 28, 12, 0, tzinfo=TZ).timestamp()  # host name changed at noon


def _split(prom: FakePrometheus, metric: str, value, start=MIDNIGHT - 3600, end=None, step=60):
    """Samples of one data point, stored as host-a before SWITCH and host-b after it."""
    end = end or NOW.timestamp()
    t = start
    while t <= end:
        prom.add(metric, "hp", [(t, value(t))], instance="host-a" if t < SWITCH else "host-b")
        t += step


async def test_history_combines_series():
    prom = FakePrometheus()
    # 40 °C before the switch, 30 °C after it
    _split(prom, "housevitals_flow_temperature_celsius", lambda t: 40.0 if t < SWITCH else 30.0)
    history = _history(prom, NOW)
    result = await history.history(history.hub.get("hp"), ["flow_temperature"],
                                    "2026-09-28T06:00", "", 36)
    flow = result["series"]["flow_temperature"]
    assert (flow["min"], flow["max"]) == (30.0, 40.0)  # both series, not just the first
    assert flow["last"] == 30.0  # the current series, not the frozen old one
    values = [v for _, v in flow["points"]]
    assert values[0] == 40.0 and values[-1] == 30.0 and len(values) == 25  # one point per step


async def test_energy_uses_the_current_counter():
    prom = FakePrometheus()
    # electricity counter: +1 kWh per hour across the switch; heat +4 kWh per hour
    _split(prom, "housevitals_electricity_kWh_total", lambda t: 100 + (t - MIDNIGHT) // 3600)
    _split(prom, "housevitals_heat_delivered_kWh_total", lambda t: 400 + 4 * ((t - MIDNIGHT) // 3600))
    history = _history(prom, NOW)
    result = await history.energy([history.hub.get("hp")], "day", "2026-09-28")
    day = next(p for p in result["appliances"]["hp"]["periods"] if p["start"].startswith("2026-09-28"))
    # The frozen host-a series (last reading at noon) must not be taken at midnight.
    assert day["kWh"]["electricity_total"] == 24
    assert day["derived"]["spf"] == 4.0


async def test_runtime_merges_series():
    prom = FakePrometheus()
    # compressor on 06:00-07:00 (host-a) and 18:00-19:00 (host-b)
    _split(prom, "housevitals_compressor",
           lambda t: 10 if 6 <= (t - MIDNIGHT) / 3600 < 7 or 18 <= (t - MIDNIGHT) / 3600 < 19 else 0,
           start=MIDNIGHT - 60, end=MIDNIGHT + 86400 - 60)
    history = _history(prom, NOW)
    result = await history.runtime(history.hub.get("hp"), "compressor", "2026-09-28", "2026-09-29")
    assert result["starts"] == 2 and result["completed_runs"]["count"] == 2
    assert result["states"]["on"]["hours"] == 2.0
    # the first minute counts as "no data": range windows exclude their start
    assert result["hours_with_data"] == pytest.approx(24, abs=0.02)


def test_instance_label_is_fixed_and_configurable(monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "renamed-host.local")  # must not matter
    for service, expected in ((ServiceConfig(), "housevitals"),
                              (ServiceConfig(instance_id="attic-pi"), "attic-pi")):
        config = ServerConfig(devices=[DeviceConfig(name="hp", host="127.0.0.1", profile="neo")],
                              service=service)
        reader = InMemoryMetricReader()
        provider, _ = setup_metrics(Hub(config), readers=[reader])
        attributes = reader.get_metrics_data().resource_metrics[0].resource.attributes
        assert attributes["service.instance.id"] == expected
        provider.shutdown()


def test_dashboard_queries_combine_series():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import build_grafana_dashboard as dashboards

    per = dashboards.per_appliance
    assert per('housevitals_x{appliance="hp"}') == 'max by (appliance) (housevitals_x{appliance="hp"})'
    assert per('increase(housevitals_e_total{appliance="hp"}[1d] offset -1d)') == \
        'sum by (appliance) (increase(housevitals_e_total{appliance="hp"}[1d] offset -1d))'
    # Every selector in the generated dashboards is combined per appliance.
    selector = re.compile(r"housevitals_\w+\{")
    for dash in dashboards.dashboards().values():
        panels = [p for row in dash["panels"] for p in ([row] if row["type"] != "row" else row["panels"])]
        for panel in panels:
            for t in panel.get("targets", []):
                for m in selector.finditer(t["expr"]):
                    before = t["expr"][:m.start()]
                    assert before.endswith(("max by (appliance) (", "sum by (appliance) (increase(")), t["expr"]
