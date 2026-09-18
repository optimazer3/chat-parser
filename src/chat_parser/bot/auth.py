"""Допуск к боту.

Без этого фильтра любой, кто найдёт бота, сможет запускать выгрузки и читать
чужие переписки. Список id задаётся в TG_ADMIN_IDS.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


class AdminOnly(BaseMiddleware):
    def __init__(self, allowed: set[int]) -> None:
        self.allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id not in self.allowed:
            # Молча игнорируем: не подсказываем чужим, что бот вообще живой.
            return None
        return await handler(event, data)
