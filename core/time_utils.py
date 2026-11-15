"""Shared local-time formatting helpers (v2.10 memory time anchoring).

The plugin always works with **naive local time** (`datetime.now()`, epoch
seconds from `time.time()`), so every helper here formats with
`datetime.fromtimestamp()` and the fixed Chinese weekday table instead of the
locale-dependent ``%A``.

Every helper is defensive: invalid / missing / non-positive timestamps render
as an empty string (or a safe placeholder) and never raise, because these
values end up inside LLM prompts and Pages payloads where a single malformed
row must not break the whole request.
"""

from datetime import datetime
from typing import Any, Optional

_WEEKDAY_LABELS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def to_timestamp(value: Any) -> Optional[int]:
    """Coerce ``value`` into a positive epoch-second integer (``None`` if invalid)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        stamp = int(float(value))
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    return stamp


def _to_datetime(value: Any) -> Optional[datetime]:
    stamp = to_timestamp(value)
    if stamp is None:
        return None
    try:
        return datetime.fromtimestamp(stamp)
    except (OverflowError, OSError, ValueError):
        return None


def format_local_datetime(value: Any) -> str:
    """``2026-11-15 21:04`` (empty string when the timestamp is invalid)."""
    dt = _to_datetime(value)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def format_local_date(value: Any) -> str:
    """``2026-11-15`` (empty string when the timestamp is invalid)."""
    dt = _to_datetime(value)
    return dt.strftime("%Y-%m-%d") if dt else ""


def weekday_label(value: Any) -> str:
    """``星期日`` for a timestamp (empty string when invalid)."""
    dt = _to_datetime(value)
    return _WEEKDAY_LABELS[dt.weekday()] if dt else ""


def format_transcript_stamp(value: Any) -> str:
    """Compact per-message timestamp used inside memory-extraction transcripts."""
    return format_local_datetime(value)


def _describe_age(dt: datetime, now_dt: datetime) -> str:
    """Human readable age of a formation time (今天 / 昨天 / N 天前 / 未来)."""
    try:
        delta_days = (now_dt.date() - dt.date()).days
    except (AttributeError, TypeError, ValueError):  # pragma: no cover - defensive
        return "时间未知"
    if delta_days < 0:
        return "未来"
    if delta_days == 0:
        return "今天"
    if delta_days == 1:
        return "昨天"
    return f"{delta_days} 天前"


def describe_time_anchor(value: Any, now_ts: Any = None) -> str:
    """Render a memory time anchor: ``2026-11-15（星期日，3 天前）``.

    ``now_ts`` is injectable for tests; when omitted (or invalid) the current
    local time is used. Returns ``""`` for an invalid formation timestamp so
    callers can fall back to a plain memory line.
    """
    dt = _to_datetime(value)
    if dt is None:
        return ""
    now_dt = _to_datetime(now_ts) or datetime.now()
    return (
        f"{dt.strftime('%Y-%m-%d')}"
        f"（{_WEEKDAY_LABELS[dt.weekday()]}，{_describe_age(dt, now_dt)}）"
    )


def current_datetime_text(now_ts: Any = None) -> str:
    """Current local time with weekday: ``2026-11-15（星期日）21:30``."""
    dt = _to_datetime(now_ts) or datetime.now()
    return (
        f"{dt.strftime('%Y-%m-%d')}"
        f"（{_WEEKDAY_LABELS[dt.weekday()]}）{dt.strftime('%H:%M')}"
    )


def describe_time_span(start_ts: Any, end_ts: Any) -> str:
    """Render the covered range of a multi-day window.

    ``2026-11-10 09:12 ~ 2026-11-15 21:04（跨度 6 天）``; a same-day range keeps
    ``（同日）``. Invalid inputs degrade to ``时间未知`` instead of raising.
    """
    start = _to_datetime(start_ts)
    end = _to_datetime(end_ts)
    if start is None or end is None:
        return "时间未知"
    if end < start:
        start, end = end, start
    span_days = (end.date() - start.date()).days
    span_text = "同日" if span_days <= 0 else f"跨度 {span_days} 天"
    return (
        f"{start.strftime('%Y-%m-%d %H:%M')} ~ "
        f"{end.strftime('%Y-%m-%d %H:%M')}（{span_text}）"
    )
