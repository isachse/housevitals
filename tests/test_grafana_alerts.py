"""The generated Grafana alert rules must be accepted by Grafana's provisioning.

An invalid rule file stops Grafana from starting at all, so check the rules Grafana
validates on start: links to a dashboard need both annotations, the linked panel and
query must exist, and the file in the repository is the generated one.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_grafana_dashboard as gen  # noqa: E402


def _panels(dashboard):
    for p in dashboard["panels"]:
        yield p
        yield from p.get("panels", [])


def test_alert_rules_are_valid_and_linked():
    dashboards = gen.dashboards()
    rules = gen.alert_rules(dashboards["home-energy"])
    ids = {p["id"] for p in _panels(dashboards["home-energy"])}
    group = rules["groups"][0]
    assert rules["apiVersion"] == 1 and group["folder"] and group["interval"]
    uids = [r["uid"] for r in group["rules"]]
    assert len(uids) == len(set(uids))
    for rule in group["rules"]:
        notes = rule["annotations"]
        assert ("__dashboardUid__" in notes) == ("__panelId__" in notes)  # Grafana refuses one alone
        assert int(notes["__panelId__"]) in ids and notes["__dashboardUid__"] == "home-energy"
        refs = {d["refId"] for d in rule["data"]}
        assert rule["condition"] in refs and rule["data"][0]["model"]["expr"]
        assert all(d["model"].get("expression", "A") in refs for d in rule["data"])
    assert group["rules"][0]["data"][0]["model"]["expr"] == gen.SPACE_FOR_10_YEARS  # same as the tile


def test_committed_rules_are_the_generated_ones():
    dashboards = gen.dashboards()
    on_disk = json.loads(gen.ALERTS.read_text(encoding="utf-8"))
    assert on_disk == gen.alert_rules(dashboards["home-energy"]), "run tools/build_grafana_dashboard.py"
