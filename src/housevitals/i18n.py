"""Languages for human-facing output.

Division of labour:
- MCP (read by an LLM): data, labels and state names are canonical English
  identifiers; the LLM answers in the user's language and translates them. This
  keeps tool results compact and consistent. Only images (charts) are rendered in
  the requested language, because an LLM cannot translate text inside a picture.
- REST API, charts, Grafana (read by people): localized via `lang` / Accept-Language,
  defaulting to the configured `lang`.

Adding a language: add it to SUPPORTED and MESSAGES (missing keys fall back to
English); register labels come from the profiles' `label_<lang>` fields.
"""

from __future__ import annotations

from typing import Any

SUPPORTED = ("en", "de")
DEFAULT = "en"

_NAMES = {
    "english": "en", "englisch": "en", "anglais": "en",
    "german": "de", "deutsch": "de", "allemand": "de",
}


def normalize(value: str | None, default: str = DEFAULT) -> str:
    """'de', 'de-DE', 'de_AT', 'German', 'Deutsch' -> 'de'; unknown/empty -> default."""
    if not value:
        return default
    value = value.strip().lower()
    if value in _NAMES:
        return _NAMES[value]
    code = value.replace("_", "-").split("-")[0]
    return code if code in SUPPORTED else default


def from_accept_language(header: str | None, default: str = DEFAULT) -> str:
    """First supported language of an Accept-Language header (by q-value)."""
    if not header:
        return default
    ranked = []
    for i, part in enumerate(header.split(",")):
        lang, _, params = part.strip().partition(";")
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        ranked.append((-q, i, lang))
    for _, _, lang in sorted(ranked):
        code = normalize(lang, "")
        if code:
            return code
    return default


MESSAGES: dict[str, dict[str, Any]] = {
    "en": {
        # chart titles and captions
        "chart.energy_flow": "Energy flow",
        "chart.energy_daily": "Energy per day",
        "chart.heatpump": "Heat pump {name}",
        "chart.heatpump_spf": "Performance factor per month",
        "chart.compressor_cycles": "Compressor runtime and starts per day",
        "pv": "PV", "house": "House", "battery": "Battery", "grid": "Grid",
        "soc_short": "SoC", "import": "Grid import", "export": "Feed-in",
        "flow": "Flow", "return": "Return", "dhw": "Hot water", "outdoor": "Outdoor",
        "demand": "Compressor demand", "hours": "Runtime (h)", "starts": "Starts",
        "days": "{n} days",
        "power_note": "battery + = discharging, grid + = import",
        "as_of": "as of", "partial": "hatched = period not complete",
        "stale": "⚠ Outdated – created {time}, update failed",
        "months": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec",
        "state.none": "none", "state.heating": "heating", "state.dhw": "hot water",
        "state.cooling": "cooling", "state.external demand": "external",
        "state.on": "on", "state.off": "off",
    },
    "de": {
        "chart.energy_flow": "Energiefluss",
        "chart.energy_daily": "Energie pro Tag",
        "chart.heatpump": "Wärmepumpe {name}",
        "chart.heatpump_spf": "Arbeitszahl pro Monat",
        "chart.compressor_cycles": "Verdichter: Laufzeit und Starts pro Tag",
        "pv": "PV", "house": "Haus", "battery": "Batterie", "grid": "Netz",
        "soc_short": "Ladung", "import": "Netzbezug", "export": "Einspeisung",
        "flow": "Vorlauf", "return": "Rücklauf", "dhw": "Warmwasser", "outdoor": "Außen",
        "demand": "Verdichteranforderung", "hours": "Laufzeit (h)", "starts": "Starts",
        "days": "{n} Tage",
        "power_note": "Batterie + = entlädt, Netz + = Bezug",
        "as_of": "Stand", "partial": "schraffiert = Zeitraum nicht abgeschlossen",
        "stale": "⚠ Veraltet – erstellt {time}, Aktualisierung fehlgeschlagen",
        "months": "Jan Feb Mär Apr Mai Jun Jul Aug Sep Okt Nov Dez",
        "state.none": "keine", "state.heating": "Heizen", "state.dhw": "Warmwasser",
        "state.cooling": "Kühlen", "state.external demand": "extern",
        "state.on": "an", "state.off": "aus",
    },
}


class Translator:
    """t("chart.heatpump", name="wp1") in one language, falling back to English."""

    def __init__(self, lang: str):
        self.lang = normalize(lang)
        self._messages = {**MESSAGES[DEFAULT], **MESSAGES.get(self.lang, {})}

    def __call__(self, key: str, **kwargs: Any) -> str:
        text = self._messages.get(key, key)
        return text.format(**kwargs) if kwargs else text

    def state(self, state: str) -> str:
        """Translate a canonical state/enum text; unknown states stay as they are."""
        return self._messages.get(f"state.{state}", state)
