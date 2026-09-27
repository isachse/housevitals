"""Modbus TCP access (one serialised connection per appliance).

Reads are the normal case; single-register writes exist only for the override
manager (see overrides.py), which enforces the allow-list and limits."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException

from .errors import HomeModbusError, UnavailableError
from .registry import Register

_LOGGER = logging.getLogger(__name__)

# Modbus allows at most 125 registers per read request.
MAX_READ_COUNT = 125
# Brötje gateways reject reads that touch unmapped addresses, so only strictly
# consecutive registers are combined into one request.
MAX_BATCH_WORDS = 100

# Raw values the devices use for "no data / sensor not connected".
_SENTINELS = {
    "int16": {-1},
    "uint16": {0xFFFF},
    "int32": {-1},
    "uint32": {0xFFFFFFFF},
}


class ModbusReadError(HomeModbusError):
    """The device answered with an error (e.g. illegal address)."""

    status = 502


class ModbusConnectError(ModbusReadError, UnavailableError):
    """The device cannot be reached or stopped answering."""

    status = 503


# Errors meaning the device (or the path to it) is not answering at all, as opposed
# to a Modbus exception response for a single register (e.g. illegal address).
_NO_RESPONSE = (ModbusIOException, ConnectionException, asyncio.TimeoutError, OSError)


def decode(reg: Register, words: list[int]) -> tuple[Any, int | None]:
    """Decode raw words into (value, raw). value is None for invalid readings."""
    dt = reg.data_type
    if dt == "bool":
        raw = words[0]
        return (bool(raw & (1 << reg.bit)) if reg.bit is not None else bool(raw)), raw

    if dt in ("int16", "uint16"):
        raw = words[0]
        if dt == "int16" and raw >= 0x8000:
            raw -= 0x10000
    elif dt == "string":
        text = b"".join(w.to_bytes(2, "big") for w in words).decode("ascii", "replace")
        text = text.replace("\x00", "").strip()
        return (text or None), None
    elif dt in ("int32", "uint32"):
        hi, lo = (words[1], words[0]) if reg.word_order == "little" else (words[0], words[1])
        raw = (hi << 16) | lo
        if dt == "int32" and raw >= 0x80000000:
            raw -= 0x100000000
    else:
        raw = words[0]

    if raw in _SENTINELS.get(dt, ()) or raw in reg.invalid_raw:
        return None, raw
    if reg.enum is not None:
        # "*" is the fallback label for any value not listed explicitly.
        label = reg.enum.get(str(raw), reg.enum.get("*"))
        return (label if label is not None else f"unknown code {raw} (0x{raw:04X})"), raw
    value = raw * reg.scale
    if isinstance(value, float):
        value = round(value, 4)
    return value, raw


@dataclass
class _Batch:
    register_type: str
    start: int
    end: int  # inclusive
    registers: list[Register]


def plan_batches(registers: list[Register]) -> list[_Batch]:
    """Group registers into read requests over strictly consecutive addresses."""
    batches: list[_Batch] = []
    for reg in sorted(registers, key=lambda r: (r.register_type, r.address)):
        reg_end = reg.address + reg.count - 1
        last = batches[-1] if batches else None
        if (
            last
            and last.register_type == reg.register_type
            and reg.address <= last.end + 1
            and reg_end - last.start + 1 <= MAX_BATCH_WORDS
        ):
            last.registers.append(reg)
            last.end = max(last.end, reg_end)
        else:
            batches.append(_Batch(reg.register_type, reg.address, reg_end, [reg]))
    return batches


class ModbusClient:
    """Thin async wrapper that keeps one TCP connection and serialises requests."""

    def __init__(
        self,
        host: str,
        port: int = 502,
        unit_id: int = 1,
        timeout: float = 5.0,
        min_request_interval: float = 0.0,
    ):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        # Minimum pause between two requests to the same device (for slow gateways).
        self.min_request_interval = min_request_interval
        self._last_request = 0.0
        self.request_count = 0
        # Created lazily: pymodbus clients must be built inside a running event loop.
        self._client: AsyncModbusTcpClient | None = None
        self._lock = asyncio.Lock()

    async def _ensure_connected(self) -> AsyncModbusTcpClient:
        if self._client is None:
            # reconnect_delay=0 disables background reconnects; we reconnect on demand.
            # retries=0: a missing answer is detected after one timeout; the poller
            # (with back-off) retries instead of every single request.
            self._client = AsyncModbusTcpClient(
                self.host, port=self.port, timeout=self.timeout, retries=0, reconnect_delay=0
            )
        if not self._client.connected and not await self._client.connect():
            raise ModbusConnectError(
                f"Cannot connect to Modbus TCP device at {self.host}:{self.port}"
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    async def _no_response(self, err: BaseException) -> ModbusConnectError:
        """Drop the connection (it may be half-open) and describe the outage."""
        await self.close()
        reason = str(err) or type(err).__name__
        return ModbusConnectError(f"No response from Modbus TCP device at {self.host}:{self.port} ({reason})")

    async def _read_words(self, register_type: str, address: int, count: int) -> list[int]:
        client = await self._ensure_connected()
        if self.min_request_interval:
            wait = self._last_request + self.min_request_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        self.request_count += 1
        self._last_request = time.monotonic()
        if register_type == "input":
            rr = await client.read_input_registers(
                address, count=count, device_id=self.unit_id
            )
        else:
            rr = await client.read_holding_registers(
                address, count=count, device_id=self.unit_id
            )
        if rr.isError():
            raise ModbusReadError(
                f"Device returned error for {register_type} registers "
                f"{address}..{address + count - 1}: {rr}"
            )
        return list(rr.registers)

    async def read_raw(self, register_type: str, address: int, count: int) -> list[int]:
        if not 1 <= count <= MAX_READ_COUNT:
            raise ValueError(f"count must be between 1 and {MAX_READ_COUNT}")
        async with self._lock:
            try:
                return await self._read_words(register_type, address, count)
            except ModbusConnectError:
                raise
            except _NO_RESPONSE as err:
                raise await self._no_response(err) from err
            except ModbusException as err:
                raise ModbusReadError(str(err) or type(err).__name__) from err

    async def write_register(self, address: int, raw: int) -> list[int]:
        """Write one holding register and read it back (same lock, no request in between).

        Returns the words read back, so the caller can verify the device accepted the value.
        """
        word = raw & 0xFFFF  # int16 values are sent as two's complement
        async with self._lock:
            try:
                client = await self._ensure_connected()
                if self.min_request_interval:
                    wait = self._last_request + self.min_request_interval - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                self.request_count += 1
                self._last_request = time.monotonic()
                rr = await client.write_register(address, word, device_id=self.unit_id)
                if rr.isError():
                    raise ModbusReadError(f"Device rejected write to holding register {address}: {rr}")
                return await self._read_words("holding", address, 1)
            except ModbusConnectError:
                raise
            except _NO_RESPONSE as err:
                raise await self._no_response(err) from err
            except ModbusException as err:
                raise ModbusReadError(str(err) or type(err).__name__) from err

    async def read(self, registers: list[Register]) -> dict[str, dict[str, Any]]:
        """Read and decode registers.

        A register the device rejects (exception response) is reported per register.
        If the device does not answer at all, the whole read stops at once with
        ModbusConnectError instead of waiting for a timeout per batch and register.
        """
        results: dict[str, dict[str, Any]] = {}
        async with self._lock:
            try:
                for batch in plan_batches(registers):
                    try:
                        words = await self._read_words(
                            batch.register_type, batch.start, batch.end - batch.start + 1
                        )
                    except (ModbusConnectError, *_NO_RESPONSE):
                        raise
                    except (ModbusReadError, ModbusException) as err:
                        # Usually one unmapped address inside the batch: read one by one.
                        _LOGGER.debug("Batch read failed (%s), retrying individually", err)
                        for reg in batch.registers:
                            try:
                                words = await self._read_words(reg.register_type, reg.address, reg.count)
                            except (ModbusConnectError, *_NO_RESPONSE):
                                raise
                            except (ModbusReadError, ModbusException) as single:
                                results[reg.key] = {"value": None,
                                                    "error": str(single) or type(single).__name__}
                                continue
                            results[reg.key] = self._result(reg, words)
                        continue
                    for reg in batch.registers:
                        off = reg.address - batch.start
                        results[reg.key] = self._result(reg, words[off : off + reg.count])
            except ModbusConnectError:
                await self.close()
                raise
            except _NO_RESPONSE as err:
                raise await self._no_response(err) from err
        return results

    @staticmethod
    def _result(reg: Register, words: list[int]) -> dict[str, Any]:
        value, raw = decode(reg, words)
        out: dict[str, Any] = {"value": value}
        if reg.unit and value is not None and not isinstance(value, (str, bool)):
            out["unit"] = reg.unit
        if reg.enum is not None or value is None:
            out["raw"] = raw
        return out
