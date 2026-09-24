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


def tz_label() -> str:
    offset = settings.display_utc_offset
    return "МСК" if offset == 3 else f"UTC{offset:+d}"


def at_time(hour: int, minute: int = 0, day: datetime | None = None) -> datetime:
    """hour:minute по местному времени в тот же день, что и day (по умолчанию — сегодня)."""
    local = (day or now()).astimezone(tz())
    return local.replace(hour=hour % 24, minute=minute % 60, second=0, microsecond=0)


def next_at(hour: int, minute: int = 0, at: datetime | None = None) -> datetime:
    """Ближайшее наступление hour:minute по местному времени строго после at."""
    local = (at or now()).astimezone(tz())
    target = at_time(hour, minute, local)
    if target <= local:
        target += timedelta(days=1)
    return target


def hhmm(hour: int, minute: int) -> str:
    """9, 0 -> «9:00»; 21, 30 -> «21:30»."""
    return f"{hour}:{minute:02d}"
