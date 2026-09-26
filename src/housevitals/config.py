"""Appliance and service configuration: heat pumps and inverters, addressable by name or alias."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import i18n
from .errors import HomeModbusError, NotFoundError
from .registry import PROFILE_NAMES


class ConfigError(HomeModbusError):
    """Invalid configuration, or an ambiguous appliance selection."""


class UnknownApplianceError(ConfigError, NotFoundError):
    """No appliance has this name or alias."""


def _norm(name: str) -> str:
    return " ".join(name.split()).casefold()


def parse_zones(value: Any) -> list[int]:
    if value in (None, ""):
        return [1]
    if isinstance(value, str) and value.strip().lower() == "all":
        return list(range(1, 13))
    if isinstance(value, str):
        value = [z for z in value.replace(" ", "").split(",") if z]
    return sorted({int(z) for z in value})


@dataclass
class DeviceConfig:
    name: str
    host: str
    port: int = 502
    unit_id: int = 1
    profile: str = "iwr"
    zones: list[int] = field(default_factory=lambda: [1])
    timeout: float = 5.0
    aliases: list[str] = field(default_factory=list)
    description: str | None = None
    # Background polling (service mode)
    poll_interval: float | None = None  # overrides service.poll_fast for this appliance
    extra_keys: list[str] = field(default_factory=list)  # additionally polled/exported
    min_request_interval: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceConfig:
        unknown = set(data) - {
            "name", "host", "port", "unit_id", "profile", "zones", "timeout",
            "aliases", "description", "poll_interval", "extra_keys", "min_request_interval",
        }
        if unknown:
            raise ConfigError(f"Unknown device option(s): {', '.join(sorted(unknown))}")
        for required in ("name", "host"):
            if not data.get(required):
                raise ConfigError(f"Every device needs a '{required}'")
        profile = data.get("profile", "iwr")
        if profile not in PROFILE_NAMES:
            raise ConfigError(
                f"Device '{data['name']}': unknown profile '{profile}', "
                f"expected one of {PROFILE_NAMES}"
            )
        aliases = data.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        return cls(
            name=str(data["name"]),
            host=str(data["host"]),
            port=int(data.get("port", 502)),
            unit_id=int(data.get("unit_id", 1)),
            profile=profile,
            zones=parse_zones(data.get("zones")),
            timeout=float(data.get("timeout", 5.0)),
            aliases=[str(a) for a in aliases],
            description=data.get("description"),
            poll_interval=float(data["poll_interval"]) if data.get("poll_interval") else None,
            extra_keys=[str(k) for k in data.get("extra_keys", [])],
            min_request_interval=float(data.get("min_request_interval", 0.0)),
        )


@dataclass
class ServiceConfig:
    """Settings for the long-running service (poller, metrics, REST API, MCP over HTTP)."""

    http_host: str = "127.0.0.1"
    http_port: int = 8080
    allowed_hosts: list[str] = field(default_factory=list)
    otlp_endpoint: str | None = None  # e.g. http://127.0.0.1:9090/api/v1/otlp/v1/metrics
    export_interval: float = 15.0
    poll_fast: float = 15.0
    poll_slow: float = 60.0
    poll_static: float = 3600.0
    on_demand_ttl: float = 10.0
    prometheus_url: str | None = None  # enables the history tools, e.g. http://127.0.0.1:9090
    timezone: str = "Europe/Berlin"  # calendar days/months for energy statistics

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServiceConfig:
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"Unknown service option(s): {', '.join(sorted(unknown))}")
        cfg = cls(**data)
        try:
            ZoneInfo(cfg.timezone)
        except Exception as err:
            raise ConfigError(f"service.timezone: unknown time zone '{cfg.timezone}'") from err
        for name in ("export_interval", "poll_fast", "poll_slow", "poll_static", "on_demand_ttl"):
            if float(getattr(cfg, name)) <= 0:
                raise ConfigError(f"service.{name} must be positive")
        return cfg


@dataclass
class ServerConfig:
    devices: list[DeviceConfig]
    default_device: str | None = None
    # Default language of human-facing output (REST API, charts); MCP text is English.
    lang: str = i18n.DEFAULT
    service: ServiceConfig = field(default_factory=ServiceConfig)
    _lookup: dict[str, DeviceConfig] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.devices:
            raise ConfigError("At least one device must be configured")
        for dev in self.devices:
            for ident in (dev.name, *dev.aliases):
                key = _norm(ident)
                other = self._lookup.get(key)
                if other is not None and other is not dev:
                    raise ConfigError(
                        f"Name/alias '{ident}' is used by both '{other.name}' and '{dev.name}'"
                    )
                self._lookup[key] = dev
        if self.default_device is not None:
            self.default_device = self.resolve(self.default_device).name
        if self.lang not in i18n.SUPPORTED:
            raise ConfigError(f"lang must be one of {', '.join(i18n.SUPPORTED)}")

    @property
    def names(self) -> list[str]:
        return [d.name for d in self.devices]

    def resolve(self, name: str | None) -> DeviceConfig:
        """Find a device by name or alias (case-insensitive).

        Without a name, the default device (or the only device) is returned.
        """
        if name is None or not name.strip():
            if self.default_device:
                return self._lookup[_norm(self.default_device)]
            if len(self.devices) == 1:
                return self.devices[0]
            raise ConfigError(
                "Several appliances are configured; pass 'appliance' with one of: "
                + self._describe_names()
            )
        dev = self._lookup.get(_norm(name))
        if dev is None:
            raise UnknownApplianceError(
                f"Unknown appliance '{name}'. Known appliances: {self._describe_names()}")
        return dev

    def _describe_names(self) -> str:
        parts = []
        for d in self.devices:
            parts.append(f"{d.name} (aliases: {', '.join(d.aliases)})" if d.aliases else d.name)
        return "; ".join(parts)


def load_config_file(path: str | Path) -> ServerConfig:
    path = Path(path).expanduser()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise ConfigError(f"Config file not found: {path}") from err
    except json.JSONDecodeError as err:
        raise ConfigError(f"Invalid JSON in {path}: {err}") from err
    if not isinstance(raw, dict) or not isinstance(raw.get("devices"), list):
        raise ConfigError(f"{path}: expected an object with a 'devices' list")
    return ServerConfig(
        devices=[DeviceConfig.from_dict(d) for d in raw["devices"]],
        default_device=raw.get("default_device"),
        lang=raw.get("lang", i18n.DEFAULT),
        service=ServiceConfig.from_dict(raw.get("service", {})),
    )


def parse_config(argv: list[str] | None = None) -> ServerConfig:
    """Build the configuration from --config/HOUSEVITALS_CONFIG, or a single --host."""
    env = os.environ
    p = argparse.ArgumentParser(description="Brötje heat pumps and Sungrow inverters via Modbus TCP")
    p.add_argument(
        "--config",
        default=env.get("HOUSEVITALS_CONFIG"),
        help="JSON file listing one or more appliances (see devices.example.json)",
    )
    p.add_argument("--host", default=env.get("HOUSEVITALS_HOST"), help="single device: IP/hostname")
    p.add_argument("--name", default=env.get("HOUSEVITALS_NAME", "heatpump"), help="single device: name")
    p.add_argument("--port", type=int, default=int(env.get("HOUSEVITALS_PORT", 502)))
    p.add_argument("--unit-id", type=int, default=int(env.get("HOUSEVITALS_UNIT_ID", 1)))
    p.add_argument(
        "--profile",
        choices=PROFILE_NAMES,
        default=env.get("HOUSEVITALS_PROFILE", "iwr"),
        help="iwr = IWR/GTW-08 gateway, isr = ISR Plus/MODBM, neo = BLW NEO (NEO-RKM), "
        "sungrow_sh = Sungrow SH hybrid inverter",
    )
    p.add_argument(
        "--zones",
        default=env.get("HOUSEVITALS_ZONES", "1"),
        help="IWR only: comma-separated zone numbers (e.g. '1,2') or 'all'",
    )
    p.add_argument("--timeout", type=float, default=float(env.get("HOUSEVITALS_TIMEOUT", 5)))
    p.add_argument("--lang", choices=i18n.SUPPORTED, default=env.get("HOUSEVITALS_LANG"))
    args = p.parse_args(argv)
    try:
        if args.config:
            config = load_config_file(args.config)
            if args.lang:
                config.lang = args.lang
            return config
        if not args.host:
            p.error("configure devices with --config/HOUSEVITALS_CONFIG or a single --host/HOUSEVITALS_HOST")
        device = DeviceConfig(
            name=args.name,
            host=args.host,
            port=args.port,
            unit_id=args.unit_id,
            profile=args.profile,
            zones=parse_zones(args.zones),
            timeout=args.timeout,
        )
        return ServerConfig(devices=[device], lang=args.lang or i18n.DEFAULT)
    except ConfigError as err:
        p.error(str(err))
