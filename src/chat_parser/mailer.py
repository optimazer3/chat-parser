"""Отправка итогов дня на почту: короткий текст и PDF во вложении.

Обычный SMTP из стандартной библиотеки. Для Gmail — smtp.gmail.com:587,
логин — адрес почты, пароль — пароль приложения (Google-аккаунт → Безопасность
→ Двухэтапная аутентификация → Пароли приложений). Если сервер напрямую
недоступен, письмо можно пустить через SOCKS-прокси (SMTP_PROXY).
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .config import settings

TIMEOUT = 30


class MailNotConfigured(RuntimeError):
    pass


def _proxy_socket(host: str, port: int, timeout: float):
    from python_socks.sync import Proxy

    return Proxy.from_url(settings.smtp_proxy).connect(
        dest_host=host, dest_port=port, timeout=timeout
    )


class _ProxySMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        return _proxy_socket(host, port, timeout)


class _ProxySMTPSSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        return self.context.wrap_socket(_proxy_socket(host, port, timeout),
                                        server_hostname=self._host)


def build_message(subject: str, text: str, attachment: bytes | None = None,
                  filename: str = "report.pdf") -> EmailMessage:
    sender = settings.smtp_from or settings.smtp_user
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Мониторинг чатов оптики", sender))
    msg["To"] = ", ".join(settings.email_recipients)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1] or None)
    msg.set_content(text)
    if attachment:
        msg.add_attachment(attachment, maintype="application", subtype="pdf",
                           filename=filename)
    return msg


def _connect() -> smtplib.SMTP:
    context = ssl.create_default_context()
    implicit_tls = settings.smtp_port == 465
    if implicit_tls:
        cls = _ProxySMTPSSL if settings.smtp_proxy else smtplib.SMTP_SSL
        server = cls(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT, context=context)
    else:
        cls = _ProxySMTP if settings.smtp_proxy else smtplib.SMTP
        server = cls(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT)
        server.starttls(context=context)
    server.login(settings.smtp_user, settings.smtp_password)
    return server


def _send_sync(msg: EmailMessage) -> None:
    server = _connect()
    try:
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass


async def send(subject: str, text: str, attachment: bytes | None = None,
               filename: str = "report.pdf") -> None:
    """Отправить письмо всем из REPORT_EMAIL_TO. Ошибка — исключение с понятным текстом."""
    if not settings.email_ready:
        raise MailNotConfigured(
            "почта не настроена: нужны REPORT_EMAIL_TO, SMTP_USER и SMTP_PASSWORD в .env"
        )
    msg = build_message(subject, text, attachment, filename)
    try:
        await asyncio.to_thread(_send_sync, msg)
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(
            "почтовый сервер не принял логин или пароль. Для Gmail нужен пароль "
            "приложения (16 букв), а не обычный пароль от почты"
        ) from e
    except (OSError, smtplib.SMTPException) as e:
        raise RuntimeError(f"письмо не ушло: {type(e).__name__}: {e}") from e


def check() -> str:
    """Для doctor: подключиться и войти, ничего не отправляя."""
    if not settings.email_ready:
        raise MailNotConfigured("не настроена (REPORT_EMAIL_TO, SMTP_USER, SMTP_PASSWORD)")
    server = _connect()
    server.quit()
    return f"{settings.smtp_host}:{settings.smtp_port} → {', '.join(settings.email_recipients)}"
