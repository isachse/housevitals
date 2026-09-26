"""Chart look: palette, figure layout and matplotlib helpers (no data access).

Colors follow a validated categorical palette on a light surface. Each appliance and
energy flow keeps one color everywhere (charts and Grafana). Legends sit above the
plot area, never on top of data; measures with different units get stacked panels
instead of a second y-axis.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .i18n import Translator

BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
VIOLET, GRAY, LIGHT_GRAY = "#4a3aa7", "#8a8984", "#e4e3df"
CRITICAL = "#d03b3b"  # status color, only for the "outdated" badge
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e8e7e3"
APPLIANCE_COLORS = (BLUE, ORANGE, AQUA, VIOLET)  # by configuration order within a kind
DEMAND_COLORS = {"none": LIGHT_GRAY, "heating": ORANGE, "dhw": BLUE, "cooling": AQUA,
                 "external demand": VIOLET}

# Date formats per language (only numeric/C-locale directives, independent of OS locale).
_DATE_FORMATS = {
    "en": {"day": "%b %d", "span": "%b %d %H:%M", "stamp": "%Y-%m-%d %H:%M"},
    "de": {"day": "%d.%m.", "span": "%d.%m. %H:%M", "stamp": "%d.%m.%Y %H:%M"},
}

# matplotlib keeps global state (fonts, rc params); draw one figure at a time.
_MPL_LOCK = threading.Lock()


def date_format(t: Translator, kind: str) -> str:
    return _DATE_FORMATS.get(t.lang, _DATE_FORMATS["en"])[kind]


def locked_draw(draw: Callable[[], bytes]) -> bytes:
    with _MPL_LOCK:
        return draw()


def figure(rows: int, heights: tuple[int, ...] | None = None):
    from matplotlib.figure import Figure

    fig = Figure(figsize=(10, 5.2), dpi=100, facecolor=SURFACE)
    axes = fig.subplots(rows, 1, sharex=True, squeeze=False,
                        gridspec_kw={"height_ratios": heights or (1,) * rows, "hspace": 0.2})
    axes = [a[0] for a in axes]
    for ax in axes:
        ax.set_facecolor(SURFACE)
        ax.grid(axis="y", color=GRID, lw=0.8, zorder=0)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(GRID)
        ax.tick_params(colors=INK_2, labelsize=9, length=0)
        ax.yaxis.label.set_color(INK_2)
    fig.subplots_adjust(left=0.07, right=0.98, top=0.80, bottom=0.09)
    return fig, axes


def header(fig, title: str, subtitle: str, t: Translator, end: datetime) -> None:
    fig.text(0.07, 0.965, title, fontsize=14, fontweight="bold", color=INK, va="top")
    stamp = end.strftime(date_format(t, "stamp"))
    fig.text(0.07, 0.905, f"{subtitle} · {t('as_of')} {stamp}", fontsize=9, color=INK_2, va="top")


def legend(ax, handles: list | None = None) -> None:
    """Legend in one row above the plot area."""
    handles = handles if handles is not None else ax.get_legend_handles_labels()[0]
    if handles:
        ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0, 1.0), ncol=len(handles),
                  frameon=False, fontsize=9, labelcolor=INK_2, handlelength=1.6,
                  borderaxespad=0.1, columnspacing=1.4)


def patches(items: list[tuple[str, str]]) -> list:
    """Plain legend swatches (bars may be hatched; the legend never is)."""
    from matplotlib.patches import Patch

    return [Patch(color=color, label=label) for label, color in items]


def hatch_partial(bars, partial: list[bool]) -> None:
    for bar, part in zip(bars, partial):
        if part:
            bar.set_hatch("///")
            bar.set_edgecolor(SURFACE)


def png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    return buf.getvalue()


def xy(series: dict | None, tz, scale: float = 1.0) -> tuple[list, list]:
    xs, ys = [], []
    for ts, v in (series or {}).get("points", []):
        xs.append(datetime.fromisoformat(ts).astimezone(tz))
        ys.append(None if v is None else v * scale)
    return xs, ys


def time_axis(ax, start: datetime, end: datetime, tz, t: Translator) -> None:
    import matplotlib.dates as mdates

    ax.set_xlim(start, end)
    fmt = "%H:%M" if (end - start) <= timedelta(days=2) else date_format(t, "day")
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=tz, maxticks=10))
    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt, tz=tz))


def day_axis(ax, days: list[datetime], tz, t: Translator) -> None:
    import matplotlib.dates as mdates

    ax.set_xlim(days[0] - timedelta(days=0.6), days[-1] + timedelta(days=0.6))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, len(days) // 12), tz=tz))
    ax.xaxis.set_major_formatter(mdates.DateFormatter(date_format(t, "day"), tz=tz))


def state_band(ax, points: list, end: datetime) -> list[str]:
    """Colored band of states over time; returns the states shown."""
    import matplotlib.dates as mdates

    spans: dict[str, list[tuple[float, float]]] = {}
    for (ts, state), nxt in zip(points, points[1:] + [[end.isoformat(), None]]):
        x0 = mdates.date2num(datetime.fromisoformat(ts))
        x1 = mdates.date2num(datetime.fromisoformat(nxt[0]))
        spans.setdefault(str(state), []).append((x0, x1 - x0))
    for state, ranges in spans.items():
        ax.broken_barh(ranges, (0, 1), color=DEMAND_COLORS.get(state, GRAY), zorder=2)
    ax.set_ylim(0, 1)
    ax.set_yticks([])
    ax.grid(False)
    return list(spans)


def span_text(start: datetime, end: datetime, t: Translator) -> str:
    fmt = date_format(t, "span")
    return f"{start.strftime(fmt)} – {end.strftime(fmt)}"


def month_label(month: str, t: Translator) -> str:
    year, m = month.split("-")
    return f"{t('months').split()[int(m) - 1]} {year[2:]}"


def bar_offsets(n: int, width: float = 0.8) -> tuple[list[float], float]:
    """Center offsets and bar width for n grouped bars per slot."""
    w = width / n
    return [(i - (n - 1) / 2) * w for i in range(n)], w


def stats(series: dict) -> dict[str, Any]:
    return {k: series[k] for k in ("min", "max", "avg", "last", "unit") if k in series}


def stale_badge(png_bytes: bytes, text: str) -> bytes:
    """The image with an "outdated" badge in the top-right corner (icon + text, so the
    status never depends on color alone)."""
    import matplotlib.image as mpimg
    from matplotlib.figure import Figure

    img = mpimg.imread(io.BytesIO(png_bytes), format="png")
    height, width = img.shape[:2]
    fig = Figure(figsize=(width / 100, height / 100), dpi=100)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.imshow(img)
    ax.axis("off")
    fig.text(0.98, 0.955, text, ha="right", va="top", fontsize=10, fontweight="bold", color="white",
             bbox={"boxstyle": "round,pad=0.45", "facecolor": CRITICAL, "edgecolor": "none"})
    return png(fig)
