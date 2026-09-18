from __future__ import annotations

from telethon import TelegramClient
from telethon.sessions import StringSession

from ..config import settings


def build_client() -> TelegramClient:
    """Клиент под user-аккаунтом (MTProto). Bot API историю не отдаёт."""
    session = (
        StringSession(settings.tg_session_string)
        if settings.tg_session_string
        else settings.tg_session
    )
    client = TelegramClient(session, settings.tg_api_id, settings.tg_api_hash)
    # Ожидания короче порога Telethon проглатывает сам; всё длиннее ловим руками
    # и откладываем чат, чтобы воркер не висел.
    client.flood_sleep_threshold = 60
    return client
