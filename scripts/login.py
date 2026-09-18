"""Разовый интерактивный вход в Telegram.

    python scripts/login.py

Печатает StringSession — положи её в .env как TG_SESSION_STRING, чтобы
не таскать .session-файл на сервер и не логиниться там заново.
"""

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

from chat_parser.config import settings


async def main() -> None:
    async with TelegramClient(
        StringSession(), settings.tg_api_id, settings.tg_api_hash
    ) as client:
        me = await client.get_me()
        print(f"\nВошли как: {me.first_name} (@{me.username})")
        print(f"\nTG_SESSION_STRING={client.session.save()}\n")


if __name__ == "__main__":
    asyncio.run(main())
