"""Разовый вход в Telegram аккаунтом-сборщиком.

    python scripts/login.py

Спросит номер, код из Telegram и (если включён) пароль двухэтапной проверки,
и напечатает строку сессии — её нужно вписать в .env как TG_SESSION_STRING.
Ходит в Telegram так же, как бот: через TG_PROXY, если он задан.
"""

from __future__ import annotations

import asyncio
import getpass
import sys

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from chat_parser.config import settings
from chat_parser.net import network_hint, telethon_proxy

CONNECT_TIMEOUT = 30


def _ask(prompt: str) -> str:
    return input(prompt).strip()


async def main() -> int:
    if not settings.telegram_ready:
        print(
            "\nВ .env не заданы TG_API_ID и TG_API_HASH (или там заглушки из примера).\n"
            "Возьми их на my.telegram.org -> API development tools и впиши в .env.\n"
        )
        return 1

    try:
        proxy = telethon_proxy(settings.tg_proxy)
    except ValueError as e:
        print(f"\n{e}\n")
        return 1

    client = TelegramClient(
        StringSession(), settings.tg_api_id, settings.tg_api_hash,
        proxy=proxy, connection_retries=3, retry_delay=2, timeout=20,
    )
    via = f" через прокси {settings.tg_proxy}" if settings.tg_proxy else ""
    print(f"\nПодключаюсь к Telegram{via}…")
    try:
        await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT)
    except (TimeoutError, OSError, ConnectionError) as e:
        print("\n" + network_hint(e or "таймаут", settings.tg_proxy) + "\n")
        return 1

    try:
        await client.start(
            phone=lambda: _ask("Номер телефона аккаунта-сборщика (в формате +7…): "),
            code_callback=lambda: _ask("Код, который пришёл в приложение Telegram: "),
            password=lambda: getpass.getpass(
                "Пароль двухэтапной проверки (при вводе не отображается): "
            ),
        )
    except errors.ApiIdInvalidError:
        print("\nTelegram не принял TG_API_ID / TG_API_HASH — перепроверь их на my.telegram.org.\n")
        return 1
    except errors.PhoneNumberInvalidError:
        print("\nНеверный номер телефона. Формат: +79991234567.\n")
        return 1
    except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError):
        print("\nКод неверный или устарел. Запусти скрипт ещё раз — придёт новый код.\n")
        return 1
    except errors.PasswordHashInvalidError:
        print("\nНеверный пароль двухэтапной проверки.\n")
        return 1
    except errors.FloodWaitError as e:
        print(f"\nTelegram просит подождать {e.seconds} с перед новой попыткой входа.\n")
        return 1

    me = await client.get_me()
    session = client.session.save()
    await client.disconnect()

    handle = f" (@{me.username})" if me.username else ""
    print(f"\n✅ Вошли как: {me.first_name}{handle}\n")
    print("Скопируй строку ниже ЦЕЛИКОМ в .env — это ключ от аккаунта, никому его не показывай:\n")
    print(f"TG_SESSION_STRING={session}\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
