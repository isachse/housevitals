"""Solar geometry and plane-of-array irradiance (no dependencies).

Sun position after the NOAA approximation (good to a fraction of a degree), irradiance
on a tilted plane with the isotropic sky model, and a simple cell temperature
derating. Accurate enough for PV forecasts whose main uncertainty is the weather.

Conventions: azimuth 0 = south, negative = east, positive = west (as Open-Meteo);
tilt 0 = horizontal.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

ALBEDO = 0.2  # ground reflectance
TEMP_COEFFICIENT = -0.004  # per K, typical crystalline silicon
CELL_HEATING = 0.025  # K per W/m² above air temperature (≈ +25 K at 1000 W/m²)


def sun_position(ts: float, latitude: float, longitude: float) -> tuple[float, float]:
    """Solar zenith and azimuth in degrees at unix time ts (azimuth 0 = south, + = west)."""
    dt = datetime.fromtimestamp(ts, timezone.utc)
    hour = dt.hour + dt.minute / 60 + dt.second / 3600
    g = 2 * math.pi / 365 * (dt.timetuple().tm_yday - 1 + (hour - 12) / 24)
    declination = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
                   - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
                   - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    equation_of_time = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                                 - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    solar_minutes = hour * 60 + equation_of_time + 4 * longitude
    hour_angle = math.radians(solar_minutes / 4 - 180)
    lat = math.radians(latitude)
    cos_zenith = (math.sin(lat) * math.sin(declination)
                  + math.cos(lat) * math.cos(declination) * math.cos(hour_angle))
    zenith = math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))
    azimuth = math.degrees(math.atan2(
        math.sin(hour_angle),
        math.cos(hour_angle) * math.sin(lat) - math.tan(declination) * math.cos(lat)))
    return zenith, azimuth


def plane_irradiance(ghi: float, dni: float, dhi: float, zenith: float, sun_azimuth: float,
                     tilt: float, azimuth: float) -> float:
    """Irradiance on a tilted plane in W/m² (isotropic sky): direct + diffuse + ground."""
    if zenith >= 90:
        return 0.0
    t, z = math.radians(tilt), math.radians(zenith)
    cos_incidence = (math.cos(z) * math.cos(t)
                     + math.sin(z) * math.sin(t) * math.cos(math.radians(sun_azimuth - azimuth)))
    direct = dni * max(0.0, cos_incidence)
    diffuse = dhi * (1 + math.cos(t)) / 2
    ground = ghi * ALBEDO * (1 - math.cos(t)) / 2
    return max(0.0, direct + diffuse + ground)


def dc_power(kwp: float, irradiance: float, air_temperature: float | None) -> float:
    """Expected DC power in W of an array at a performance ratio of 1."""
    power = kwp * irradiance  # kWp * W/m² / (1000 W/m²) * 1000 W/kW
    if air_temperature is not None:
        cell = air_temperature + CELL_HEATING * irradiance
        power *= 1 + TEMP_COEFFICIENT * (cell - 25)
    return max(0.0, power)
