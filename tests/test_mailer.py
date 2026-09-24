import smtplib

import pytest

from chat_parser import mailer
from chat_parser.config import settings


@pytest.fixture
def mail(monkeypatch):
    monkeypatch.setattr(settings, "report_email_to", "owner@example.com, boss@example.com")
    monkeypatch.setattr(settings, "smtp_host", "smtp.gmail.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_user", "bot@gmail.com")
    monkeypatch.setattr(settings, "smtp_password", "abcd efgh ijkl mnop")
    monkeypatch.setattr(settings, "smtp_from", "")
    monkeypatch.setattr(settings, "smtp_proxy", "")
    log = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            log.append(("connect", type(self).__name__, host, port))

        def starttls(self, context=None):
            log.append(("starttls",))

        def login(self, user, password):
            if password != "abcd efgh ijkl mnop":
                raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
            log.append(("login", user))

        def send_message(self, msg):
            log.append(("send", msg))

        def quit(self):
            log.append(("quit",))

    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", type("FakeSSL", (FakeSMTP,), {}))
    return log


async def test_report_email_with_pdf(mail):
    await mailer.send("Итоги дня · 24.09.2026", "текст письма", b"%PDF-1.4 test",
                      "itogi-dnya-2026-09-24.pdf")
    steps = [x[0] for x in mail]
    assert steps == ["connect", "starttls", "login", "send", "quit"]
    msg = mail[3][1]
    assert msg["To"] == "owner@example.com, boss@example.com"
    assert "bot@gmail.com" in msg["From"] and msg["Subject"] == "Итоги дня · 24.09.2026"
    (att,) = list(msg.iter_attachments())
    assert att.get_filename() == "itogi-dnya-2026-09-24.pdf"
    assert att.get_content_type() == "application/pdf"
    assert att.get_content() == b"%PDF-1.4 test"
    assert msg.get_body(("plain",)).get_content().strip() == "текст письма"


async def test_ssl_port_and_wrong_password(mail, monkeypatch):
    monkeypatch.setattr(settings, "smtp_port", 465)
    await mailer.send("s", "t")
    assert mail[0][1] == "FakeSSL" and ("starttls",) not in mail

    monkeypatch.setattr(settings, "smtp_password", "обычный пароль")
    with pytest.raises(RuntimeError, match="пароль приложения"):
        await mailer.send("s", "t")


async def test_not_configured(mail, monkeypatch):
    monkeypatch.setattr(settings, "smtp_password", "")
    assert not settings.email_ready
    with pytest.raises(mailer.MailNotConfigured):
        await mailer.send("s", "t")


def test_proxy_is_used_when_set(monkeypatch):
    calls = []

    class FakeProxy:
        @staticmethod
        def from_url(url):
            calls.append(url)
            return FakeProxy()

        def connect(self, dest_host, dest_port, timeout):
            calls.append((dest_host, dest_port))
            raise OSError("стоп: дальше сеть не нужна")

    import python_socks.sync

    monkeypatch.setattr(python_socks.sync, "Proxy", FakeProxy)
    monkeypatch.setattr(settings, "smtp_proxy", "socks5://127.0.0.1:1080")
    with pytest.raises(OSError):
        mailer._ProxySMTP("smtp.gmail.com", 587, timeout=5)
    assert calls == ["socks5://127.0.0.1:1080", ("smtp.gmail.com", 587)]
