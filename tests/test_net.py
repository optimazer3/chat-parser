import pytest

from chat_parser.net import aiogram_proxy, network_hint, parse_proxy, telethon_proxy


def test_empty_means_no_proxy():
    assert parse_proxy("") is None
    assert aiogram_proxy("") is None
    assert telethon_proxy("  ") is None


def test_socks5_local_vpn_client():
    p = parse_proxy("socks5://127.0.0.1:10808")
    assert (p.scheme, p.host, p.port) == ("socks5", "127.0.0.1", 10808)
    assert aiogram_proxy("socks5://127.0.0.1:10808") == "socks5://127.0.0.1:10808"
    assert telethon_proxy("socks5://127.0.0.1:10808") == {
        "proxy_type": "socks5", "addr": "127.0.0.1", "port": 10808, "rdns": True,
    }


def test_socks5h_is_normalized():
    assert aiogram_proxy("socks5h://127.0.0.1:1080") == "socks5://127.0.0.1:1080"


def test_http_proxy_with_credentials_roundtrip():
    url = "http://user%40mail:p%3Ass@proxy.example.com:3128"
    p = parse_proxy(url)
    assert (p.username, p.password) == ("user@mail", "p:ss")
    assert aiogram_proxy(url) == url
    t = telethon_proxy(url)
    assert t["proxy_type"] == "http" and t["username"] == "user@mail" and t["password"] == "p:ss"


@pytest.mark.parametrize(
    "bad, fragment",
    [
        ("vless://127.0.0.1:443", "не поддерживается"),
        ("127.0.0.1:10808", "допиши его в начало: socks5://127.0.0.1:10808"),
        ("socks5://127.0.0.1", "адрес и порт"),
        ("socks5://127.0.0.1:abc", "адрес и порт"),
    ],
)
def test_bad_proxy_explains_itself(bad, fragment):
    with pytest.raises(ValueError, match=fragment):
        parse_proxy(bad)


def test_hint_points_to_vpn_and_setting():
    text = network_hint("ClientConnectorError", "")
    assert "VPN" in text and "TG_PROXY=socks5://" in text and "не задан" in text
