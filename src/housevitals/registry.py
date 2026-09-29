"""Register profiles (Brötje heat pumps, Sungrow inverters) and the background poll plan."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

PROFILE_NAMES = ("iwr", "isr", "neo", "sungrow_sh")


@dataclass(frozen=True)
class Register:
    key: str
    address: int
    register_type: str  # "holding" or "input"
    data_type: str  # int16, uint16, int32, uint32, bool, string
    count: int
    scale: float
    label: str
    category: str
    unit: str | None = None
    # Labels in other languages: (("de", "Vorlauftemperatur"), ...) from label_<lang>.
    translations: tuple[tuple[str, str], ...] = ()
    summary: bool = False
    writable: bool = False
    bit: int | None = None
    zone: int | None = None
    enum: dict[str, str] | None = None
    invalid_raw: tuple[int, ...] = ()
    word_order: str = "big"  # 32-bit values: "big" = high word first, "little" = low word first

    def describe(self, lang: str = "en") -> dict[str, Any]:
        info: dict[str, Any] = {
            "key": self.key,
            "label": self.display_label(lang),
            "category": self.category,
            "address": self.address,
            "register_type": self.register_type,
            "data_type": self.data_type,
        }
        if self.unit:
            info["unit"] = self.unit
        if self.scale != 1:
            info["scale"] = self.scale
        if self.bit is not None:
            info["bit"] = self.bit
        if self.enum:
            info["values"] = self.enum
        return info

    @property
    def is_counter(self) -> bool:
        """Monotonic lifetime energy counter (daily counters reset and are gauges)."""
        return (
            self.unit in ("kWh", "Wh")
            and "daily" not in self.key
            and "daily" not in self.category
            and "capacity" not in self.key
        )

    @property
    def is_numeric(self) -> bool:
        return self.data_type != "string"

    def display_label(self, lang: str = "en") -> str:
        return dict(self.translations).get(lang, self.label)

    @property
    def all_labels(self) -> list[str]:
        return [self.label, *(text for _, text in self.translations)]


class PowerIntegration:
    """How to compute an energy counter from a power data point (optional per profile).

    For devices whose energy counters are not updated over Modbus: energy per period
    = sum of power over time. `state`/`value`: only while that state data point has
    this raw value (e.g. compressor_demand == 30 for hot water).
    """

    def __init__(self, counter: str, power: str, to_kw: float, state: str | None = None,
                 value: int | None = None):
        self.counter, self.power, self.to_kw, self.state, self.value = counter, power, to_kw, state, value

    @property
    def keys(self) -> list[str]:
        return [self.power] + ([self.state] if self.state else [])


@dataclass
class Profile:
    name: str
    description: str
    kind: str = "heat_pump"  # "heat_pump" or "inverter"
    registers: dict[str, Register] = field(default_factory=dict)
    power_integration: dict[str, PowerIntegration] = field(default_factory=dict)

    @property
    def categories(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for reg in self.registers.values():
            counts[reg.category] = counts.get(reg.category, 0) + 1
        return counts

    def find(
        self,
        keys: list[str] | None = None,
        category: str | None = None,
        search: str | None = None,
        summary_only: bool = False,
    ) -> list[Register]:
        if keys:
            unknown = [k for k in keys if k not in self.registers]
            if unknown:
                raise KeyError(f"Unknown register key(s): {', '.join(unknown)}")
            return [self.registers[k] for k in keys]
        result = []
        needle = search.lower() if search else None
        for reg in self.registers.values():
            if category and reg.category != category:
                continue
            if summary_only and not reg.summary:
                continue
            if needle and not any(
                needle in s.lower() for s in (reg.key, reg.category, *reg.all_labels)
            ):
                continue
            result.append(reg)
        return result


def load_profile(name: str, zones: list[int] | None = None) -> Profile:
    """Load a bundled profile. For IWR, only registers of the given zones are kept."""
    if name not in PROFILE_NAMES:
        raise ValueError(f"Unknown profile '{name}', expected one of {PROFILE_NAMES}")
    raw = json.loads(
        resources.files("housevitals.profiles").joinpath(f"{name}.json").read_text("utf-8")
    )
    profile = Profile(
        name=raw["name"], description=raw["description"], kind=raw.get("kind", "heat_pump")
    )
    for counter, spec in raw.get("power_integration", {}).items():
        profile.power_integration[counter] = PowerIntegration(
            counter, spec["power"], float(spec["to_kw"]), spec.get("state"), spec.get("value"))
    default_word_order = raw.get("word_order", "big")
    for item in raw["registers"]:
        reg = Register(
            key=item["key"],
            address=item["address"],
            register_type=item.get("register_type", "holding"),
            data_type=item.get("data_type", "uint16"),
            count=item.get("count", 1),
            scale=item.get("scale", 1),
            label=item.get("label", item["key"]),
            category=item.get("category", "general"),
            unit=item.get("unit"),
            translations=tuple(
                (k[len("label_"):], v) for k, v in item.items() if k.startswith("label_") and v
            ),
            summary=item.get("summary", False),
            writable=item.get("writable", False),
            bit=item.get("bit"),
            zone=item.get("zone"),
            enum=item.get("enum"),
            invalid_raw=tuple(item.get("invalid_raw", ())),
            word_order=item.get("word_order", default_word_order),
        )
        if zones is not None and reg.zone is not None and reg.zone not in zones:
            continue
        profile.registers[reg.key] = reg
    for integration in profile.power_integration.values():
        missing = [k for k in (integration.counter, *integration.keys) if k not in profile.registers]
        if missing:
            raise ValueError(f"Profile '{name}': power_integration refers to unknown register(s) {missing}")
    return profile


POLL_GROUPS = ("fast", "slow", "static")


def poll_group(reg: Register, extra_keys: frozenset[str] = frozenset()) -> str | None:
    """Which background poll group a register belongs to, or None for on-demand only.

    fast:   overview values (temperatures, power, status)
    slow:   energy counters (lifetime and daily)
    static: identification (serial, firmware, device type)
    """
    identification = reg.data_type == "string" and reg.category in ("device_info", "device_information")
    if not (reg.summary or reg.is_counter or identification or reg.key in extra_keys):
        return None
    if identification or reg.category in ("device_info", "device_information"):
        return "static"
    if reg.unit in ("kWh", "Wh"):
        return "slow"
    return "fast"
