"""Запуск бота при недоступном Telegram: понятное сообщение, а не трейсбек,
и закрытая HTTP-сессия (раньше было «Unclosed client session»)."""

import pytest
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
