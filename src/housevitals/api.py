"""REST API (OpenAPI 3) on top of the shared services.

Everything is read-only except the control API (/overrides), which needs a bearer
token and only accepts allow-listed registers (see overrides.py).

Labels and chart texts are localized: `?lang=de` or the Accept-Language header,
defaulting to the configured language. Errors are HomeModbusError subclasses whose
`status` becomes the HTTP status code (see install_error_handler).
"""

# No "from __future__ import annotations": FastAPI must see the locally defined Lang type.
import hmac
from email.utils import formatdate
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import i18n, queries
from .context import Services
from .errors import HomeModbusError


class ControlDisabledError(HomeModbusError):
    status = 403


class UnauthorizedError(HomeModbusError):
    status = 401


class Value(BaseModel):
    label: str
    value: float | int | bool | str | None = Field(description="Scaled value; enum values as text")
    unit: str | None = None
    raw: int | None = Field(None, description="Raw register value for enums/invalid readings")
    age_s: float | None = Field(None, description="Age of the reading in seconds")
    stale: bool | None = Field(None, description="Last known value; the latest reading failed")
    error: str | None = None


class ApplianceValues(BaseModel):
    appliance: str
    profile: str | None = None
    values: dict[str, Value] = {}
    warning: str | None = None
    error: str | None = None
    available: bool | None = Field(None, description="false while the appliance does not answer")
    unavailable_since: str | None = None
    last_success: str | None = Field(None, description="Last successful Modbus request")
    last_error: str | None = None
    retry_in_s: int | None = None
    retry_after_s: int | None = None
    hint: str | None = None


class ApplianceInfo(BaseModel):
    name: str
    aliases: list[str]
    type: str
    description: str | None
    host: str
    port: int
    unit_id: int
    profile: str
    profile_description: str
    zones: list[int] | None
    register_count: int
    status: dict[str, Any] | None = None


class RegisterInfo(BaseModel):
    key: str
    label: str
    category: str
    address: int
    register_type: str
    data_type: str
    unit: str | None = None
    scale: float | None = None
    bit: int | None = None
    values: dict[str, str] | None = None
    poll_group: str | None = Field(None, description="fast/slow/static, or null = read on demand")


def install_error_handler(app: FastAPI) -> None:
    @app.exception_handler(HomeModbusError)
    async def handle(request: Request, err: HomeModbusError) -> JSONResponse:
        body = err.to_dict()
        headers = {}
        if err.retry_after is not None:
            headers["Retry-After"] = str(max(1, round(err.retry_after)))
        return JSONResponse(status_code=err.status, content={"detail": body.pop("error"), **body},
                            headers=headers)


class OverrideRequest(BaseModel):
    value: float | int | str = Field(description="Scaled value (e.g. 50 for 50 °C) or enum label")
    owner: str = Field(description="Who holds the override, e.g. housereflexes/dhw_pv_boost",
                       pattern=r"^[A-Za-z0-9_.:/-]{1,64}$")
    until: str | None = Field(None, description="End: ISO date/time (local unless an offset "
                              "is given) or HH:MM today. Exactly one of until/duration_s.")
    duration_s: float | None = Field(None, gt=0, description="Duration in seconds")
    reason: str | None = Field(None, max_length=200, description="Free text for the log")
    restore_value: float | int | str | None = Field(
        None, description="Written when the override ends instead of the previous value "
        "(within the allow-list bounds), e.g. to correct a setpoint")


def build_router(services: Services, control_token: str | None = None) -> APIRouter:
    hub = services.hub
    router = APIRouter()

    def language(
        lang: Annotated[str | None, Query(description=f"One of {', '.join(i18n.SUPPORTED)}; "
                                          "default: Accept-Language, then the configured language")] = None,
        accept_language: Annotated[str | None, Header()] = None,
    ) -> str:
        default = services.config.lang
        return i18n.normalize(lang, default) if lang else i18n.from_accept_language(accept_language, default)

    Lang = Annotated[str, Depends(language)]

    @router.get("/appliances", response_model=list[ApplianceInfo], tags=["appliances"])
    def list_appliances() -> list[dict[str, Any]]:
        """Configured appliances with connection settings and poll status."""
        return [{**a.describe(), "status": a.poll_status()} for a in hub.appliances.values()]

    @router.get("/overview", response_model=dict[str, ApplianceValues], tags=["values"])
    async def overview_all(lang: Lang) -> dict[str, Any]:
        """Overview values of every appliance (from the cache)."""
        return await queries.overview_all(list(hub.appliances.values()), lang)

    @router.get("/appliances/{name}/overview", response_model=ApplianceValues, tags=["values"])
    async def overview(name: str, lang: Lang) -> dict[str, Any]:
        """Overview values of one appliance (name or alias)."""
        return await queries.overview(hub.get(name), lang)

    @router.get("/appliances/{name}/values", response_model=ApplianceValues, tags=["values"])
    async def values(
        name: str,
        lang: Lang,
        keys: list[str] = Query(default=[], description="Register keys (repeatable)"),
        category: str | None = Query(default=None),
        search: str | None = Query(default=None, description="Substring of key/label/category"),
    ) -> dict[str, Any]:
        """Values selected by keys, category or search term. Polled values come from
        the cache; others are read on demand through the serialised Modbus client."""
        app = hub.get(name)
        return await queries.read_values(app, queries.select_registers(app, keys, category, search), lang)

    @router.get("/appliances/{name}/registers", response_model=list[RegisterInfo], tags=["metadata"])
    def registers(name: str, lang: Lang, category: str | None = None,
                  search: str | None = None) -> list[dict[str, Any]]:
        """Data points known for the appliance's profile (no device access)."""
        return queries.describe_registers(hub.get(name), category, search, lang)

    @router.get("/appliances/{name}/categories", response_model=dict[str, int], tags=["metadata"])
    def categories(name: str) -> dict[str, int]:
        """Register categories with register counts."""
        return hub.get(name).profile.categories

    if services.history is not None:
        _history_routes(router, services, Lang)
    if services.charts is not None:
        _chart_routes(router, services, Lang)
    if services.overrides is not None:
        _override_routes(router, services, control_token)
    return router


def _override_routes(router: APIRouter, services: Services, token: str | None) -> None:
    overrides = services.overrides

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if token is None:
            raise ControlDisabledError(
                "The control API is disabled: no control token is configured "
                "(HOUSEVITALS_CONTROL_TOKEN or service.control_token_file)")
        scheme, _, given = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip().encode(), token.encode()):
            raise UnauthorizedError("Missing or wrong bearer token")

    @router.get("/overrides", tags=["control"])
    def list_overrides() -> dict[str, Any]:
        """Active overrides of all appliances and what may be overridden."""
        return overrides.status()

    @router.get("/appliances/{name}/overrides", tags=["control"])
    def appliance_overrides(name: str) -> dict[str, Any]:
        """Active overrides of one appliance and what may be overridden."""
        return overrides.status(name)

    @router.put("/appliances/{name}/overrides/{key}", tags=["control"],
                dependencies=[Depends(authorize)])
    async def put_override(name: str, key: str, request: OverrideRequest = Body()) -> dict[str, Any]:
        """Hold an allow-listed register at a value until a given time. The previous value
        is restored when the override ends. Repeating the call (same owner) changes the
        value or end without a new baseline; nothing is written if the device already
        has the value. A written value is checked again after a few seconds; if the device
        changed it (a limit of its own), the previous value is written back and the request
        fails with 422. Errors: 404 not allow-listed, 409 held by another owner,
        422 adjusted by the device, 429 writes for today used up, 503 appliance unreachable."""
        return await overrides.apply(name, key, request.value, request.owner, until=request.until,
                                     duration_s=request.duration_s, reason=request.reason,
                                     restore_value=request.restore_value)

    @router.delete("/appliances/{name}/overrides/{key}", tags=["control"],
                   dependencies=[Depends(authorize)])
    async def delete_override(name: str, key: str,
                              owner: str = Query(description="Owner that set the override")) -> dict[str, Any]:
        """End an override now and restore the previous value (unless it was changed
        on the device in the meantime). If the appliance is unreachable, the restore is
        retried in the background (state "restoring")."""
        return await overrides.release(name, key, owner)


def _history_routes(router: APIRouter, services: Services, Lang) -> None:
    hub, history = services.hub, services.history
    time_help = "ISO date/time (local), relative (24h, 7d) or today/yesterday"

    @router.get("/appliances/{name}/history", tags=["history"])
    async def get_history(
        name: str,
        lang: Lang,
        keys: list[str] = Query(description="Register keys (repeatable)"),
        start: str = Query("", description=time_help + "; default 24h"),
        end: str = Query("", description=time_help + "; default now"),
        max_points: int = Query(100, ge=1, le=500),
    ) -> dict[str, Any]:
        """Recorded time series with min/max/avg/last per key (from Prometheus)."""
        return await history.history(hub.get(name), keys, start, end, max_points, lang)

    @router.get("/energy", tags=["history"])
    async def get_energy(
        period: str = Query("day", pattern="^(day|week|month|year)$"),
        start: str = Query("", description=time_help),
        end: str = Query("", description=time_help + "; default now"),
        appliance: str | None = Query(None, description="Name or alias; default all"),
    ) -> dict[str, Any]:
        """Energy per local calendar period with self-sufficiency and performance factor."""
        apps = [hub.get(appliance)] if appliance else list(hub.appliances.values())
        return await history.energy(apps, period, start, end)

    @router.get("/appliances/{name}/runtime", tags=["history"])
    async def get_runtime(
        name: str,
        lang: Lang,
        key: str = Query("compressor", description="State register (enum or on/off)"),
        start: str = Query("", description=time_help + "; default today"),
        end: str = Query("", description=time_help + "; default now"),
    ) -> dict[str, Any]:
        """Hours and share per state, starts and run lengths."""
        return await history.runtime(hub.get(name), key, start, end, lang)


def _chart_routes(router: APIRouter, services: Services, Lang) -> None:
    charts = services.charts

    @router.get("/charts", tags=["charts"])
    def list_charts() -> dict[str, Any]:
        """Chart catalog and the images currently cached."""
        return {"charts": charts.catalog(), "cached": charts.status()}

    @router.get("/charts/{chart}.png", tags=["charts"], response_class=Response,
                responses={200: {"content": {"image/png": {}}}})
    async def chart_png(
        chart: str,
        lang: Lang,
        appliance: str = Query("", description="Name or alias; default depends on the chart"),
        range: str = Query("", description="e.g. 24h, 7d; default depends on the chart"),  # noqa: A002
    ) -> Response:
        """Pre-rendered chart as PNG (served from the cache when fresh)."""
        image = await charts.get(chart, appliance, range, lang)
        headers = {
            "Cache-Control": "no-cache" if image.stale else "max-age=60",
            "Content-Language": image.summary["lang"],
            "Last-Modified": formatdate(image.generated_at, usegmt=True),
            "X-Chart-Generated-At": image.summary["generated_at"],
            "X-Chart-Age": f"{image.age_s:.0f}",
        }
        if image.stale:
            headers["X-Chart-Stale"] = (image.stale_reason or "1").encode("ascii", "replace").decode()
        return Response(image.png, media_type="image/png", headers=headers)
