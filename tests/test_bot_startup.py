"""Запуск бота при недоступном Telegram: понятное сообщение, а не трейсбек,
и закрытая HTTP-сессия (раньше было «Unclosed client session»)."""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe

from chat_parser.bot import main
from chat_parser.config import settings


@pytest.fixture
def bot_settings(monkeypatch):
    monkeypatch.setattr(settings, "tg_bot_token", "42:TESTTOKEN")
    monkeypatch.setattr(settings, "tg_admin_ids", "777")
    monkeypatch.setattr(settings, "tg_proxy", "")


def test_build_bot_uses_proxy(bot_settings, monkeypatch):
    assert main.build_bot().session.proxy is None
    monkeypatch.setattr(settings, "tg_proxy", "socks5://127.0.0.1:10808")
    assert main.build_bot().session.proxy == "socks5://127.0.0.1:10808"


@pytest.mark.asyncio
async def test_blocked_telegram_gives_hint_and_closes_session(bot_settings, monkeypatch):
    closed = []

    async def get_me(self, **kw):
        raise TelegramNetworkError(
            method=GetMe(),
            message="HTTP Client says - ClientConnectorError: Cannot connect to host "
                    "api.telegram.org:443",
        )

    orig_close = main.AiohttpSession.close

    async def close(self):
        closed.append(True)
        await orig_close(self)

    monkeypatch.setattr(Bot, "get_me", get_me)
    monkeypatch.setattr(main.AiohttpSession, "close", close)
    with pytest.raises(SystemExit) as err:
        await main.run_bot()
    assert "TG_PROXY" in str(err.value) and "VPN" in str(err.value)
    assert closed, "сессия бота не закрыта"


@pytest.mark.asyncio
async def test_bad_token_is_explained(bot_settings, monkeypatch):
    async def get_me(self, **kw):
        raise TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")

    monkeypatch.setattr(Bot, "get_me", get_me)
    with pytest.raises(SystemExit, match="BotFather"):
        await main.run_bot()


@pytest.mark.asyncio
async def test_bad_proxy_setting_is_explained(bot_settings, monkeypatch):
    monkeypatch.setattr(settings, "tg_proxy", "vless://x:1")
    with pytest.raises(SystemExit, match="не поддерживается"):
        await main.run_bot()


def test_tracebacks_do_not_print_variables():
    """В трейсбеке не должно быть значений переменных — там пароли и токены."""
    from chat_parser.cli import app

    assert app.pretty_exceptions_show_locals is False


@pytest.mark.asyncio
async def test_paused_supabase_is_explained(bot_settings, monkeypatch):
    """Проект Supabase на паузе: вместо трассировки — что нажать."""
    import asyncpg
    from aiogram.types import User

    from chat_parser import db

    async def get_me(self, **kw):
        return User(id=1, is_bot=True, first_name="bot", username="test_bot")

    async def apply_schema():
        raise asyncpg.exceptions.InternalServerError(
            "(ENOTFOUND) tenant/user postgres.sqrprtazaljwrlnscfux not found")

    monkeypatch.setattr(Bot, "get_me", get_me)
    monkeypatch.setattr(db, "apply_schema", apply_schema)
    with pytest.raises(SystemExit) as err:
        await main.run_bot()
    text = str(err.value)
    assert "База данных недоступна" in text and "«sqrprtazaljwrlnscfux»" in text
    assert "Restore project" in text and "Session pooler" in text


def test_db_error_hints():
    from chat_parser.db import explain_error

    assert "Restore project" in explain_error("Tenant or user not found")
    assert "Reset database password" in explain_error(
        'password authentication failed for user "postgres"')
    assert "Session pooler" in explain_error("[Errno 11001] getaddrinfo failed")
    assert "вторая копия бота" in explain_error("Max client connections reached")
    assert "не на паузе" in explain_error(TimeoutError())
    assert explain_error("какая-то другая ошибка") is None


async def test_telegram_startup_timeout_is_explained_and_closes(bot_settings, monkeypatch, caplog):
    async def no_reply(self, **kwargs):
        await asyncio.Event().wait()

    closed = AsyncMock()
    monkeypatch.setattr(Bot, "get_me", no_reply)
    monkeypatch.setattr(main.AiohttpSession, "close", closed)
    monkeypatch.setattr(main, "TELEGRAM_STARTUP_TIMEOUT", 0.01)
    with caplog.at_level("INFO"), pytest.raises(SystemExit, match="Нет связи с серверами Telegram"):
        await main.run_bot()
    assert "Шаг 1/3" in caplog.text
    closed.assert_awaited_once()


async def test_schema_startup_timeout_is_explained(bot_settings, monkeypatch, caplog):
    from aiogram.types import User
    from chat_parser import db

    async def no_database_reply():
        await asyncio.Event().wait()

    monkeypatch.setattr(Bot, "get_me", AsyncMock(return_value=User(
        id=1, is_bot=True, first_name="bot", username="test_bot",
    )))
    monkeypatch.setattr(db, "apply_schema", no_database_reply)
    monkeypatch.setattr(main, "DATABASE_STARTUP_TIMEOUT", 0.01)
    with caplog.at_level("INFO"), pytest.raises(SystemExit, match="Сервер базы не отвечает"):
        await main.run_bot()
    assert "Шаг 2/3" in caplog.text


async def test_pool_cleanup_is_bounded(monkeypatch):
    from chat_parser import db

    class StuckPool:
        async def close(self):
            await asyncio.Event().wait()

        def terminate(self):
            self.terminated = True

    pool = StuckPool()
    wait_for = asyncio.wait_for

    async def short_timeout(awaitable, *, timeout):
        return await wait_for(awaitable, timeout=0.01)

    monkeypatch.setattr(db, "_pool", pool)
    monkeypatch.setattr(db.asyncio, "wait_for", short_timeout)
    await db.close_pool()
    assert pool.terminated and db._pool is None


@pytest.mark.parametrize("commands_timeout", [False, True])
async def test_startup_reaches_polling_or_explains_commands_timeout(
    bot_settings, monkeypatch, caplog, commands_timeout,
):
    from aiogram.types import User
    from chat_parser import db

    monkeypatch.setattr(Bot, "get_me", AsyncMock(return_value=User(
        id=1, is_bot=True, first_name="bot", username="test_bot",
    )))
    monkeypatch.setattr(db, "apply_schema", AsyncMock(return_value=[]))
    monkeypatch.setattr(main, "announce_keyboard", AsyncMock())
    monkeypatch.setattr(main, "weekly_loop", AsyncMock())
    monkeypatch.setattr(main, "connect_saved", AsyncMock())
    dispatcher = MagicMock()
    dispatcher.start_polling = AsyncMock()
    monkeypatch.setattr(main, "Dispatcher", lambda: dispatcher)

    async def commands(self, *args, **kwargs):
        if commands_timeout:
            await asyncio.Event().wait()

    monkeypatch.setattr(Bot, "set_my_commands", commands)
    monkeypatch.setattr(main, "TELEGRAM_STARTUP_TIMEOUT", 0.01)
    with caplog.at_level("INFO"):
        if commands_timeout:
            with pytest.raises(SystemExit, match="зарегистрировать команды"):
                await main.run_bot()
            dispatcher.start_polling.assert_not_awaited()
        else:
            await main.run_bot()
            dispatcher.start_polling.assert_awaited_once()
            assert "Готов. Отправь /start" in caplog.text
    assert "Шаг 3/3" in caplog.text
