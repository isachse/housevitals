import json

import pytest

from conftest import _free_port, _start_neo
from housevitals.modbus import ModbusClient, decode, plan_batches
from housevitals.registry import Register, load_profile
from housevitals.config import ConfigError, DeviceConfig, ServerConfig, load_config_file
from housevitals.config import parse_config
from housevitals.server import build_server


def _single(**kw):
    base = dict(name="hp", host="127.0.0.1", port=502, profile="neo", timeout=2)
    base.update(kw)
    return ServerConfig(devices=[DeviceConfig(**base)])


def _reg(**kw):
    base = dict(key="x", address=0, register_type="holding", data_type="int16",
                count=1, scale=1, label="x", category="c")
    base.update(kw)
    return Register(**base)


def test_decode_types():
    assert decode(_reg(scale=0.1), [0xFF9C]) == (-10.0, -100)
    assert decode(_reg(data_type="uint16"), [0xFFFF]) == (None, 0xFFFF)
    assert decode(_reg(data_type="uint32"), [1, 2]) == (65538, 65538)
    assert decode(_reg(data_type="bool", bit=3), [0b1000]) == (True, 8)
    assert decode(_reg(enum={"1": "on"}), [1]) == ("on", 1)
    assert decode(_reg(data_type="uint16", enum={"1": "on"}), [0x8200]) == ("unknown code 33280 (0x8200)", 33280)
    assert decode(_reg(enum={"0": "locked", "*": "released"}), [10]) == ("released", 10)
    assert decode(_reg(invalid_raw=(-500,), scale=0.1), [0x10000 - 500]) == (None, -500)
    # Sungrow: 32-bit values with the low word first
    assert decode(_reg(data_type="uint32", word_order="little", scale=0.1), [37061, 5]) == (36474.1, 364741)
    assert decode(_reg(data_type="int32", word_order="little"), [0xFFFA, 0xFFFF]) == (-6, -6)
    assert decode(_reg(data_type="string", count=3), [0x4132, 0x3432, 0x0000]) == ("A242", None)


def test_batches_only_join_consecutive():
    regs = [_reg(key="a", address=10), _reg(key="b", address=11),
            _reg(key="c", address=13), _reg(key="d", address=11, register_type="input")]
    batches = plan_batches(regs)
    assert [(b.register_type, b.start, b.end) for b in batches] == [
        ("holding", 10, 11), ("holding", 13, 13), ("input", 11, 11)]


def test_profiles_load_and_filter_zones():
    assert len(load_profile("iwr", [1, 2]).registers) > len(load_profile("iwr", [1]).registers)
    for name in ("iwr", "isr", "neo", "sungrow_sh"):
        profile = load_profile(name, [1])
        assert profile.find(summary_only=True)
        # Every register must fit in one Modbus request
        assert all(1 <= r.count <= 125 for r in profile.registers.values() if r.register_type != "derived")
        assert all(r.count == 2 for r in profile.registers.values() if r.data_type.endswith("32"))


def test_build_server_outside_event_loop():
    # The stdio entry point builds the server before any event loop is running.
    mcp = build_server(_single(host="127.0.0.1", profile="iwr"))
    assert mcp.name == "housevitals"


async def _call(mcp, tool, args=None):
    result = await mcp.call_tool(tool, args or {})
    return json.loads(result.content[0].text)


async def test_overview_against_simulator(neo_device):
    mcp = build_server(_single(port=neo_device))
    data = await _call(mcp, "get_overview")
    values = data["values"]
    assert values["outdoor_temperature"]["value"] == -5.2
    assert values["outdoor_temperature"]["unit"] == "°C"
    assert values["flow_temperature"]["value"] == 34.5
    assert values["return_temperature"]["value"] is None  # -50.0 °C = sensor missing
    assert values["compressor"]["value"] == "on"
    assert values["cop"]["value"] == 4.2
    assert values["compressor_demand"]["value"] == "heating"
    assert values["electricity_total"]["value"] == 1234
    assert values["operating_mode"]["value"] == "auto"
    assert values["room_setpoint"]["value"] == 21.5


async def test_read_values_and_raw(neo_device):
    mcp = build_server(_single(port=neo_device))
    data = await _call(mcp, "read_values", {"keys": ["flow_temperature"]})
    assert data["values"]["flow_temperature"]["value"] == 34.5
    data = await _call(mcp, "read_values", {"keys": ["nope"]})
    assert "Unknown register" in data["error"]
    raw = await _call(mcp, "read_raw_registers",
                      {"address": 10, "count": 2, "register_type": "input"})
    assert raw["registers"]["10"]["int16"] == -52


async def test_batch_failure_falls_back_to_single_reads(neo_device):
    client = ModbusClient("127.0.0.1", neo_device, timeout=2)
    good = _reg(key="good", address=12, register_type="input", scale=0.1)
    # Address 42 is not mapped in the simulator -> the joined batch fails.
    bad = _reg(key="bad", address=42, register_type="input")
    regs = [good, _reg(key="mid", address=41, register_type="input"), bad]
    result = await client.read(regs)
    assert result["mid"]["value"] == 20
    assert "error" in result["bad"]
    await client.close()


async def test_unreachable_host_reports_error():
    mcp = build_server(_single(port=_free_port(), timeout=1))
    data = await _call(mcp, "read_values", {"keys": ["cop"]})
    assert "Cannot connect" in data["error"]


def test_device_resolution_by_name_and_alias():
    cfg = ServerConfig(devices=[
        DeviceConfig(name="house", host="a", aliases=["WP1", "Wärmepumpe 1"]),
        DeviceConfig(name="garage", host="b", aliases=["wp2"]),
    ])
    assert cfg.resolve("HOUSE").name == "house"
    assert cfg.resolve("wp1").name == "house"
    assert cfg.resolve(" wärmepumpe   1 ").name == "house"
    assert cfg.resolve("WP2").name == "garage"
    with pytest.raises(ConfigError, match="Several appliances"):
        cfg.resolve(None)
    with pytest.raises(ConfigError, match="Unknown appliance"):
        cfg.resolve("attic")
    cfg = ServerConfig(devices=cfg.devices, default_device="wp2")
    assert cfg.default_device == "garage" and cfg.resolve(None).name == "garage"


def test_duplicate_alias_rejected():
    with pytest.raises(ConfigError, match="used by both"):
        ServerConfig(devices=[DeviceConfig(name="a", host="x", aliases=["wp"]),
                              DeviceConfig(name="b", host="y", aliases=["WP"])])


def test_config_file_and_cli(tmp_path):
    path = tmp_path / "devices.json"
    path.write_text(json.dumps({"lang": "de", "devices": [
        {"name": "hp1", "host": "10.0.0.1", "profile": "neo", "aliases": "one"},
        {"name": "hp2", "host": "10.0.0.2", "profile": "iwr", "zones": "1,3"},
    ]}))
    cfg = load_config_file(path)
    assert cfg.lang == "de" and cfg.resolve("one").host == "10.0.0.1"
    assert cfg.resolve("hp2").zones == [1, 3]
    assert parse_config(["--config", str(path)]).names == ["hp1", "hp2"]
    single = parse_config(["--host", "10.0.0.9", "--profile", "neo"])
    assert single.names == ["heatpump"] and single.resolve(None).host == "10.0.0.9"
    path.write_text(json.dumps({"devices": [{"name": "x", "host": "h", "profile": "bad"}]}))
    with pytest.raises(ConfigError, match="unknown profile"):
        load_config_file(path)


async def test_multiple_devices(neo_device):
    port2, server2, task2 = await _start_neo(outdoor_raw=123)
    try:
        cfg = ServerConfig(devices=[
            DeviceConfig(name="hp1", host="127.0.0.1", port=neo_device, profile="neo",
                         aliases=["wp1"], timeout=2),
            DeviceConfig(name="hp2", host="127.0.0.1", port=port2, profile="neo",
                         aliases=["wp2"], timeout=2),
        ])
        mcp = build_server(cfg)
        overview = await _call(mcp, "get_overview")
        assert overview["appliances"]["hp1"]["values"]["outdoor_temperature"]["value"] == -5.2
        assert overview["appliances"]["hp2"]["values"]["outdoor_temperature"]["value"] == 12.3
        one = await _call(mcp, "read_values", {"appliance": "WP2", "keys": ["outdoor_temperature"]})
        assert one["appliance"] == "hp2" and one["values"]["outdoor_temperature"]["value"] == 12.3
        missing = await _call(mcp, "read_values", {"keys": ["cop"]})
        assert "Several appliances" in missing["error"]
        listed = await _call(mcp, "list_devices")
        assert [d["aliases"] for d in listed["devices"]] == [["wp1"], ["wp2"]]
    finally:
        await server2.shutdown()
        task2.cancel()



async def test_tool_parameters_have_plain_types():
    # Clients such as Claude Code drop anyOf/Optional schemas, leaving untyped params
    # that the model then omits. Every parameter must carry a concrete type.
    cfg = ServerConfig(devices=[DeviceConfig(name="a", host="x", aliases=["wp1"]),
                                DeviceConfig(name="b", host="y")])
    for tool in await build_server(cfg).list_tools():
        for name, prop in tool.input_schema.get("properties", {}).items():
            assert "type" in prop and "anyOf" not in prop, (tool.name, name, prop)
            assert prop.get("description"), (tool.name, name)
    read_values = next(t for t in await build_server(cfg).list_tools() if t.name == "read_values")
    assert "'wp1'" in read_values.input_schema["properties"]["appliance"]["description"]


async def test_no_tool_parameter_named_device():
    # Remote MCP bridges consume a "device" argument (target computer) and strip it.
    cfg = ServerConfig(devices=[DeviceConfig(name="a", host="x"), DeviceConfig(name="b", host="y")])
    for tool in await build_server(cfg).list_tools():
        assert "device" not in tool.input_schema.get("properties", {}), tool.name


async def test_empty_string_device_and_selectors(neo_device):
    mcp = build_server(_single(port=neo_device))
    data = await _call(mcp, "read_values", {"appliance": "", "keys": ["cop"], "category": "", "search": ""})
    assert data["values"]["cop"]["value"] == 4.2
    data = await _call(mcp, "read_values", {"appliance": "hp"})
    assert data["error"] == "Provide keys, category or search."


async def test_sungrow_inverter_alongside_heat_pump(neo_device, sungrow_device):
    cfg = ServerConfig(devices=[
        DeviceConfig(name="hp", host="127.0.0.1", port=neo_device, profile="neo", timeout=2),
        DeviceConfig(name="inverter", host="127.0.0.1", port=sungrow_device,
                     profile="sungrow_sh", aliases=["sh20t", "Wechselrichter"], timeout=2),
    ])
    mcp = build_server(cfg)
    data = await _call(mcp, "read_values", {
        "appliance": "Wechselrichter",
        "keys": ["device_type", "serial_number", "pv_power", "battery_power", "battery_soc",
                 "load_power", "running_state", "pv_generating", "battery_charging",
                 "exporting", "daily_pv_energy"],
    })
    v = {k: x["value"] for k, x in data["values"].items()}
    assert data["appliance"] == "inverter"
    assert v == {
        "device_type": "SH20T", "serial_number": "A242", "pv_power": 5230,
        "battery_power": -2000, "battery_soc": 65.5, "load_power": 1500,
        "running_state": "Running", "pv_generating": True, "battery_charging": True,
        "exporting": True, "daily_pv_energy": 12.3,
    }
    # Unsupported registers fail individually; the overview still returns the rest.
    overview = await _call(mcp, "get_overview", {"appliance": "sh20t"})
    assert overview["values"]["battery_soc"]["value"] == 65.5
    assert "error" in overview["values"]["grid_power"]
    listed = await _call(mcp, "list_devices")
    assert [d["type"] for d in listed["devices"]] == ["heat pump", "inverter"]
