"""Скрипт входа: понятные ошибки до того, как что-то пойдёт в сеть."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "login.py"


def _load():
    spec = importlib.util.spec_from_file_location("login_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_without_keys_explains_where_to_get_them(monkeypatch, capsys):
    from chat_parser.config import settings

    monkeypatch.setattr(settings, "tg_api_id", None)
    monkeypatch.setattr(settings, "tg_api_hash", "")
    assert await _load().main() == 1
    assert "my.telegram.org" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_bad_proxy_is_reported(monkeypatch, capsys):
    from chat_parser.config import settings

    monkeypatch.setattr(settings, "tg_api_id", 20481234)
    monkeypatch.setattr(settings, "tg_api_hash", "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    monkeypatch.setattr(settings, "tg_proxy", "127.0.0.1:10808")
    assert await _load().main() == 1
    assert "socks5://127.0.0.1:10808" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_unreachable_telegram_gives_network_hint(monkeypatch, capsys):
    from telethon import TelegramClient

    from chat_parser.config import settings

    monkeypatch.setattr(settings, "tg_api_id", 20481234)
    monkeypatch.setattr(settings, "tg_api_hash", "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    monkeypatch.setattr(settings, "tg_proxy", "")

    async def connect(self):
        raise ConnectionError("Превышен таймаут семафора")

    monkeypatch.setattr(TelegramClient, "connect", connect)
    assert await _load().main() == 1
    out = capsys.readouterr().out
    assert "VPN" in out and "TG_PROXY" in out
