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


# Upper bound for one override lease; a lease can never outlive this.
MAX_OVERRIDE_DURATION_S = 24 * 3600


@dataclass
class OverrideRule:
    """Allow-list entry: a holding register that may be overridden, and its limits."""

    min: float | None = None  # bounds for numeric registers (scaled value)
    max: float | None = None
    values: dict[str, int] | None = None  # enum registers: allowed labels -> raw codes
    max_duration_s: float = 6 * 3600
    max_writes_per_day: int = 6  # applying overrides; restores are never refused

    @classmethod
    def from_dict(cls, where: str, data: dict[str, Any]) -> OverrideRule:
        unknown = set(data) - {"min", "max", "values", "max_duration_s", "max_writes_per_day"}
        if unknown:
            raise ConfigError(f"{where}: unknown option(s): {', '.join(sorted(unknown))}")
        rule = cls(
            min=float(data["min"]) if data.get("min") is not None else None,
            max=float(data["max"]) if data.get("max") is not None else None,
            values={str(k): int(v) for k, v in data["values"].items()} if data.get("values") else None,
            max_duration_s=float(data.get("max_duration_s", 6 * 3600)),
            max_writes_per_day=int(data.get("max_writes_per_day", 6)),
        )
        if rule.values is None and (rule.min is None or rule.max is None):
            raise ConfigError(f"{where}: needs 'min' and 'max' (numeric) or 'values' (enum)")
        if rule.min is not None and rule.max is not None and rule.min > rule.max:
            raise ConfigError(f"{where}: 'min' is greater than 'max'")
        if not 0 < rule.max_duration_s <= MAX_OVERRIDE_DURATION_S:
            raise ConfigError(f"{where}: max_duration_s must be between 1 and {MAX_OVERRIDE_DURATION_S}")
        if rule.max_writes_per_day < 1:
            raise ConfigError(f"{where}: max_writes_per_day must be at least 1")
        return rule


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
    # Registers that may be overridden (written) through the control API, by key.
    overrides: dict[str, OverrideRule] = field(default_factory=dict)
    # Energy statistics from the profile's derived counters (integrated power) instead
    # of the device's energy counters (for devices whose counters are not updated over
    # Modbus; see the profile's "derived" points with "replaces").
    energy_from_power: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceConfig:
        unknown = set(data) - {
            "name", "host", "port", "unit_id", "profile", "zones", "timeout",
            "aliases", "description", "poll_interval", "extra_keys", "min_request_interval",
            "overrides", "energy_from_power",
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
            overrides={
                str(key): OverrideRule.from_dict(f"Device '{data['name']}', override '{key}'", rule)
                for key, rule in (data.get("overrides") or {}).items()
            },
            energy_from_power=bool(data.get("energy_from_power", False)),
        )


DEFAULT_DERIVED_STATE_FILE = "~/.local/state/housevitals/derived.json"
DEFAULT_AVAILABILITY_STATE_FILE = "~/.local/state/housevitals/availability.json"


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
    # OpenTelemetry service.instance.id, i.e. Prometheus' `instance` label. Fixed on
    # purpose: a changing value (e.g. the host name, which macOS may derive from the
    # router) would start new series for every metric.
    instance_id: str = "housevitals"
    # Control API (overrides). Writing needs a bearer token, read from this file or from
    # HOUSEVITALS_CONTROL_TOKEN; without one the control API only lists overrides.
    control_token_file: str | None = None
    # Active overrides survive restarts here (outside the repository by default).
    override_state_file: str = "~/.local/state/housevitals/overrides.json"
    # Seconds to wait before checking a written value a second time. Controllers may
    # accept a value and adjust it a few seconds later (the Brötje NEO limits the DHW
    # minimum to the maximum - 5 K after about 5 s); the first read-back misses that.
    override_verify_delay_s: float = 10.0
    # End all overrides (restore the previous values) when the service stops, and on
    # the next start after an unclean end, so no override outlives the service.
    restore_overrides_on_stop: bool = True
    # Counters of derived data points (e.g. energy integrated from power) survive
    # restarts here. None: not kept (the service sets DEFAULT_DERIVED_STATE_FILE).
    derived_state_file: str | None = None
    # "Unavailable since" and "last success" of every appliance survive restarts here,
    # so an outage keeps its start time. None: not kept (the service sets
    # DEFAULT_AVAILABILITY_STATE_FILE).
    availability_state_file: str | None = None

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
class PVArray:
    """One PV array (e.g. the modules on one MPPT input) for the forecast."""

    name: str
    kwp: float
    tilt: float  # degrees from horizontal
    azimuth: float  # degrees, 0 = south, negative = east, positive = west
    # Measured power for calibration: a power data point, or voltage × current.
    power: str | None = None
    voltage: str | None = None
    current: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PVArray:
        where = f"forecast array '{data.get('name', '?')}'"
        unknown = set(data) - {"name", "kwp", "tilt", "azimuth", "power", "voltage", "current"}
        if unknown:
            raise ConfigError(f"{where}: unknown option(s) {', '.join(sorted(unknown))}")
        try:
            array = cls(name=str(data["name"]), kwp=float(data["kwp"]), tilt=float(data["tilt"]),
                        azimuth=float(data.get("azimuth", 0)), power=data.get("power"),
                        voltage=data.get("voltage"), current=data.get("current"))
        except KeyError as err:
            raise ConfigError(f"{where}: '{err.args[0]}' is required") from err
        if not (0 < array.kwp and 0 <= array.tilt <= 90 and -180 <= array.azimuth <= 180):
            raise ConfigError(f"{where}: kwp > 0, tilt 0..90, azimuth -180..180 required")
        if not array.power and not (array.voltage and array.current):
            raise ConfigError(f"{where}: set 'power', or 'voltage' and 'current'")
        return array


@dataclass
class ForecastConfig:
    """PV forecast and surplus windows from Open-Meteo weather forecasts."""

    latitude: float
    longitude: float
    appliance: str  # the inverter whose data points are used
    arrays: list[PVArray]
    refresh_s: float = 900.0
    calibration_days: int = 14
    open_meteo_url: str = "https://api.open-meteo.com/v1/forecast"
    # Surplus simulation (data points of the appliance, battery limits)
    load: str = "load_power"
    battery_soc: str | None = "battery_soc"
    battery_capacity: str | None = "battery_capacity"  # kWh data point
    battery_min_soc: float = 5.0
    battery_max_charge_w: float = 10000.0
    battery_max_discharge_w: float = 10000.0
    surplus_threshold_w: float = 1000.0
    max_ac_w: float | None = None  # inverter AC limit, caps the forecast

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ForecastConfig:
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"Unknown forecast option(s): {', '.join(sorted(unknown))}")
        for required in ("latitude", "longitude", "appliance", "arrays"):
            if required not in data:
                raise ConfigError(f"forecast: '{required}' is required")
        cfg = cls(**{**data, "arrays": [PVArray.from_dict(a) for a in data["arrays"]]})
        if not (-90 <= cfg.latitude <= 90 and -180 <= cfg.longitude <= 180):
            raise ConfigError("forecast: latitude/longitude out of range")
        if not cfg.arrays:
            raise ConfigError("forecast: at least one array is required")
        if not 1 <= cfg.calibration_days <= 92:
            raise ConfigError("forecast: calibration_days must be 1..92 (Open-Meteo past_days)")
        return cfg


@dataclass
class InsightsConfig:
    """Tenant insights page (/insights): prices and areas for the heating and hot water
    figures of a utility bill. Every value is a default the tenant can change on the page."""

    living_area_m2: float | None = None  # heated living area of the whole house
    consumption_share: float = 0.7  # share of heating costs split by consumption (HeizkostenV: 0.5-0.7)
    grid_price_eur_per_kwh: float = 0.30  # electricity from the grid for the heat pumps
    pv_price_eur_per_kwh: float = 0.0  # own PV electricity used by the heat pumps
    water_price_eur_per_m3: float = 4.5  # fresh water plus sewage
    room_temperature_c: float = 20.0  # degree days G20/15 (VDI 3807)
    heating_limit_c: float = 15.0
    dhw_temperature_c: float = 50.0  # hot water at the tap, for litres from heat
    cold_water_temperature_c: float = 10.0
    dhw_loss_share: float = 0.3  # storage and circulation losses in the delivered hot water heat

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InsightsConfig:
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"Unknown insights option(s): {', '.join(sorted(unknown))}")
        cfg = cls(**data)
        if cfg.living_area_m2 is not None and cfg.living_area_m2 <= 0:
            raise ConfigError("insights.living_area_m2 must be positive")
        if not 0 < cfg.consumption_share <= 1:
            raise ConfigError("insights.consumption_share must be above 0 and at most 1")
        if not 0 <= cfg.dhw_loss_share < 1:
            raise ConfigError("insights.dhw_loss_share must be at least 0 and below 1")
        for name in ("grid_price_eur_per_kwh", "pv_price_eur_per_kwh", "water_price_eur_per_m3"):
            if getattr(cfg, name) < 0:
                raise ConfigError(f"insights.{name} must not be negative")
        if cfg.heating_limit_c > cfg.room_temperature_c:
            raise ConfigError("insights.heating_limit_c must not be above room_temperature_c")
        if cfg.dhw_temperature_c <= cfg.cold_water_temperature_c:
            raise ConfigError("insights.dhw_temperature_c must be above cold_water_temperature_c")
        return cfg


@dataclass
class ServerConfig:
    devices: list[DeviceConfig]
    default_device: str | None = None
    # Default language of human-facing output (REST API, charts); MCP text is English.
    lang: str = i18n.DEFAULT
    service: ServiceConfig = field(default_factory=ServiceConfig)
    forecast: ForecastConfig | None = None
    insights: InsightsConfig = field(default_factory=InsightsConfig)
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
        if self.forecast is not None:
            self.forecast.appliance = self.resolve(self.forecast.appliance).name
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
        forecast=ForecastConfig.from_dict(raw["forecast"]) if raw.get("forecast") else None,
        insights=InsightsConfig.from_dict(raw.get("insights", {})),
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
