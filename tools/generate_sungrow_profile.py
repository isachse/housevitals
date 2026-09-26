"""Generate the Sungrow SH hybrid inverter profile (SH*RS/RT/T, e.g. SH20T).

Register addresses, types and scales come from the MIT-licensed
https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant
(modbus_sungrow.yaml, addresses already 0-based). Keys, German labels,
categories, overview flags and enum texts are curated below.

Usage (needs PyYAML):
    git clone --depth 1 https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant /tmp/sg
    python tools/generate_sungrow_profile.py /tmp/sg/modbus_sungrow.yaml
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

OUT = Path(__file__).resolve().parent.parent / "src" / "housevitals" / "profiles" / "sungrow_sh.json"

RUNNING_STATE = {
    0x0000: "Running", 0x0001: "Stop", 0x0002: "Key stop", 0x0004: "Emergency stop",
    0x0008: "Standby", 0x0010: "Initial standby", 0x0014: "Microgrid operation",
    0x0020: "Starting", 0x0040: "Running", 0x0041: "Off-grid charge",
    0x0080: "Derating running", 0x0100: "Fault", 0x0200: "Update failed",
    0x0400: "Running in maintain mode", 0x0800: "Running in forced mode",
    0x1000: "Running (off-grid)", 0x1111: "Uninitialized", 0x1200: "Initial standby",
    0x1300: "Key stop", 0x1400: "Standby", 0x1500: "Emergency stop", 0x1600: "Starting",
    0x1700: "AFCI self-test shutdown", 0x1800: "Intelligent station building status",
    0x1900: "Safe mode", 0x2000: "Open loop", 0x2500: "Communication fault",
    0x2501: "Restarting", 0x4000: "Running in external EMS mode",
    0x4001: "Emergency charging operation", 0x5500: "Fault", 0x8000: "Stop",
    0x8100: "Derating running",
}
DEVICE_TYPES = {
    0x0D06: "SH3K6", 0x0D07: "SH4K6", 0x0D09: "SH5K-20", 0x0D03: "SH5K-V13",
    0x0D0A: "SH3K6-30", 0x0D0B: "SH4K6-30", 0x0D0C: "SH5K-30", 0x0D17: "SH3.0RS",
    0x0D0D: "SH3.6RS", 0x0D18: "SH4.0RS", 0x0D0F: "SH5.0RS", 0x0D10: "SH6.0RS",
    0x0D1A: "SH8.0RS", 0x0D1B: "SH10RS", 0x0E00: "SH5.0RT", 0x0E01: "SH6.0RT",
    0x0E02: "SH8.0RT", 0x0E03: "SH10RT", 0x0E10: "SH5.0RT-20", 0x0E11: "SH6.0RT-20",
    0x0E12: "SH8.0RT-20", 0x0E13: "SH10RT-20", 0x0E0C: "SH5.0RT-V112",
    0x0E0D: "SH6.0RT-V112", 0x0E0E: "SH8.0RT-V112", 0x0E0F: "SH10RT-V112",
    0x0E08: "SH5.0RT-V122", 0x0E09: "SH6.0RT-V122", 0x0E0A: "SH8.0RT-V122",
    0x0E0B: "SH10RT-V122", 0x0E20: "SH5T", 0x0E21: "SH6T", 0x0E22: "SH8T",
    0x0E23: "SH10T", 0x0E24: "SH12T", 0x0E25: "SH15T", 0x0E26: "SH20T",
    0x0E28: "SH25T", 0x0D27: "MG5RL", 0x0D28: "MG6RL",
}
ENABLE_AA_55 = {0xAA: "enabled", 0x55: "disabled"}
ENUMS = {
    "Running state raw": RUNNING_STATE,
    "Sungrow device type code": DEVICE_TYPES,
    "EMS mode selection raw": {0: "Self-consumption", 2: "Forced mode", 3: "External EMS", 4: "VPP"},
    "Battery forced charge discharge cmd raw": {0xAA: "Forced charge", 0xBB: "Forced discharge", 0xCC: "Stop"},
    "Load adjustment mode selection raw": {0: "Timing", 1: "ON/OFF", 2: "Power optimization", 3: "Disabled"},
    "Load adjustment mode enable raw": ENABLE_AA_55,
    "Backup mode raw": ENABLE_AA_55,
    "Export power limit mode raw": ENABLE_AA_55,
    "Active power limitation raw": ENABLE_AA_55,
    "APL shutdown at zero raw": ENABLE_AA_55,
}

# YAML name -> (key, English label, German label, category, in overview)
CURATED: dict[str, tuple[str, str, str, str, bool]] = {
    "Sungrow Version 1": ("firmware_part_1", "Firmware string part 1", "Firmware Teil 1", "device_info", False),
    "Sungrow Version 2": ("firmware_part_2", "Firmware string part 2", "Firmware Teil 2", "device_info", False),
    "Sungrow Version 3": ("firmware_part_3", "Firmware string part 3", "Firmware Teil 3", "device_info", False),
    "Sungrow Version 4 (Sungrow Battery)": ("firmware_part_4_battery", "Firmware string part 4 (Sungrow battery)", "Firmware Teil 4 (Sungrow-Batterie)", "device_info", False),
    "Sungrow Protocol Version": ("protocol_version", "Protocol version", "Protokollversion", "device_info", False),
    "Sungrow Arm Software": ("arm_software", "ARM software version", "ARM-Softwareversion", "device_info", False),
    "Sungrow DSP Software": ("dsp_software", "DSP software version", "DSP-Softwareversion", "device_info", False),
    "Sungrow inverter serial": ("serial_number", "Serial number", "Seriennummer", "device_info", False),
    "Sungrow device type code": ("device_type", "Device type", "Gerätetyp", "device_info", True),
    "Inverter rated output": ("rated_output_power", "Rated output power", "Nennleistung", "device_info", False),
    "Inverter Firmware Version": ("inverter_firmware", "Inverter firmware version", "Wechselrichter-Firmware", "device_info", False),
    "Communication Module Firmware Version": ("comm_module_firmware", "Communication module firmware", "Kommunikationsmodul-Firmware", "device_info", False),
    "Battery Firmware Version": ("battery_firmware", "Battery firmware version", "Batterie-Firmware", "device_info", False),
    "Daily PV generation & battery discharge": ("daily_output_energy", "Daily output energy (PV + battery discharge)", "Tagesertrag Ausgang (PV + Batterieentladung)", "energy_daily", False),
    "Total PV generation & battery discharge": ("total_output_energy", "Total output energy (PV + battery discharge)", "Gesamtertrag Ausgang (PV + Batterieentladung)", "energy_total", False),
    "Inverter temperature": ("inverter_temperature", "Inverter temperature", "Wechselrichtertemperatur", "inverter", True),
    "MPPT1 voltage": ("mppt1_voltage", "MPPT1 voltage", "MPPT1 Spannung", "pv", False),
    "MPPT1 current": ("mppt1_current", "MPPT1 current", "MPPT1 Strom", "pv", False),
    "MPPT2 voltage": ("mppt2_voltage", "MPPT2 voltage", "MPPT2 Spannung", "pv", False),
    "MPPT2 current": ("mppt2_current", "MPPT2 current", "MPPT2 Strom", "pv", False),
    "MPPT3 voltage": ("mppt3_voltage", "MPPT3 voltage", "MPPT3 Spannung", "pv", False),
    "MPPT3 current": ("mppt3_current", "MPPT3 current", "MPPT3 Strom", "pv", False),
    "MPPT4 voltage": ("mppt4_voltage", "MPPT4 voltage", "MPPT4 Spannung", "pv", False),
    "MPPT4 current": ("mppt4_current", "MPPT4 current", "MPPT4 Strom", "pv", False),
    "Total DC power": ("pv_power", "PV power (total DC)", "PV-Leistung (DC gesamt)", "pv", True),
    "Phase A voltage": ("phase_a_voltage", "Phase A voltage", "Spannung L1", "inverter", False),
    "Phase B voltage": ("phase_b_voltage", "Phase B voltage", "Spannung L2", "inverter", False),
    "Phase C voltage": ("phase_c_voltage", "Phase C voltage", "Spannung L3", "inverter", False),
    "Phase A current": ("phase_a_current", "Phase A current", "Strom L1", "inverter", False),
    "Phase B current": ("phase_b_current", "Phase B current", "Strom L2", "inverter", False),
    "Phase C current": ("phase_c_current", "Phase C current", "Strom L3", "inverter", False),
    "Total active power": ("inverter_active_power", "Inverter active power", "Wechselrichter-Wirkleistung", "inverter", False),
    "Reactive power": ("reactive_power", "Reactive power", "Blindleistung", "inverter", False),
    "Power factor": ("power_factor", "Power factor", "Leistungsfaktor", "inverter", False),
    "Grid frequency": ("grid_frequency", "Grid frequency", "Netzfrequenz", "grid", False),
    "Meter active power": ("grid_power", "Grid power at meter (positive = import, negative = export)", "Netzleistung am Zähler (positiv = Bezug, negativ = Einspeisung)", "grid", True),
    "Meter phase A active power": ("grid_power_a", "Grid power phase A (positive = import)", "Netzleistung L1 (positiv = Bezug)", "grid", False),
    "Meter phase B active power": ("grid_power_b", "Grid power phase B (positive = import)", "Netzleistung L2 (positiv = Bezug)", "grid", False),
    "Meter phase C active power": ("grid_power_c", "Grid power phase C (positive = import)", "Netzleistung L3 (positiv = Bezug)", "grid", False),
    "Meter phase A voltage": ("meter_voltage_a", "Meter voltage phase A", "Zähler Spannung L1", "grid", False),
    "Meter phase B voltage": ("meter_voltage_b", "Meter voltage phase B", "Zähler Spannung L2", "grid", False),
    "Meter phase C voltage": ("meter_voltage_c", "Meter voltage phase C", "Zähler Spannung L3", "grid", False),
    "Meter phase A current": ("meter_current_a", "Meter current phase A", "Zähler Strom L1", "grid", False),
    "Meter phase B current": ("meter_current_b", "Meter current phase B", "Zähler Strom L2", "grid", False),
    "Meter phase C current": ("meter_current_c", "Meter current phase C", "Zähler Strom L3", "grid", False),
    "Export power raw": ("export_power", "Export power (positive = export, negative = import)", "Einspeiseleistung (positiv = Einspeisung, negativ = Bezug)", "grid", False),
    "Export power limit min": ("export_limit_min", "Export power limit range min", "Einspeiselimit Bereich min", "settings", False),
    "Export power limit max": ("export_limit_max", "Export power limit range max", "Einspeiselimit Bereich max", "settings", False),
    "Battery power": ("battery_power", "Battery power (positive = discharging, negative = charging)", "Batterieleistung (positiv = Entladen, negativ = Laden)", "battery", True),
    "BDC rated power": ("bdc_rated_power", "Battery converter rated power", "Nennleistung Batteriewandler", "battery", False),
    "Battery current": ("battery_current", "Battery current", "Batteriestrom", "battery", False),
    "BMS max. charging current": ("bms_max_charge_current", "BMS max. charging current", "BMS max. Ladestrom", "battery", False),
    "BMS max. discharging current": ("bms_max_discharge_current", "BMS max. discharging current", "BMS max. Entladestrom", "battery", False),
    "Battery capacity high precision": ("battery_capacity", "Battery capacity", "Batteriekapazität", "battery", False),
    "Battery voltage": ("battery_voltage", "Battery voltage", "Batteriespannung", "battery", False),
    "Battery level": ("battery_soc", "Battery state of charge", "Batterie-Ladezustand", "battery", True),
    "Battery state of health": ("battery_soh", "Battery state of health", "Batterie-Gesundheitszustand", "battery", False),
    "Battery temperature": ("battery_temperature", "Battery temperature", "Batterietemperatur", "battery", False),
    "Backup phase A power": ("backup_power_a", "Backup power phase A", "Notstrom Leistung L1", "backup", False),
    "Backup phase B power": ("backup_power_b", "Backup power phase B", "Notstrom Leistung L2", "backup", False),
    "Backup phase C power": ("backup_power_c", "Backup power phase C", "Notstrom Leistung L3", "backup", False),
    "Total backup power": ("backup_power", "Backup power total", "Notstrom Leistung gesamt", "backup", False),
    "Running state raw": ("running_state", "Running state", "Betriebszustand", "inverter", True),
    "Power Flow Status": ("power_flow_status", "Power flow status bits", "Energiefluss-Statusbits", "inverter", False),
    "Load power": ("load_power", "House load power", "Hausverbrauch", "load", True),
    "Daily PV generation": ("daily_pv_energy", "PV generation today", "PV-Erzeugung heute", "energy_daily", True),
    "Total PV generation": ("total_pv_energy", "PV generation total", "PV-Erzeugung gesamt", "energy_total", True),
    "Daily exported energy from PV": ("daily_pv_export_energy", "Export from PV today", "Einspeisung aus PV heute", "energy_daily", False),
    "Total exported energy from PV": ("total_pv_export_energy", "Export from PV total", "Einspeisung aus PV gesamt", "energy_total", False),
    "Daily battery charge from PV": ("daily_battery_charge_from_pv", "Battery charge from PV today", "Batterieladung aus PV heute", "energy_daily", False),
    "Total battery charge from PV": ("total_battery_charge_from_pv", "Battery charge from PV total", "Batterieladung aus PV gesamt", "energy_total", False),
    "Daily direct energy consumption": ("daily_direct_consumption", "Direct PV self-consumption today", "Direkter PV-Eigenverbrauch heute", "energy_daily", False),
    "Total direct energy consumption": ("total_direct_consumption", "Direct PV self-consumption total", "Direkter PV-Eigenverbrauch gesamt", "energy_total", False),
    "Daily battery discharge": ("daily_battery_discharge", "Battery discharge today", "Batterieentladung heute", "energy_daily", True),
    "Total battery discharge": ("total_battery_discharge", "Battery discharge total", "Batterieentladung gesamt", "energy_total", False),
    "Daily imported energy": ("daily_import_energy", "Grid import today", "Netzbezug heute", "energy_daily", True),
    "Total imported energy": ("total_import_energy", "Grid import total", "Netzbezug gesamt", "energy_total", True),
    "Daily battery charge": ("daily_battery_charge", "Battery charge today", "Batterieladung heute", "energy_daily", True),
    "Total battery charge": ("total_battery_charge", "Battery charge total", "Batterieladung gesamt", "energy_total", False),
    "Daily exported energy": ("daily_export_energy", "Grid export today", "Netzeinspeisung heute", "energy_daily", True),
    "Total exported energy": ("total_export_energy", "Grid export total", "Netzeinspeisung gesamt", "energy_total", True),
    "Load adjustment mode selection raw": ("load_adjustment_mode", "Load adjustment mode", "Lastregelungsmodus", "settings", False),
    "Load adjustment mode enable raw": ("load_adjustment_enabled", "Load adjustment", "Lastregelung", "settings", False),
    "EMS mode selection raw": ("ems_mode", "EMS mode", "EMS-Modus", "settings", True),
    "Battery forced charge discharge cmd raw": ("battery_forced_cmd", "Battery forced charge/discharge command", "Batterie Zwangsladung/-entladung", "settings", False),
    "Battery forced charge discharge power": ("battery_forced_power", "Battery forced charge/discharge power", "Leistung Zwangsladung/-entladung", "settings", False),
    "Battery max SoC": ("battery_max_soc", "Battery max. state of charge", "Batterie max. Ladezustand", "settings", False),
    "Battery min SoC": ("battery_min_soc", "Battery min. state of charge", "Batterie min. Ladezustand", "settings", False),
    "Export power limit": ("export_power_limit", "Export power limit", "Einspeiselimit", "settings", False),
    "Backup mode raw": ("backup_mode", "Backup mode", "Notstrommodus", "settings", False),
    "Export power limit mode raw": ("export_limit_mode", "Export power limitation", "Einspeisebegrenzung", "settings", False),
    "Active power limitation raw": ("active_power_limitation", "Active power limitation", "Wirkleistungsbegrenzung", "settings", False),
    "Active power limitation ratio raw": ("active_power_limitation_ratio", "Active power limitation ratio", "Wirkleistungsbegrenzung Anteil", "settings", False),
    "Battery reserved SoC for backup": ("battery_backup_reserve_soc", "Battery reserve for backup", "Batteriereserve für Notstrom", "settings", False),
    "APL shutdown at zero raw": ("apl_shutdown_at_zero", "Shutdown at zero active power limit", "Abschaltung bei Wirkleistungslimit 0", "settings", False),
    "Battery max charge power": ("battery_max_charge_power", "Battery max. charge power", "Batterie max. Ladeleistung", "settings", False),
    "Battery max discharge power": ("battery_max_discharge_power", "Battery max. discharge power", "Batterie max. Entladeleistung", "settings", False),
    "Battery charging start power": ("battery_charge_start_power", "Battery charging start power", "Batterie Ladestartleistung", "settings", False),
    "Battery discharging start power": ("battery_discharge_start_power", "Battery discharging start power", "Batterie Entladestartleistung", "settings", False),
}

# Power flow status (register 13001) bits, exposed as individual booleans.
POWER_FLOW_BITS = [
    (0, "pv_generating", "PV generating", "PV erzeugt", True),
    (1, "battery_charging", "Battery charging", "Batterie lädt", True),
    (2, "battery_discharging", "Battery discharging", "Batterie entlädt", True),
    (3, "load_positive", "Positive load power", "Positive Lastleistung", False),
    (4, "exporting", "Exporting to grid", "Einspeisung ins Netz", True),
    (5, "importing", "Importing from grid", "Bezug aus dem Netz", True),
    (7, "load_negative", "Negative load power", "Negative Lastleistung", False),
]

UNITS = {"W": "W", "kWh": "kWh", "V": "V", "A": "A", "Hz": "Hz", "%": "%", "°C": "°C"}


def main(yaml_path: Path) -> None:
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!secret", lambda loader, node: None)
    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    sensors = yaml.load(yaml_path.read_text(), Loader=Loader)["modbus"][0]["sensors"]

    missing = [s["name"] for s in sensors if s["name"] not in CURATED]
    if missing:
        sys.exit(f"Uncurated registers in YAML: {missing}")

    registers = []
    for s in sensors:
        key, label, label_de, category, summary = CURATED[s["name"]]
        dt = s.get("data_type", "uint16")
        count = s.get("count") or (2 if dt.endswith("32") else 1)
        entry = {
            "key": key,
            "address": s["address"],
            "register_type": "input" if s.get("input_type") == "input" else "holding",
            "data_type": dt,
            "count": count,
            "scale": s.get("scale", 1),
        }
        unit = s.get("unit_of_measurement")
        if unit in UNITS:
            entry["unit"] = UNITS[unit]
        entry.update(label=label, label_de=label_de, category=category, summary=summary, writable=False)
        if s["name"] in ENUMS:
            entry["enum"] = {str(k): v for k, v in ENUMS[s["name"]].items()}
        registers.append(entry)
        if s["name"] == "Power Flow Status":
            for bit, bkey, blabel, blabel_de, bsum in POWER_FLOW_BITS:
                registers.append({
                    "key": bkey, "address": s["address"], "register_type": "input",
                    "data_type": "bool", "count": 1, "scale": 1, "label": blabel,
                    "label_de": blabel_de, "category": "power_flow", "summary": bsum,
                    "writable": False, "bit": bit,
                })

    registers.sort(key=lambda r: (r["register_type"], r["address"], r.get("bit", -1)))
    doc = {
        "name": "sungrow_sh",
        "kind": "inverter",
        "description": "Sungrow SH hybrid inverter (SH*RS / SH*RT / SH*T, e.g. SH20T) via LAN or WiNet-S",
        "source": "Generated from https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant (MIT License)",
        "word_order": "little",
        "registers": registers,
    }
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(registers)} registers)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]))
