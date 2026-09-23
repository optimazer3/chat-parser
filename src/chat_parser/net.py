"""Прокси для Telegram.

У части провайдеров api.telegram.org и серверы MTProto заблокированы или
замедлены: приложение Telegram обходит это само, а программы, которые ходят
в API напрямую, — нет. Один TG_PROXY используется и ботом (aiogram), и
аккаунтом-сборщиком (Telethon). Шлюз LLM и Supabase через прокси не ходят.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlparse

SCHEMES = {"socks5": "socks5", "socks5h": "socks5", "socks4": "socks4", "http": "http"}
EXAMPLE = "socks5://127.0.0.1:10808"


@dataclass(frozen=True)
class Proxy:
    scheme: str  # socks5 | socks4 | http
    host: str
    port: int
    username: str | None = None
    password: str | None = None


def parse_proxy(url: str) -> Proxy | None:
    """'' -> None; неправильный адрес -> ValueError с понятным текстом."""
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        raise ValueError(
            f"TG_PROXY: не указан тип прокси — допиши его в начало: socks5://{url} "
            "(или http://, если в VPN-клиенте это HTTP-прокси)"
        )
    u = urlparse(url)
    scheme = SCHEMES.get(u.scheme.lower())
    if scheme is None:
        raise ValueError(
            f"TG_PROXY: схема «{u.scheme or '?'}» не поддерживается — "
            f"нужна socks5, socks4 или http, например {EXAMPLE}"
        )
    try:
        port = u.port
    except ValueError:
        port = None
    if not u.hostname or not port:
        raise ValueError(f"TG_PROXY: нужен адрес и порт, например {EXAMPLE}")
    return Proxy(
        scheme=scheme,
        host=u.hostname,
        port=port,
        username=unquote(u.username) if u.username else None,
        password=unquote(u.password) if u.password else None,
    )


def aiogram_proxy(url: str) -> str | None:
    """URL в том виде, который понимает aiohttp-socks."""
    p = parse_proxy(url)
    if p is None:
        return None
    auth = ""
    if p.username:
        from urllib.parse import quote

        auth = quote(p.username, safe="")
        if p.password:
            auth += ":" + quote(p.password, safe="")
        auth += "@"
    return f"{p.scheme}://{auth}{p.host}:{p.port}"


def telethon_proxy(url: str) -> dict | None:
    """Словарь прокси для TelegramClient (через python-socks)."""
    p = parse_proxy(url)
    if p is None:
        return None
    proxy: dict = {"proxy_type": p.scheme, "addr": p.host, "port": p.port, "rdns": True}
    if p.username:
        proxy["username"] = p.username
        proxy["password"] = p.password or ""
    return proxy


def network_hint(error: object, proxy_url: str) -> str:
    """Текст для случая «до Telegram не достучаться»."""
    current = proxy_url or "не задан"
    return (
        f"Нет связи с серверами Telegram ({error}).\n\n"
        "Похоже, провайдер блокирует или замедляет Telegram. Приложение Telegram\n"
        "обходит это само, а программы — нет. Варианты:\n"
        "  1) включи VPN на весь компьютер и запусти снова;\n"
        "  2) если у VPN-программы есть локальный прокси, пропиши его в .env:\n"
        f"       TG_PROXY={EXAMPLE}\n"
        "     порт смотри в настройках VPN-клиента (часто 10808, 1080, 7890 или 2080).\n\n"
        f"Сейчас TG_PROXY: {current}\n"
        "Проверить связь: chat-parser doctor"
    )
