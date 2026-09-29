"""Generate the Grafana dashboards deploy/grafana/dashboards/home-energy.json (German)
and home-energy-en.json (English).

Colors follow a fixed entity -> color mapping (validated categorical palette, dark
steps, since Grafana defaults to the dark theme): each appliance/energy flow keeps
its color in every panel. Status colors are reserved for reachability/faults.

    python tools/build_grafana_dashboard.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "deploy" / "grafana" / "dashboards" / "home-energy.json"
DS = {"type": "prometheus", "uid": "prometheus"}
M = "housevitals_"

# Categorical palette (dark steps), fixed per entity.
BLUE, ORANGE, AQUA, YELLOW = "#3987e5", "#d95926", "#199e70", "#c98500"
MAGENTA, VIOLET = "#d55181", "#9085e9"
GRAY = "#6e6d68"
GOOD, WARNING, CRITICAL = "#0ca30c", "#fab219", "#d03b3b"

HEAT_PUMPS = [("heatpump1", "WP1", BLUE), ("heatpump2", "WP2", ORANGE)]
INV = 'appliance="inverter"'
FLOW = {"PV": YELLOW, "Haus": BLUE, "Batterie": AQUA, "Netz": ORANGE}
DASH = {"fill": "dash", "dash": [10, 6]}
DOT = {"fill": "dot", "dash": [0, 6]}

_ids = iter(range(1, 1000))


# Daily bars use `increase(x[1d] offset -1d)`: the bar at a day's start covers that
# day, so today's (partial) bar is visible instead of appearing only tomorrow.

# --------------------------------------------------------------------------- helpers
_INCREASE = re.compile(r"increase\((" + M + r"\w+\{[^}]*\})(\[[^\]]+\](?: offset -?\w+)?)\)")
_SUBQUERY = re.compile(r"(sum|count)_over_time\((" + M + r"\w+\{[^}]*\})(\[[^\]:]+:[^\]]+\](?: offset -?\w+)?)\)")
_SELECTOR = re.compile(r"(?<!increase\()(?<!max by \(appliance\) \()(" + M + r"\w+\{[^}]*\})(?![\[\w])")


def per_appliance(expr: str) -> str:
    """Combine all series of each appliance.

    One data point can be stored as several series (a label such as the host name or
    the version changed and a new series started), so every selector is combined per
    appliance: momentary values by max, increases by sum (each series covers its own
    part of the time range).
    """
    expr = _INCREASE.sub(r"sum by (appliance) (increase(\1\2))", expr)
    expr = _SUBQUERY.sub(r"\1_over_time((max by (appliance) (\2))\3)", expr)

    def combine(m: re.Match) -> str:
        # forecast metrics have no appliance label; they are keyed by array and day
        by = "array, day" if m.group(1).startswith(M + "forecast_") else "appliance"
        return f"max by ({by}) ({m.group(1)})"

    return _SELECTOR.sub(combine, expr)


def daily_energy(metric: str, appliance: str, to_kw: float) -> str:
    """kWh per day from a power metric: sum of one-minute samples over the day.

    Used for the heat pumps, whose energy counters are not updated over Modbus. The
    bar at a day's start covers that day (see "offset -1d" above).
    """
    return (f'sum_over_time({M}{metric}{{appliance="{appliance}"}}[1d:1m] offset -1d)'
            f" * {to_kw / 60:.10g}")


def target(expr: str, legend: str = "", instant: bool = False, interval: str | None = None) -> dict:
    expr = per_appliance(expr)
    t = {"datasource": DS, "expr": expr, "legendFormat": legend or "__auto", "range": not instant,
         "instant": instant}
    if interval:
        t["interval"] = interval
    return t


def override(name: str, color: str | None = None, dashed: bool = False, extra: list | None = None,
             dotted: bool = False) -> dict:
    props = []
    if color:
        props.append({"id": "color", "value": {"mode": "fixed", "fixedColor": color}})
    if dashed or dotted:
        props.append({"id": "custom.lineStyle", "value": DOT if dotted else DASH})
    props += extra or []
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def _finish(panel: dict, targets: list[dict]) -> dict:
    for ref, t in zip("ABCDEFGHIJKLMNOP", targets):
        t["refId"] = ref
    panel["id"] = next(_ids)
    panel["datasource"] = DS
    panel["targets"] = targets
    return panel


def stat(title, targets, grid, unit="none", decimals=None, overrides=None, mappings=None,
         description="", color_mode="none", thresholds=None, min_=None, max_=None, only_when_up=True):
    """A "now" tile: the current value from an instant query.

    With only_when_up, a value is shown only while its appliance answers
    (housevitals_up == 1). A tile of an unreachable appliance then shows "No data"
    instead of its last reading as if it were current.
    """
    for t in targets:
        if only_when_up:
            t["expr"] = f"({t['expr']}) and on(appliance) max by (appliance) ({M}up) == 1"
        t["instant"], t["range"] = True, False
    defaults = {"unit": unit, "mappings": mappings or [],
                "color": {"mode": "thresholds"} if thresholds else {"mode": "fixed", "fixedColor": GRAY},
                "thresholds": thresholds or {"mode": "absolute", "steps": [{"color": GRAY, "value": None}]}}
    if decimals is not None:
        defaults["decimals"] = decimals
    if min_ is not None:
        defaults["min"] = min_
    if max_ is not None:
        defaults["max"] = max_
    return _finish({
        "type": "stat", "title": title, "description": description, "gridPos": grid,
        # "Now" tiles always evaluate at the current time, independent of the
        # dashboard range (e.g. when viewing the last 30 days).
        "timeFrom": "1h", "hideTimeOverride": True,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": color_mode, "graphMode": "none",
            # names when there are several values: several targets, or one per label value
            "textMode": "value_and_name" if len(targets) > 1 or "{{" in targets[0]["legendFormat"] else "value",
            "justifyMode": "center", "orientation": "vertical", "wideLayout": True,
            "showPercentChange": False,
        },
    }, targets)


def timeseries(title, targets, grid, unit="none", overrides=None, description="", bars=False,
               min_=None, max_=None, decimals=None, stack=False, interval=None):
    custom = {
        "drawStyle": "bars" if bars else "line", "lineWidth": 2, "lineInterpolation": "smooth",
        "fillOpacity": 85 if bars else 0, "gradientMode": "none", "showPoints": "never",
        "pointSize": 8, "spanNulls": 60_000 if not bars else False,  # gaps after 1 min without data
        "barAlignment": -1, "barWidthFactor": 0.8, "axisBorderShow": False,
        "axisPlacement": "auto", "axisSoftMin": 0 if bars else None,
        "stacking": {"mode": "normal" if stack else "none", "group": "A"},
        "thresholdsStyle": {"mode": "off"},
    }
    custom = {k: v for k, v in custom.items() if v is not None}
    defaults = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"}}
    for key, value in (("min", min_), ("max", max_), ("decimals", decimals)):
        if value is not None:
            defaults[key] = value
    panel = {
        "type": "timeseries", "title": title, "description": description, "gridPos": grid,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True,
                       "calcs": ["lastNotNull", "max"] if not bars else ["sum"]},
            "tooltip": {"mode": "multi", "sort": "none"},
        },
    }
    if interval:
        panel["interval"] = interval
    if bars:
        # Daily bars sit at the start of each day; pin them to 30 days so they never
        # show "data outside time range" when the dashboard looks at a few hours.
        panel["timeFrom"] = "30d"
    if unit == "celsius" and decimals is None:
        panel["fieldConfig"]["defaults"]["decimals"] = 1  # sensors resolve 0.1 K
    return _finish(panel, targets)


def state_timeline(title, targets, grid, mappings, description=""):
    return _finish({
        "type": "state-timeline", "title": title, "description": description, "gridPos": grid,
        "fieldConfig": {"defaults": {"mappings": mappings, "color": {"mode": "fixed", "fixedColor": GRAY},
                                     "custom": {"fillOpacity": 85, "lineWidth": 0}},
                        "overrides": []},
        "options": {"showValue": "never", "rowHeight": 0.8, "mergeValues": True, "alignValue": "left",
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "single", "sort": "none"}},
    }, targets)


def value_map(entries: dict, colors: dict) -> list[dict]:
    return [{"type": "value", "options": {
        str(k): {"text": text, "color": colors[k], "index": i}
        for i, (k, text) in enumerate(entries.items())}}]


def on_off_map(on: str, off: str, on_color: str, off_color: str = GRAY) -> list[dict]:
    """0 = off, any other value = on (the NEO reports e.g. 10 for a running compressor)."""
    return [
        {"type": "value", "options": {"0": {"text": off, "color": off_color, "index": 0}}},
        {"type": "range", "options": {"from": 0.5, "to": 1e9,
                                       "result": {"text": on, "color": on_color, "index": 1}}},
    ]


def row(title, y, collapsed=False, panels=None) -> dict:
    return {"type": "row", "title": title, "collapsed": collapsed, "id": next(_ids),
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": panels or []}


def g(x, y, w, h) -> dict:
    return {"x": x, "y": y, "w": w, "h": h}


def per_hp(metric: str, suffix: str = "", dashed: bool = False, dotted: bool = False):
    """One target + color override per heat pump."""
    targets, overrides = [], []
    for app, short, color in HEAT_PUMPS:
        name = f"{short}{suffix}"
        targets.append(target(f'{M}{metric}{{appliance="{app}"}}', name))
        overrides.append(override(name, color, dashed, dotted=dotted))
    return targets, overrides


# --------------------------------------------------------------------------- panels
def build() -> dict:
    panels: list[dict] = []
    y = 0

    # ---- Jetzt: PV & Batterie
    panels.append(row("Jetzt · PV-Anlage & Batterie", y)); y += 1
    panels += [
        stat("PV-Leistung", [target(f"{M}pv_power_watts{{{INV}}}", "PV")], g(0, y, 4, 5), "watt",
             overrides=[override("PV", YELLOW)]),
        stat("Hausverbrauch", [target(f"{M}load_power_watts{{{INV}}}", "Haus")], g(4, y, 4, 5), "watt",
             overrides=[override("Haus", BLUE)]),
        stat("Batterie", [target(f"{M}battery_power_watts{{{INV}}}", "Batterie")], g(8, y, 4, 5), "watt",
             description="Positiv = Batterie entlädt ins Haus, negativ = Batterie wird geladen.",
             overrides=[override("Batterie", AQUA)]),
        stat("Ladezustand", [target(f"{M}battery_soc_percent{{{INV}}}", "Ladezustand")], g(12, y, 4, 5),
             "percent", decimals=0, min_=0, max_=100, overrides=[override("Ladezustand", AQUA)]),
        stat("Netz", [target(f"{M}grid_power_watts{{{INV}}}", "Netz")], g(16, y, 4, 5), "watt",
             description="Positiv = Bezug aus dem Netz, negativ = Einspeisung.",
             overrides=[override("Netz", ORANGE)]),
        stat("Autarkie heute", [target(
            f"1 - {M}daily_import_energy_kWh{{{INV}}} / ({M}daily_direct_consumption_kWh{{{INV}}}"
            f" + {M}daily_battery_discharge_kWh{{{INV}}} + {M}daily_import_energy_kWh{{{INV}}})",
            "Autarkie")], g(20, y, 4, 5), "percentunit", decimals=0, min_=0, max_=1,
            description="Anteil des heutigen Hausverbrauchs aus PV und Batterie.",
            overrides=[override("Autarkie", AQUA)]),
    ]
    y += 5
    panels += [
        stat("PV-Erzeugung heute", [target(f"{M}daily_pv_energy_kWh{{{INV}}}", "PV")], g(0, y, 4, 4),
             "kwatth", decimals=1),
        stat("Netzbezug heute", [target(f"{M}daily_import_energy_kWh{{{INV}}}", "Bezug")], g(4, y, 4, 4),
             "kwatth", decimals=1),
        stat("Einspeisung heute", [target(f"{M}daily_export_energy_kWh{{{INV}}}", "Einspeisung")],
             g(8, y, 4, 4), "kwatth", decimals=1),
        stat("Batterie geladen / entladen heute", [
            target(f"{M}daily_battery_charge_kWh{{{INV}}}", "geladen"),
            target(f"{M}daily_battery_discharge_kWh{{{INV}}}", "entladen")], g(12, y, 6, 4),
            "kwatth", decimals=1),
        stat("Erreichbar", [target(f'{M}up{{appliance="{a}"}}', label) for a, label in
                            (("heatpump1", "WP1"), ("heatpump2", "WP2"), ("inverter", "Wechselrichter"))],
             g(18, y, 6, 4), color_mode="background", only_when_up=False,
             mappings=value_map({1: "online", 0: "offline"}, {1: GOOD, 0: CRITICAL}),
             description="Letzte Modbus-Abfrage erfolgreich. Keine Anzeige = Dienst liefert keine Daten."),
    ]
    y += 4

    # ---- Jetzt: Wärmepumpen
    # ---- Prognose (Open-Meteo, see README "PV forecast")
    panels.append(row("Prognose · PV", y)); y += 1
    energy = f"{M}forecast_pv_energy_kWh"
    panels += [
        stat("PV-Prognose heute", [target(f'{energy}{{day="today"}}', "heute")], g(0, y, 6, 4),
             "kwatth", decimals=1, only_when_up=False,
             description="Erwartete PV-Erzeugung des ganzen Tages (Open-Meteo, je Modulfeld kalibriert)."),
        stat("PV-Prognose morgen", [target(f'{energy}{{day="tomorrow"}}', "morgen")], g(6, y, 6, 4),
             "kwatth", decimals=1, only_when_up=False,
             description="Erwartete PV-Erzeugung von morgen."),
        stat("Performance Ratio je Modulfeld",
             [target(f'{M}forecast_performance_ratio{{array!=""}}', "{{array}}")], g(12, y, 12, 4),
             "percentunit", decimals=0, only_when_up=False,
             description="Gemessene / modellierte Energie der letzten Tage je Modulfeld. "
                         "Deutlich unter den anderen: Verschattung, Verschmutzung oder ein Defekt."),
    ]
    y += 4

    panels.append(row("Jetzt · Wärmepumpen", y)); y += 1
    hp_tiles = [
        ("Außentemperatur", "outdoor_temperature_celsius", "celsius", 1),
        ("Vorlauf", "flow_temperature_celsius", "celsius", 1),
        ("Warmwasser", "dhw_temperature_celsius", "celsius", 1),
        ("Leistungsaufnahme", "electrical_power_watts", "watt", None),
    ]
    x = 0
    for title, metric, unit, dec in hp_tiles:
        t, o = per_hp(metric)
        panels.append(stat(title, t, g(x, y, 4, 5), unit, decimals=dec, overrides=o))
        x += 4
    onoff = on_off_map("läuft", "aus", BLUE)
    t, _ = per_hp("compressor")
    panels.append(stat("Verdichter", t, g(16, y, 4, 5), mappings=onoff))
    panels.append(stat(
        "JAZ seit Inbetriebnahme",
        [target(f'{M}heat_delivered_kWh_total{{appliance="{a}"}} / {M}electricity_kWh_total{{appliance="{a}"}}', s)
         for a, s, _ in HEAT_PUMPS],
        g(20, y, 4, 5), decimals=2,
        description="Jahresarbeitszahl: gelieferte Wärme / aufgenommener Strom, gesamt."))
    y += 5

    # ---- Verlauf: Energiefluss
    panels.append(row("Verlauf · Energiefluss", y)); y += 1
    panels.append(timeseries(
        "Leistung", [
            target(f"{M}pv_power_watts{{{INV}}}", "PV"),
            target(f"{M}load_power_watts{{{INV}}}", "Haus"),
            target(f"{M}battery_power_watts{{{INV}}}", "Batterie"),
            target(f"{M}grid_power_watts{{{INV}}}", "Netz"),
            target(f'{M}forecast_pv_power_watts{{array="total"}}', "PV-Prognose"),
        ], g(0, y, 16, 9), "watt",
        description="Batterie: positiv = entlädt, negativ = lädt. Netz: positiv = Bezug, negativ = Einspeisung. "
                    "Gestrichelt: prognostizierte PV-Leistung.",
        overrides=[override(n, c) for n, c in FLOW.items()] + [override("PV-Prognose", YELLOW, dashed=True)]))
    panels.append(timeseries(
        "Batterie-Ladezustand", [target(f"{M}battery_soc_percent{{{INV}}}", "Ladezustand")],
        g(16, y, 8, 9), "percent", min_=0, max_=100, overrides=[override("Ladezustand", AQUA)]))
    y += 9
    consumption = (f"increase({M}direct_consumption_kWh_total{{{INV}}}[1d] offset -1d)"
                   f" + increase({M}battery_discharge_kWh_total{{{INV}}}[1d] offset -1d)"
                   f" + increase({M}import_energy_kWh_total{{{INV}}}[1d] offset -1d)")
    panels.append(timeseries(
        "Energie pro Tag", [
            target(f"increase({M}pv_energy_kWh_total{{{INV}}}[1d] offset -1d)", "PV-Erzeugung", interval="1d"),
            target(consumption, "Hausverbrauch", interval="1d"),
            target(f"increase({M}import_energy_kWh_total{{{INV}}}[1d] offset -1d)", "Netzbezug", interval="1d"),
            target(f"increase({M}export_energy_kWh_total{{{INV}}}[1d] offset -1d)", "Einspeisung", interval="1d"),
        ], g(0, y, 12, 9), "kwatth", bars=True, decimals=1, interval="1d",
        description="Tagessummen aus den Gesamtzählern. Der Balken des laufenden Tages wächst bis Mitternacht (Tagesgrenzen in UTC).",
        overrides=[override("PV-Erzeugung", YELLOW), override("Hausverbrauch", BLUE),
                   override("Netzbezug", ORANGE), override("Einspeisung", VIOLET)]))
    t = [target(daily_energy("electrical_power_watts", a, 0.001), s, interval="1d")
         for a, s, _ in HEAT_PUMPS]
    panels.append(timeseries(
        "Wärmepumpen · Strom pro Tag", t, g(12, y, 12, 9), "kwatth", bars=True, decimals=1,
        interval="1d", description="Stromverbrauch je Wärmepumpe und Tag, aus der gemessenen Leistung summiert (die Energiezähler der Wärmepumpen werden über Modbus nicht laufend aktualisiert).",
        overrides=[override(s, c) for _, s, c in HEAT_PUMPS]))
    y += 9

    # ---- Verlauf: Wärmepumpen
    panels.append(row("Verlauf · Wärmepumpen", y)); y += 1
    t1, o1 = per_hp("flow_temperature_celsius", " Vorlauf")
    t2, o2 = per_hp("return_temperature_celsius", " Rücklauf", dashed=True)
    panels.append(timeseries("Vor- und Rücklauf", t1 + t2, g(0, y, 12, 9), "celsius", o1 + o2,
                             description="Durchgezogen = Vorlauf, gestrichelt = Rücklauf."))
    t1, o1 = per_hp("dhw_temperature_celsius", " Warmwasser")
    t2, o2 = per_hp("dhw_setpoint_max_celsius", " Soll", dashed=True)
    t3, o3 = per_hp("dhw_setpoint_min_celsius", " Minimum", dotted=True)
    # min 35 °C: a heat pump with hot water practically disabled has a minimum of e.g.
    # 15 °C, which would squash the curves; its value still shows in the legend.
    panels.append(timeseries("Warmwasser", t1 + t2 + t3, g(12, y, 12, 9), "celsius", o1 + o2 + o3, min_=35,
                             description="Durchgezogen = Ist, gestrichelt = Sollwert (bis dahin wird geladen), "
                                         "gepunktet = Minimum (darunter startet eine Ladung). Die Wärmepumpe "
                                         "lässt das Minimum höchstens 5 K unter dem Sollwert zu."))
    y += 9
    t, o = per_hp("electrical_power_watts")
    panels.append(timeseries("Leistungsaufnahme", t, g(0, y, 12, 8), "watt", o, min_=0))
    t, o = per_hp("outdoor_temperature_celsius")
    panels.append(timeseries("Außentemperatur", t, g(12, y, 12, 8), "celsius", o))
    y += 8
    t, _ = per_hp("compressor")
    panels.append(state_timeline("Verdichter", t, g(0, y, 12, 5), onoff))
    demand = value_map({0: "keine", 10: "Kühlen", 20: "Heizen", 30: "Warmwasser", 40: "extern"},
                       {0: GRAY, 10: AQUA, 20: ORANGE, 30: BLUE, 40: VIOLET})
    t, _ = per_hp("compressor_demand")
    panels.append(state_timeline("Verdichteranforderung", t, g(12, y, 12, 5), demand))
    y += 5
    t = [target(f'({daily_energy("thermal_power_kW", a, 1)}) / ({daily_energy("electrical_power_watts", a, 0.001)})',
                s, interval="1d")
         for a, s, _ in HEAT_PUMPS]
    panels.append(timeseries(
        "Arbeitszahl pro Tag", t, g(0, y, 12, 8), "none", bars=True, decimals=1, interval="1d",
        description="Wärme / Strom je Tag, beide aus der gemessenen Leistung summiert.",
        overrides=[override(s, c) for _, s, c in HEAT_PUMPS]))
    t = [target(daily_energy("thermal_power_kW", a, 1), s, interval="1d")
         for a, s, _ in HEAT_PUMPS]
    panels.append(timeseries(
        "Wärme pro Tag", t, g(12, y, 12, 8), "kwatth", bars=True, decimals=0, interval="1d",
        description="Gelieferte Wärme je Wärmepumpe und Tag, aus der gemessenen thermischen Leistung summiert.",
        overrides=[override(s, c) for _, s, c in HEAT_PUMPS]))
    y += 8

    # ---- Details (collapsed)
    pv_details = []
    dy = y + 1
    strings = [(f"String {i}", c) for i, c in ((1, YELLOW), (2, BLUE), (3, AQUA))]
    pv_details.append(timeseries(
        "PV-Leistung je String", [
            target(f"{M}mppt{i}_voltage_volts{{{INV}}} * {M}mppt{i}_current_amperes{{{INV}}}", f"String {i}")
            for i in (1, 2, 3)], g(0, dy, 12, 8), "watt",
        description="MPPT-Spannung × Strom.", overrides=[override(n, c) for n, c in strings]))
    phases = [("L1", BLUE), ("L2", ORANGE), ("L3", AQUA)]
    pv_details.append(timeseries(
        "Netzleistung je Phase", [
            target(f"{M}grid_power_{p}_watts{{{INV}}}", f"L{i}") for i, p in enumerate("abc", 1)],
        g(12, dy, 12, 8), "watt", description="Positiv = Bezug, negativ = Einspeisung.",
        overrides=[override(n, c) for n, c in phases]))
    dy += 8
    pv_details.append(timeseries(
        "Temperaturen Wechselrichter & Batterie", [
            target(f"{M}inverter_temperature_celsius{{{INV}}}", "Wechselrichter"),
            target(f"{M}battery_temperature_celsius{{{INV}}}", "Batterie")],
        g(0, dy, 12, 8), "celsius",
        overrides=[override("Wechselrichter", BLUE), override("Batterie", AQUA)]))
    pv_details.append(timeseries(
        "Netzspannung je Phase", [
            target(f"{M}phase_{p}_voltage_volts{{{INV}}}", f"L{i}") for i, p in enumerate("abc", 1)],
        g(12, dy, 12, 8), "volt", overrides=[override(n, c) for n, c in phases]))
    panels.append(row("Details · PV-Anlage", y, collapsed=True, panels=pv_details)); y += 1

    hp_details = []
    dy = y + 1
    t1, o1 = per_hp("high_pressure_bar", " Hochdruck")
    t2, o2 = per_hp("low_pressure_bar", " Niederdruck", dashed=True)
    hp_details.append(timeseries("Kältekreis · Drücke", t1 + t2, g(0, dy, 12, 8), "pressurebar", o1 + o2,
                                 description="Durchgezogen = Hochdruck, gestrichelt = Niederdruck."))
    t1, o1 = per_hp("condensation_temperature_celsius", " Kondensation")
    t2, o2 = per_hp("evaporation_temperature_celsius", " Verdampfung", dashed=True)
    hp_details.append(timeseries("Kältekreis · Temperaturen", t1 + t2, g(12, dy, 12, 8), "celsius", o1 + o2,
                                 description="Durchgezogen = Kondensation, gestrichelt = Verdampfung."))
    dy += 8
    t1, o1 = per_hp("source_inlet_temperature_celsius", " Quelle Eintritt")
    t2, o2 = per_hp("buffer_temperature_celsius", " Puffer", dashed=True)
    hp_details.append(timeseries("Wärmequelle & Pufferspeicher", t1 + t2, g(0, dy, 12, 8), "celsius", o1 + o2,
                                 description="Durchgezogen = Wärmequelle (Eintritt), gestrichelt = Pufferspeicher."))
    t, o = per_hp("flow_rate_l_per_min")
    hp_details.append(timeseries("Durchfluss Wärmemengenzähler", t, g(12, dy, 12, 8), "none", o, min_=0,
                                 description="Liter pro Minute."))
    panels.append(row("Details · Wärmepumpen", y, collapsed=True, panels=hp_details)); y += 1

    svc = []
    dy = y + 1
    svc.append(state_timeline(
        "Erreichbarkeit", [target(f'{M}up{{appliance="{a}"}}', label) for a, label in
                           (("heatpump1", "WP1"), ("heatpump2", "WP2"), ("inverter", "Wechselrichter"))],
        g(0, dy, 12, 6), value_map({1: "online", 0: "offline"}, {1: GOOD, 0: CRITICAL})))
    svc.append(timeseries(
        "Abfragedauer (schneller Takt)",
        [target(f'{M}poll_duration_seconds{{group="fast"}}', "{{appliance}}")],
        g(12, dy, 12, 6), "s",
        overrides=[override("heatpump1", BLUE), override("heatpump2", ORANGE), override("inverter", AQUA)]))
    panels.append(row("Dienst · housevitals", y, collapsed=True, panels=svc)); y += 1

    return {
        "uid": "home-energy",
        "title": "Haus · Energie & Wärmepumpen",
        "description": "Live-Werte und Verlauf von PV-Anlage (Sungrow SH20T) und Wärmepumpen (Brötje BLW NEO).",
        "tags": ["housevitals", "energie"],
        "timezone": "browser",
        "weekStart": "monday",
        "refresh": "30s",
        "time": {"from": "now-24h", "to": "now"},
        "timepicker": {"refresh_intervals": ["15s", "30s", "1m", "5m", "15m"]},
        "graphTooltip": 1,  # shared crosshair across panels
        "editable": True,
        "schemaVersion": 41,
        "links": [
            {"title": "Prometheus", "type": "link", "url": "http://localhost:9090", "targetBlank": True},
            {"title": "REST-API", "type": "link", "url": "http://localhost:8080/docs", "targetBlank": True},
        ],
        "templating": {"list": []},
        "annotations": {"list": []},
        "panels": panels,
    }


# --------------------------------------------------------------------------- languages
# The dashboard is written in German; other languages replace only display texts
# (titles, descriptions, legends, state texts). Queries are never touched. A display
# text missing from a table is an error, so no half-translated dashboard is written.
KEEP = {"PV", "L1", "L2", "L3", "String 1", "String 2", "String 3", "Prometheus", "REST-API",
        "online", "offline", "heatpump1", "heatpump2", "inverter", "{{appliance}}", "{{array}}"}
TRANSLATIONS = {
    "en": {
        "Haus · Energie & Wärmepumpen": "Home · Energy & heat pumps",
        "Live-Werte und Verlauf von PV-Anlage (Sungrow SH20T) und Wärmepumpen (Brötje BLW NEO).":
            "Live values and history of the PV system (Sungrow SH20T) and heat pumps (Brötje BLW NEO).",
        "Jetzt · PV-Anlage & Batterie": "Now · PV system & battery",
        "Jetzt · Wärmepumpen": "Now · heat pumps",
        "Verlauf · Energiefluss": "History · energy flow",
        "Verlauf · Wärmepumpen": "History · heat pumps",
        "Details · PV-Anlage": "Details · PV system",
        "Details · Wärmepumpen": "Details · heat pumps",
        "Dienst · housevitals": "Service · housevitals",
        "PV-Leistung": "PV power", "Hausverbrauch": "House consumption", "Haus": "House",
        "Batterie": "Battery", "Ladezustand": "State of charge", "Batterie-Ladezustand": "Battery state of charge",
        "Netz": "Grid", "Autarkie": "Self-sufficiency", "Autarkie heute": "Self-sufficiency today",
        "PV-Erzeugung": "PV generation", "PV-Erzeugung heute": "PV generation today",
        "Netzbezug": "Grid import", "Netzbezug heute": "Grid import today", "Bezug": "Import",
        "Einspeisung": "Feed-in", "Einspeisung heute": "Feed-in today",
        "Batterie geladen / entladen heute": "Battery charged / discharged today",
        "geladen": "charged", "entladen": "discharged",
        "Erreichbar": "Reachable", "Erreichbarkeit": "Reachability", "Wechselrichter": "Inverter",
        "Außentemperatur": "Outdoor temperature", "Vorlauf": "Flow", "Rücklauf": "Return",
        "Warmwasser": "Hot water", "Soll": "setpoint", "Leistungsaufnahme": "Power draw",
        "Verdichter": "Compressor", "Verdichteranforderung": "Compressor demand",
        "läuft": "running", "aus": "off", "keine": "none", "Kühlen": "cooling", "Heizen": "heating",
        "extern": "external",
        "JAZ seit Inbetriebnahme": "SPF since commissioning",
        "Jahresarbeitszahl: gelieferte Wärme / aufgenommener Strom, gesamt.":
            "Seasonal performance factor: heat delivered / electricity used, lifetime.",
        "Leistung": "Power", "Energie pro Tag": "Energy per day",
        "Wärmepumpen · Strom pro Tag": "Heat pumps · electricity per day",
        "Vor- und Rücklauf": "Flow and return", "Arbeitszahl pro Tag": "Performance factor per day",
        "Wärme pro Tag": "Heat per day",
        "PV-Leistung je String": "PV power per string", "Netzleistung je Phase": "Grid power per phase",
        "Netzspannung je Phase": "Grid voltage per phase",
        "Temperaturen Wechselrichter & Batterie": "Inverter & battery temperatures",
        "Kältekreis · Drücke": "Refrigerant circuit · pressures",
        "Kältekreis · Temperaturen": "Refrigerant circuit · temperatures",
        "Hochdruck": "high pressure", "Niederdruck": "low pressure",
        "Kondensation": "condensation", "Verdampfung": "evaporation",
        "Wärmequelle & Pufferspeicher": "Heat source & buffer tank",
        "Quelle Eintritt": "source inlet", "Puffer": "buffer",
        "Durchfluss Wärmemengenzähler": "Heat meter flow rate",
        "Abfragedauer (schneller Takt)": "Poll duration (fast cycle)",
        "Anteil des heutigen Hausverbrauchs aus PV und Batterie.":
            "Share of today's house consumption covered by PV and battery.",
        "Positiv = Batterie entlädt ins Haus, negativ = Batterie wird geladen.":
            "Positive = battery discharges into the house, negative = battery is charging.",
        "Positiv = Bezug aus dem Netz, negativ = Einspeisung.":
            "Positive = import from the grid, negative = feed-in.",
        "Positiv = Bezug, negativ = Einspeisung.": "Positive = import, negative = feed-in.",
        "Batterie: positiv = entlädt, negativ = lädt. Netz: positiv = Bezug, negativ = Einspeisung. "
        "Gestrichelt: prognostizierte PV-Leistung.":
            "Battery: positive = discharging, negative = charging. Grid: positive = import, negative = feed-in. "
            "Dashed: forecast PV power.",
        "Prognose · PV": "Forecast · PV", "PV-Prognose": "PV forecast",
        "PV-Prognose heute": "PV forecast today", "PV-Prognose morgen": "PV forecast tomorrow",
        "heute": "today", "morgen": "tomorrow",
        "Performance Ratio je Modulfeld": "Performance ratio per array",
        "Erwartete PV-Erzeugung des ganzen Tages (Open-Meteo, je Modulfeld kalibriert).":
            "Expected PV generation of the whole day (Open-Meteo, calibrated per array).",
        "Erwartete PV-Erzeugung von morgen.": "Expected PV generation tomorrow.",
        "Gemessene / modellierte Energie der letzten Tage je Modulfeld. "
        "Deutlich unter den anderen: Verschattung, Verschmutzung oder ein Defekt.":
            "Measured / modelled energy of the past days per array. "
            "Clearly below the others: shading, soiling or a defect.",
        "Letzte Modbus-Abfrage erfolgreich. Keine Anzeige = Dienst liefert keine Daten.":
            "Last Modbus request succeeded. Nothing shown = the service delivers no data.",
        "Tagessummen aus den Gesamtzählern. Der Balken des laufenden Tages wächst bis Mitternacht (Tagesgrenzen in UTC).":
            "Daily totals from the lifetime counters. Today's bar grows until midnight (day boundaries in UTC).",
        "Stromverbrauch je Wärmepumpe und Tag, aus der gemessenen Leistung summiert (die Energiezähler der Wärmepumpen werden über Modbus nicht laufend aktualisiert).":
            "Electricity per heat pump and day, summed from the measured power (the heat pumps' energy counters are not updated continuously over Modbus).",
        "Wärme / Strom je Tag, beide aus der gemessenen Leistung summiert.":
            "Heat / electricity per day, both summed from the measured power.",
        "Gelieferte Wärme je Wärmepumpe und Tag, aus der gemessenen thermischen Leistung summiert.":
            "Heat delivered per heat pump and day, summed from the measured thermal power.",
        "Durchgezogen = Vorlauf, gestrichelt = Rücklauf.": "Solid = flow, dashed = return.",
        "Durchgezogen = Ist, gestrichelt = Sollwert (bis dahin wird geladen), gepunktet = Minimum "
        "(darunter startet eine Ladung). Die Wärmepumpe lässt das Minimum höchstens 5 K unter dem "
        "Sollwert zu.":
            "Solid = actual, dashed = setpoint (charged up to it), dotted = minimum (a charge starts "
            "below it). The heat pump keeps the minimum at least 5 K below the setpoint.",
        "Minimum": "minimum",
        "Durchgezogen = Hochdruck, gestrichelt = Niederdruck.": "Solid = high pressure, dashed = low pressure.",
        "Durchgezogen = Kondensation, gestrichelt = Verdampfung.": "Solid = condensation, dashed = evaporation.",
        "Durchgezogen = Wärmequelle (Eintritt), gestrichelt = Pufferspeicher.":
            "Solid = heat source (inlet), dashed = buffer tank.",
        "Liter pro Minute.": "Litres per minute.",
        "MPPT-Spannung × Strom.": "MPPT voltage × current.",
    },
}
HEAT_PUMP_SHORT = {"en": "HP"}  # WP1 -> HP1
LANG_LINKS = {"de": "Deutsch", "en": "English"}


def _translate_text(text: str, lang: str) -> str:
    if text in KEEP:
        return text
    table = TRANSLATIONS[lang]
    if text in table:
        return table[text]
    if m := re.fullmatch(r"WP(\d)(?: (.+))?", text):  # heat pump legends: "WP1 Vorlauf"
        short = f"{HEAT_PUMP_SHORT[lang]}{m.group(1)}"
        return f"{short} {_translate_text(m.group(2), lang)}" if m.group(2) else short
    raise KeyError(f"No {lang} translation for dashboard text {text!r}")


def localize(node, lang: str):
    """Copy of the dashboard with display texts translated."""
    if isinstance(node, list):
        return [localize(n, lang) for n in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key in ("title", "description", "legendFormat", "text") and isinstance(value, str) \
                and value and value != "__auto":
            out[key] = _translate_text(value, lang)
        elif key == "matcher" and isinstance(value.get("options"), str):
            out[key] = {**value, "options": _translate_text(value["options"], lang)}
        else:
            out[key] = localize(value, lang)
    return out


def dashboards() -> dict[str, dict]:
    """{lang: dashboard}; German is the source, the others are translated."""
    base = build()
    result = {"de": base, **{lang: localize(base, lang) for lang in TRANSLATIONS}}
    for lang, dash in result.items():
        if lang != "de":
            dash["uid"] = f"{base['uid']}-{lang}"
        dash["tags"] = [*base["tags"], lang]
        dash["links"] = [link for link in dash["links"] if link["title"] not in LANG_LINKS.values()] + [
            {"title": LANG_LINKS[other], "type": "link", "targetBlank": False,
             "url": f"/d/{base['uid'] if other == 'de' else base['uid'] + '-' + other}"}
            for other in result if other != lang
        ]
    return result


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    for lang, dash in dashboards().items():
        path = OUT if lang == "de" else OUT.with_name(f"{OUT.stem}-{lang}.json")
        path.write_text(json.dumps(dash, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path}")
