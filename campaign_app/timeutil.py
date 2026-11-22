"""UTC 时间处理。

业务契约要求：时间均带时区并以 UTC 记录；区间左闭右开。
本模块只接受带时区的 ISO-8601 时间，避免把本地时间误当作业务事实。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def parse_ts(value: str, *, field: str = "时间") -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} 不能为空")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:  # pragma: no cover - 错误信息保留
        raise ValueError(f"{field} 不是合法 ISO-8601 时间: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} 必须带时区: {value!r}")
    return parsed.astimezone(timezone.utc)


def format_ts(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("输出时间必须带时区")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def add_days(value: datetime, days: float) -> datetime:
    return value + timedelta(days=days)


def add_hours(value: datetime, hours: float) -> datetime:
    return value + timedelta(hours=hours)


def in_half_open(value: datetime, start: datetime, end: datetime) -> bool:
    return start <= value < end
