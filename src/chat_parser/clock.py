"""Местное время пользователя. В базе всё хранится в UTC."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import settings


def tz() -> timezone:
    return timezone(timedelta(hours=settings.display_utc_offset))


def now() -> datetime:
    return datetime.now(tz())


def day_start(at: datetime | None = None) -> datetime:
    """Местная полночь того дня, к которому относится момент at."""
    local = (at or now()).astimezone(tz())
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def next_at(hour: int, at: datetime | None = None) -> datetime:
    """Ближайшее наступление hour:00 по местному времени строго после at."""
    local = (at or now()).astimezone(tz())
    target = local.replace(hour=hour % 24, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target
