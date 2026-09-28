"""Tests for the override path: allow-list, leases, restore, write budget, control API."""

import asyncio
import json
import time

import httpx
import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from housevitals.config import ConfigError, DeviceConfig, OverrideRule, ServerConfig, ServiceConfig
from housevitals.context import Services
from housevitals.errors import NotFoundError
from housevitals.hub import Hub
from housevitals.overrides import (
    OverrideConflictError,
    OverrideError,
    OverrideManager,
    WriteBudgetError,
)
from housevitals.service import build_app, load_control_token
from conftest import _start_neo

TOKEN = "test-token-0123456789"
OWNER = "housereflexes/dhw_pv_boost"


def _config(port, state_file, **rule):
    rules = {
        "dhw_setpoint_min": OverrideRule(min=40, max=55, **rule),
        "return_setpoint_active": OverrideRule(values={"off": 0, "on": 1}),
    }
    device = DeviceConfig(name="hp", host="127.0.0.1", port=port, profile="neo",
                          aliases=["wp2"], timeout=2, overrides=rules)
    return ServerConfig(devices=[device],
                        service=ServiceConfig(override_state_file=str(state_file)))


def _manager(port, tmp_path, **rule):
    hub = Hub(_config(port, tmp_path / "overrides.json", **rule))
    return hub, OverrideManager(hub, tmp_path / "overrides.json")


async def _device_value(hub, key="dhw_setpoint_min"):
    app = hub.get("hp")
    return (await app.read_now(app.profile.registers[key]))["value"]


async def test_apply_and_release_restores_baseline(neo_device, tmp_path):
    hub, mgr = _manager(neo_device, tmp_path)
    app = hub.get("wp2")
    result = await mgr.apply("wp2", "dhw_setpoint_min", 50, OWNER, duration_s=3600, reason="PV surplus")
    assert result["written"] is True and result["value"] == 50 and result["baseline"] == 42
    assert result["state"] == "active" and 3590 < result["remaining_s"] <= 3600
    assert await _device_value(hub) == 50
    assert app.cache["dhw_setpoint_min"].data["value"] == 50  # cache follows the write

    # Same owner again: new end, no write because the device already has the value.
    writes = mgr.writes_total[("hp", "dhw_setpoint_min")]
    again = await mgr.apply("hp", "dhw_setpoint_min", 50, OWNER, duration_s=7200)
    assert again["written"] is False and again["baseline"] == 42
    assert mgr.writes_total[("hp", "dhw_setpoint_min")] == writes

    released = await mgr.release("hp", "dhw_setpoint_min", OWNER)
    assert released["outcome"] == "restored"
    assert await _device_value(hub) == 42
    assert mgr.leases == {}
    await hub.stop()


async def test_requests_are_validated(neo_device, tmp_path):
    hub, mgr = _manager(neo_device, tmp_path)
    with pytest.raises(NotFoundError):  # not on the allow-list
        await mgr.apply("hp", "dhw_setpoint_max", 55, OWNER, duration_s=60)
    with pytest.raises(OverrideError, match="between 40 and 55"):
        await mgr.apply("hp", "dhw_setpoint_min", 60, OWNER, duration_s=60)
    with pytest.raises(OverrideError, match="multiple of 0.1"):
        await mgr.apply("hp", "dhw_setpoint_min", 50.05, OWNER, duration_s=60)
    with pytest.raises(OverrideError, match="one of off, on"):
        await mgr.apply("hp", "return_setpoint_active", "maybe", OWNER, duration_s=60)
    with pytest.raises(OverrideError, match="at most 6 h"):
        await mgr.apply("hp", "dhw_setpoint_min", 50, OWNER, duration_s=7 * 3600)
    with pytest.raises(OverrideError, match="exactly one"):
        await mgr.apply("hp", "dhw_setpoint_min", 50, OWNER)
    with pytest.raises(OverrideError, match="in the future"):
        await mgr.apply("hp", "dhw_setpoint_min", 50, OWNER, until="2020-01-01T10:00")
    with pytest.raises(OverrideError, match="owner"):
        await mgr.apply("hp", "dhw_setpoint_min", 50, "bad owner!", duration_s=60)
    assert mgr.writes_total == {} and await _device_value(hub) == 42
    await hub.stop()


async def test_enum_override(neo_device, tmp_path):
    hub, mgr = _manager(neo_device, tmp_path)
    result = await mgr.apply("hp", "return_setpoint_active", "on", OWNER, duration_s=60)
    assert result["value"] == "on" and result["baseline"] == "off"
    assert (await mgr.release("hp", "return_setpoint_active", OWNER))["outcome"] == "restored"
    await hub.stop()


async def test_other_owner_conflicts(neo_device, tmp_path):
    hub, mgr = _manager(neo_device, tmp_path)
    await mgr.apply("hp", "dhw_setpoint_min", 50, OWNER, duration_s=60)
    with pytest.raises(OverrideConflictError):
        await mgr.apply("hp", "dhw_setpoint_min", 45, "someone-else", duration_s=60)
    with pytest.raises(OverrideConflictError):
        await mgr.release("hp", "dhw_setpoint_min", "someone-else")
    with pytest.raises(NotFoundError):
        await mgr.release("hp", "return_setpoint_active", OWNER)
    await hub.stop()


async def test_write_budget(neo_device, tmp_path):
    hub, mgr = _manager(neo_device, tmp_path, max_writes_per_day=2)
    await mgr.apply("hp", "dhw_setpoint_min", 50, OWNER, duration_s=60)
    await mgr.apply("hp", "dhw_setpoint_min", 52, OWNER, duration_s=60)
    with pytest.raises(WriteBudgetError) as err:
        await mgr.apply("hp", "dhw_setpoint_min", 54, OWNER, duration_s=60)
    assert err.value.status == 429 and err.value.retry_after > 0
    # Restoring is never refused by the budget.
    assert (await mgr.release("hp", "dhw_setpoint_min", OWNER))["outcome"] == "restored"
    assert await _device_value(hub) == 42
    assert mgr.status()["allowed"]["hp"]["dhw_setpoint_min"]["writes_today"] == 3
    await hub.stop()


async def test_expired_override_is_restored(neo_device, tmp_path):
    hub, mgr = _manager(neo_device, tmp_path)
    await mgr.apply("hp", "dhw_setpoint_min", 55, OWNER, duration_s=60)
    await mgr.check()
    assert await _device_value(hub) == 55  # still running
    mgr.leases[("hp", "dhw_setpoint_min")].until = time.time() - 1
    await mgr.check()
    assert mgr.leases == {} and await _device_value(hub) == 42
    await hub.stop()


async def test_manual_change_is_not_undone(neo_device, tmp_path):
    hub, mgr = _manager(neo_device, tmp_path)
    await mgr.apply("hp", "dhw_setpoint_min", 50, OWNER, duration_s=60)
    app = hub.get("hp")
    await app.write(app.profile.registers["dhw_setpoint_min"], 470)  # set on the device
    assert (await mgr.release("hp", "dhw_setpoint_min", OWNER))["outcome"] == "kept_manual_change"
    assert await _device_value(hub) == 47
    await hub.stop()


async def test_leases_survive_a_restart(neo_device, tmp_path):
    hub, mgr = _manager(neo_device, tmp_path)
    await mgr.apply("hp", "dhw_setpoint_min", 50, OWNER, duration_s=60)
    state = json.loads((tmp_path / "overrides.json").read_text())
    assert state["leases"][0]["baseline_raw"] == 420
    assert (tmp_path / "overrides.json").stat().st_mode & 0o777 == 0o600
    await hub.stop()

    # A new process loads the lease; once it has ended, the baseline is restored.
    hub2, mgr2 = _manager(neo_device, tmp_path)
    lease = mgr2.leases[("hp", "dhw_setpoint_min")]
    assert lease.owner == OWNER and lease.baseline_value == 42
    assert mgr2.status()["allowed"]["hp"]["dhw_setpoint_min"]["writes_today"] == 1
    lease.until = time.time() - 1
    await mgr2.check()
    assert mgr2.leases == {} and await _device_value(hub2) == 42
    await hub2.stop()


async def test_restore_waits_for_an_unreachable_device(tmp_path):
    port, server, task = await _start_neo()
    hub, mgr = _manager(port, tmp_path)
    await mgr.apply("hp", "dhw_setpoint_min", 55, OWNER, duration_s=60)
    await server.shutdown()
    task.cancel()

    result = await mgr.release("hp", "dhw_setpoint_min", OWNER)
    assert result["outcome"] == "restore_pending" and result["state"] == "restoring"
    with pytest.raises(OverrideConflictError, match="still being restored"):
        await mgr.apply("hp", "dhw_setpoint_min", 50, OWNER, duration_s=60)

    port, server, task = await _start_neo(port=port)
    try:
        app = hub.get("hp")
        app.retry_at = 0.0  # skip the back-off
        await app.client.write_register(106, 550)  # the device kept the override value
        await mgr.check()
        assert mgr.leases == {} and await _device_value(hub) == 42
    finally:
        await hub.stop()
        await server.shutdown()
        task.cancel()


def test_allow_list_is_checked_at_start(tmp_path):
    def build(key, rule):
        device = DeviceConfig(name="hp", host="127.0.0.1", profile="neo", overrides={key: rule})
        return OverrideManager(Hub(ServerConfig(devices=[device])), None)

    with pytest.raises(ConfigError, match="unknown register"):
        build("nope", OverrideRule(min=0, max=1))
    with pytest.raises(ConfigError, match="holding"):
        build("flow_temperature", OverrideRule(min=0, max=1))  # input register
    with pytest.raises(ConfigError, match="list allowed 'values'"):
        build("return_setpoint_active", OverrideRule(min=0, max=1))
    with pytest.raises(ConfigError, match="needs 'min' and 'max'"):
        DeviceConfig.from_dict({"name": "hp", "host": "x", "profile": "neo",
                                "overrides": {"dhw_setpoint_min": {"min": 40}}})
    with pytest.raises(ConfigError, match="max_duration_s"):
        DeviceConfig.from_dict({"name": "hp", "host": "x", "profile": "neo",
                                "overrides": {"dhw_setpoint_min": {"min": 40, "max": 55,
                                                                   "max_duration_s": 3 * 86400}}})


def test_control_token(tmp_path, monkeypatch):
    config = _config(1, tmp_path / "s.json")
    monkeypatch.delenv("HOUSEVITALS_CONTROL_TOKEN", raising=False)
    assert load_control_token(config) is None
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    config.service.control_token_file = str(token_file)
    assert load_control_token(config) == TOKEN
    monkeypatch.setenv("HOUSEVITALS_CONTROL_TOKEN", "short")
    with pytest.raises(ConfigError, match="at least 16"):
        load_control_token(config)


async def test_control_api(neo_device, tmp_path):
    config = _config(neo_device, tmp_path / "overrides.json")
    reader = InMemoryMetricReader()
    app = build_app(config, metric_readers=[reader], control_token=TOKEN)
    transport = httpx.ASGITransport(app=app)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    url = "/api/v1/appliances/wp2/overrides/dhw_setpoint_min"
    body = {"value": 50, "owner": OWNER, "duration_s": 600}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            assert (await client.put(url, json=body)).status_code == 401
            assert (await client.put(url, json=body,
                                     headers={"Authorization": "Bearer wrong"})).status_code == 401
            r = await client.put(url, json=body, headers=auth)
            assert r.status_code == 200 and r.json()["written"] is True

            listing = (await client.get("/api/v1/overrides")).json()
            assert listing["overrides"][0]["owner"] == OWNER
            assert listing["allowed"]["hp"]["dhw_setpoint_min"]["max"] == 55
            health = (await client.get("/healthz")).json()
            assert health["overrides"] == {"active": 1, "control": True}

            r = await client.put(url, json={**body, "owner": "other"}, headers=auth)
            assert r.status_code == 409 and r.json()["override"]["owner"] == OWNER
            r = await client.put("/api/v1/appliances/wp2/overrides/dhw_setpoint_max",
                                 json=body, headers=auth)
            assert r.status_code == 404
            r = await client.put(url, json={**body, "value": 70}, headers=auth)
            assert r.status_code == 400

            metrics = {m.name: m for rm in reader.get_metrics_data().resource_metrics
                       for sm in rm.scope_metrics for m in sm.metrics}
            active = metrics["housevitals.override.active"].data.data_points[0]
            assert active.value == 1 and active.attributes["owner"] == OWNER
            writes = {p.attributes["key"]: p.value
                      for p in metrics["housevitals.override.writes"].data.data_points}
            assert writes["dhw_setpoint_min"] == 1

            r = await client.delete(url, params={"owner": OWNER}, headers=auth)
            assert r.status_code == 200 and r.json()["outcome"] == "restored"
            assert (await client.get("/api/v1/overrides")).json()["overrides"] == []


async def test_control_api_without_token_is_read_only(neo_device, tmp_path):
    app = build_app(_config(neo_device, tmp_path / "o.json"), metric_readers=[InMemoryMetricReader()])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        assert (await client.get("/api/v1/overrides")).status_code == 200
        r = await client.put("/api/v1/appliances/hp/overrides/dhw_setpoint_min",
                             json={"value": 50, "owner": OWNER, "duration_s": 60},
                             headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 403


async def test_no_allow_list_no_control_api(neo_device):
    device = DeviceConfig(name="hp", host="127.0.0.1", port=neo_device, profile="neo")
    services = Services.create(ServerConfig(devices=[device]))
    assert services.overrides is None
    app = build_app(services.config, services, metric_readers=[InMemoryMetricReader()])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        assert (await client.get("/api/v1/overrides")).status_code == 404
    await services.close()


async def test_poll_overlapping_a_write_does_not_bring_back_the_old_value(neo_device, tmp_path):
    hub, mgr = _manager(neo_device, tmp_path)
    app = hub.get("hp")
    reg = app.profile.registers["dhw_setpoint_min"]
    app.groups["fast"].append(reg)  # polled, as with extra_keys
    original = app.client.read

    async def slow_read(regs):
        results = await original(regs)  # reads 42 ...
        await asyncio.sleep(0.3)  # ... and stores it only after the write below
        return results

    app.client.read = slow_read
    poll = asyncio.create_task(app.poll("fast"))
    await asyncio.sleep(0.1)
    await mgr.apply("hp", "dhw_setpoint_min", 50, OWNER, duration_s=60)
    await poll
    assert app.cache["dhw_setpoint_min"].data["value"] == 50
    assert app.cache["flow_temperature"].data["value"] == 34.5  # other values still stored
    await hub.stop()
