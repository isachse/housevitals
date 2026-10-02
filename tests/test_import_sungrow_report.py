"""The iSolarCloud plant report importer (tools/import_sungrow_report.py)."""

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import import_sungrow_report as sg  # noqa: E402

TZ = ZoneInfo("Europe/Berlin")
HEADER = ("﻿Plant report_Year_2026\n"
          "Plant name,Time,Installed power(kWp),Monthly yield(kWh),Total yield(kWh),Monthly equivalent hours(h),"
          "Monthly load consumption(kWh),Total load consumption(kWh),Monthly feed-in(kWh),Total feed-in(kWh),"
          "Energy purchased this month(kWh),Total purchased energy(kWh)\n")


def _row(time, total_yield, load=100.0, feed=10.0, bought=50.0):
    return f"House,{time},18.000,0.00,{total_yield},0.00,0.00,{load},0.00,{feed},0.00,{bought}\n"


def test_read_dst_and_cutoff(tmp_path):
    rows = [
        _row("2026-03-29 01:45", "100.0"),
        _row("2026-03-29 02:00", "100.0"),  # does not exist (clocks jump to 03:00)
        _row("2026-03-29 03:00", "100.5"),
        _row("2026-10-01 12:00", "200.0"),
        "House,2026-12-31 23:45,18.000,,,,,,,,,\n",  # empty: future rows of the year
    ]
    f = tmp_path / "Plant report_Annual report_1.csv"
    f.write_text(HEADER + "".join(rows), encoding="utf-8")
    series, problems = sg.read_reports([f], TZ)
    assert problems == [] and sg.drops(series) == []
    pv = series["housevitals_pv_energy_kWh_total"]
    assert len(pv) == 3  # the nonexistent 02:00 is dropped
    t = sg.local_ts("2026-03-29 03:00", TZ)
    assert t - sg.local_ts("2026-03-29 01:45", TZ) == 15 * 60  # 01:45 CET -> 03:00 CEST
    cutoff = sg.local_ts("2026-10-01 12:00", TZ)
    text, counts = sg.openmetrics(series, "inverter", {"housevitals_pv_energy_kWh_total": cutoff})
    assert counts["housevitals_pv_energy_kWh_total"] == 2  # ends before the first live sample
    assert counts["housevitals_load_energy_kWh_total"] == 3
    assert 'source="sungrow_portal"' in text and text.endswith("# EOF\n")
    assert f"housevitals_pv_energy_kWh_total{{appliance=\"inverter\"" in text


def test_drops_and_wrong_files_are_reported(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text(HEADER + _row("2026-01-01 00:00", "100.0") + _row("2026-01-01 00:15", "99.0"), encoding="utf-8")
    other = tmp_path / "b.csv"
    other.write_text("something else\n", encoding="utf-8")
    series, problems = sg.read_reports([f, other], TZ)
    assert len(problems) == 1 and "not a plant report" in problems[0]
    assert any("drops 100.0 -> 99.0" in d for d in sg.drops(series))


def test_ambiguous_hour_is_taken_once():
    # 02:30 on the night clocks go back exists twice; it maps to one time
    assert sg.local_ts("2026-10-25 02:30", TZ) is not None


def test_make_monotonic():
    t = list(range(0, 900 * 8, 900))
    raw = dict(zip(t, [10.0, 11.0, 5.0, 12.0, 13.0, 9.0, 9.5, 10.0]))  # glitch at 5.0, step -4 at 9.0
    fixed, notes = sg.make_monotonic(raw)
    assert t[2] not in fixed  # glitch dropped
    values = [fixed[x] for x in sorted(fixed)]
    assert values == sorted(values)  # monotonic
    assert values[-1] == 10.0  # still ends at the (live) end value
    assert values[3] - values[2] == 13.0 - 12.0  # increases kept
    assert [n.split()[0] for n in notes] == ["glitch", "correction"]
