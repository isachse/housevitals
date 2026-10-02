"""The NEO-RKM SD log importer (tools/import_rkm_log.py)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import import_rkm_log as rkm  # noqa: E402

DAY1 = """241;1790899362;0
3171;1790899362;1285
3172;1790899362;9347
3173;1790899362;10632
3375;1790899362;592
3188;1790899362;301
3171;1790902963;1286
3375;1790902963;593
"""
DAY2 = "3375;1791072000;594\n3171;1791072000;1286\n\n"


def _card(tmp_path, files):
    folder = tmp_path / "2026"
    folder.mkdir()
    for name, text in files.items():
        (folder / name).write_text(text)
    (folder / "notes.txt").write_text("not a log")  # ignored: not a day file
    return folder


def test_read_and_convert(tmp_path):
    folder = _card(tmp_path, {"20261002": DAY1, "20261004": DAY2})
    series, problems, days = rkm.read_logs([folder])
    assert problems == [] and rkm.check(series) == []
    assert series["3375"] == {1790899362: 592, 1790902963: 593, 1791072000: 594}
    assert [str(a) for a, _ in rkm.gaps(days)] == ["2026-10-03"]  # one day without log
    text = rkm.openmetrics(series, "heatpump2", offset_s=60)
    lines = text.splitlines()
    assert lines[-1] == "# EOF"
    assert ('housevitals_rkm_compressor_starts_total{appliance="heatpump2",instance="housevitals",'
            'job="housevitals",source="rkm_sd"} 592 1790899302') in lines  # clock offset applied
    assert any(l.startswith('housevitals_rkm_log_value{') and 'id="3188"' in l for l in lines)
    # one TYPE line per family, its samples together and in time order
    types = [l for l in lines if l.startswith("# TYPE")]
    assert len(types) == len(set(types)) == 5
    starts = [l for l in lines if l.startswith("housevitals_rkm_compressor_starts_total")]
    assert [int(l.rsplit(" ", 1)[1]) for l in starts] == sorted(int(l.rsplit(" ", 1)[1]) for l in starts)
    assert rkm.monthly(series)["2026-10"]["3375"] == 2


def test_drops_and_bad_lines_are_reported(tmp_path):
    folder = _card(tmp_path, {"20261002": DAY1 + "3375;1790906564;590\nnot;a;line\n"})
    series, problems, _ = rkm.read_logs([folder])
    assert len(problems) == 1 and "unreadable" in problems[0]
    [drop] = rkm.check(series)
    assert "3375" in drop and "593 -> 590" in drop


def test_appliance_name_is_validated():
    with pytest.raises(ValueError):
        rkm.openmetrics({"3375": {1: 1}}, 'x",evil="1')


def test_cli(tmp_path, capsys):
    folder = _card(tmp_path, {"20261002": DAY1})
    out = tmp_path / "rkm.om"
    assert rkm.main([str(folder), "--appliance", "heatpump2", "--out", str(out)]) == 0
    assert out.read_text().endswith("# EOF\n")
    assert "1 days from 2026-10-02" in capsys.readouterr().out


def test_values_from_another_module_are_dropped(tmp_path):
    # the card was briefly in another module: one set of foreign counters in between
    text = ("3172;1000;8800\n3375;1000;2770\n"
            "3172;2000;8805\n"
            "3172;3000;9347\n3375;3000;594\n3171;3000;1287\n"
            "3172;4000;8805\n3172;5000;8806\n")
    folder = _card(tmp_path, {"20261002": text})
    series, _, _ = rkm.read_logs([folder])
    [note] = rkm.remove_foreign(series)
    assert "out of line" in note
    assert 3000 not in series["3172"] and 3000 not in series["3375"] and series["3171"] == {}
    assert series["3172"][4000] == 8805  # the real samples after the spike stay
    assert rkm.check(series) == []
