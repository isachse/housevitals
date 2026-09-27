"""Shared fixtures: in-process Modbus TCP simulators for a BLW NEO and a Sungrow SH20T."""

import asyncio
import socket

import pytest
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusServerContext,
    ModbusSparseDataBlock,
)
from pymodbus.server import ModbusTcpServer


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _start_neo(outdoor_raw=0x10000 - 52, port=None):
    ir = {a: 0 for a in list(range(10, 42)) + list(range(60, 76))}
    ir.update({10: outdoor_raw, 12: 345, 13: 0x10000 - 500, 25: 1, 30: 42, 41: 20,
               68: 0, 69: 1234})
    hr = {a: 0 for a in range(100, 127)}
    hr.update({100: 1, 101: 215, 105: 500, 106: 420})  # DHW setpoints 50 / 42 °C
    ctx = ModbusServerContext(devices=ModbusDeviceContext(
        ir=ModbusSparseDataBlock(ir),
        hr=ModbusSparseDataBlock(hr),
    ), single=True)
    port = port or _free_port()
    server = ModbusTcpServer(ctx, address=("127.0.0.1", port))
    task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.2)
    return port, server, task


@pytest.fixture
async def neo_device():
    """Simulated BLW NEO with a few known values. Only mapped addresses exist."""
    port, server, task = await _start_neo()
    yield port
    await server.shutdown()
    task.cancel()


@pytest.fixture
async def sungrow_device():
    """Simulated Sungrow SH20T (input registers only, low word first)."""
    ir = {a: 0 for a in range(4989, 5000)}
    ir.update({4989: 0x4132, 4990: 0x3432, 4999: 0x0E26})  # serial "A242", SH20T
    ir.update({a: 0 for a in list(range(5002, 5022)) + [5032, 5033, 5034, 5213, 5214]})
    ir.update({5016: 5230, 5213: 0xFFFF - 1999, 5214: 0xFFFF})  # 5.23 kW PV, battery -2000 W
    ir.update({a: 0 for a in range(12999, 13047)})
    ir.update({12999: 0x0040, 13000: 0b10011, 13007: 1500, 13022: 655, 13001: 123})
    ctx = ModbusServerContext(devices=ModbusDeviceContext(ir=ModbusSparseDataBlock(ir)), single=True)
    port = _free_port()
    server = ModbusTcpServer(ctx, address=("127.0.0.1", port))
    task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.2)
    yield port
    await server.shutdown()
    task.cancel()
