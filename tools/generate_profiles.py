"""Generate IWR and ISR register profiles from the ha-broetje integration.

The register maps for the Brötje IWR/GTW-08 and ISR Plus Modbus modules are
taken from https://github.com/henrywiechert/ha-broetje (MIT License) and
converted into plain JSON so the MCP server does not depend on Home Assistant.

Usage:
    git clone --depth 1 https://github.com/henrywiechert/ha-broetje /tmp/ha-broetje
    python tools/generate_profiles.py /tmp/ha-broetje
"""

from __future__ import annotations

import csv
import importlib.util
import json
import re
import sys
import types
from pathlib import Path
from typing import Any

OUT_DIR = Path(__file__).resolve().parent.parent / "src" / "housevitals" / "profiles"
PKG = "_ha_broetje"


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_integration(root: Path) -> tuple[types.ModuleType, types.ModuleType, Path]:
    comp = root / "custom_components" / "broetje_heating"
    # Load const + device modules without executing the HA-dependent package __init__.
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(comp)]
    sys.modules[PKG] = pkg
    devices = types.ModuleType(f"{PKG}.devices")
    devices.__path__ = [str(comp / "devices")]
    sys.modules[f"{PKG}.devices"] = devices
    _load(f"{PKG}.const", comp / "const.py")
    iwr = _load(f"{PKG}.devices.iwr", comp / "devices" / "iwr.py")
    isr = _load(f"{PKG}.devices.isr", comp / "devices" / "isr.py")
    return iwr, isr, comp


def _translations(comp: Path, lang: str) -> dict[str, Any]:
    return json.loads((comp / "translations" / f"{lang}.json").read_text())["entity"]


def _label(tr: dict[str, Any], platform: str, key: str, ent: dict[str, Any]) -> str | None:
    name = tr.get(platform, {}).get(key, {}).get("name")
    if name is None:
        return None
    return name.replace("{zone}", str(ent.get("zone_number", ""))).replace(
        "{board}", str(ent.get("board_number", ""))
    )


def _enum_labels(tr: dict[str, Any], platform: str, key: str, enum: dict[int, str]) -> dict[str, str]:
    states = tr.get(platform, {}).get(key, {}).get("state", {})
    return {str(k): states.get(v, v) for k, v in enum.items()}


def _build(
    register_map: dict[str, Any],
    entity_groups: dict[str, dict[str, Any]],
    enum_maps: dict[str, dict[int, str]],
    classification: dict[str, tuple[str | None, bool]],
    tr_en: dict[str, Any],
    tr_de: dict[str, Any],
    category_for: Any,
) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for platform, entities in entity_groups.items():
        for ent_key, ent in entities.items():
            reg_key = ent.get("register")
            if reg_key not in register_map:
                continue
            reg = register_map[reg_key]
            tkey = ent.get("translation_key", ent_key)
            entry: dict[str, Any] = {
                "key": ent_key,
                "address": reg["address"],
                "register_type": reg.get("type", "holding"),
                "data_type": reg.get("data_type", "uint16"),
                "count": reg.get("count", 1),
                "scale": reg.get("scale", 1),
                "unit": ent.get("unit"),
                "label": _label(tr_en, platform, tkey, ent) or ent_key,
                "label_de": _label(tr_de, platform, tkey, ent),
                "category": category_for(ent_key, ent),
                "summary": classification.get(ent_key, (None, True)) == (None, True)
                and "board_number" not in ent,
                "writable": platform in ("number", "select") or bool(reg.get("writable")),
            }
            if "bit" in reg:
                entry["bit"] = reg["bit"]
            if ent.get("zone_number"):
                entry["zone"] = ent["zone_number"]
            enum_name = ent.get("enum_map") or reg.get("enum_map")
            if enum_name and enum_name in enum_maps:
                entry["enum"] = _enum_labels(tr_en, platform, tkey, enum_maps[enum_name])
            out[ent_key] = {k: v for k, v in entry.items() if v is not None}
    return sorted(out.values(), key=lambda e: (e["address"], e.get("bit", -1)))


def main(root: Path) -> None:
    iwr, isr, comp = _load_integration(root)
    tr_en, tr_de = _translations(comp, "en"), _translations(comp, "de")

    # --- IWR / GTW-08 (all 12 zones; the server filters by configured zones) ---
    cfg = iwr.get_iwr_device_config(zones=list(range(1, 13)))
    with (comp / "register_map.csv").open(newline="", encoding="utf-8") as fh:
        csv_cat = {row["ha_sensor_name"]: row["category"] for row in csv.DictReader(fh)}

    def iwr_category(key: str, ent: dict[str, Any]) -> str:
        if ent.get("zone_number"):
            return f"zone{ent['zone_number']}"
        if ent.get("board_number"):
            return "boards"
        cat = csv_cat.get(key) or ent.get("sub_device") or "appliance"
        return re.sub(r"[^a-z0-9]+", "_", cat.lower()).strip("_")

    iwr_regs = _build(
        cfg["register_map"],
        {
            "sensor": cfg["sensors"],
            "binary_sensor": cfg["binary_sensors"],
            "number": cfg.get("numbers", {}),
            "select": cfg.get("selects", {}),
        },
        cfg["enum_maps"],
        cfg["entity_classification"],
        tr_en,
        tr_de,
        iwr_category,
    )
    _write(
        "iwr",
        "Brötje IWR / GTW-08 gateway (current heat pumps, e.g. BLW Eco, BLW Mono)",
        iwr_regs,
    )

    # --- ISR Plus / ISR MODBM (older systems) ---
    isr_prefix = {
        "hc1": "heating_circuit_1",
        "dhw": "dhw",
        "buffer": "buffer",
        "boiler": "boiler",
        "burner": "boiler",
        "chimney": "boiler",
    }

    def isr_category(key: str, ent: dict[str, Any]) -> str:
        if key.startswith("dhw_tank"):
            return "dhw_tank"
        return isr_prefix.get(key.split("_")[0], "general")

    isr_regs = _build(
        isr.ISR_REGISTER_MAP,
        {"sensor": isr.ISR_SENSORS, "binary_sensor": isr.ISR_BINARY_SENSORS},
        isr.ISR_ENUM_MAPS,
        isr.ISR_ENTITY_CLASSIFICATION,
        tr_en,
        tr_de,
        isr_category,
    )
    _write("isr", "Brötje ISR Plus / ISR MODBM module (older heat pumps and boilers)", isr_regs)


def _write(name: str, description: str, registers: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    doc = {
        "name": name,
        "description": description,
        "source": "Generated from https://github.com/henrywiechert/ha-broetje (MIT License)",
        "registers": registers,
    }
    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {path} ({len(registers)} registers)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]))
