from __future__ import annotations

from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

from ..config import settings


def build_client() -> TelegramClient:
    """Клиент под user-аккаунтом (MTProto). Bot API историю не отдаёт."""
    if settings.tg_session_string:
        session = StringSession(settings.tg_session_string)
    else:
        # data/ в репозитории не лежит (он в .gitignore), а sqlite не создаёт
        # каталог сам — на свежем клоне это «unable to open database file».
        Path(settings.tg_session).expanduser().parent.mkdir(parents=True, exist_ok=True)
        session = settings.tg_session
    client = TelegramClient(
        session,
        settings.tg_api_id,
        settings.tg_api_hash,
        connection_retries=3,
        retry_delay=2,
        timeout=20,
    )
    # Ожидания короче порога Telethon проглатывает сам; всё длиннее ловим руками
    # и откладываем чат, чтобы воркер не висел.
    client.flood_sleep_threshold = 60
    return client
