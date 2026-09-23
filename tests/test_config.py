import pytest

from chat_parser.config import Settings


def make(**kw):
    base = {"database_url": "postgresql://u:p@host/db", "author_salt": "s"}
    base.update(kw)
    return Settings(**base)


def test_example_placeholders_are_not_real_keys():
    """Заглушки из старого .env.example не должны включать выгрузку."""
    s = make(tg_api_id=1234567, tg_api_hash="x" * 32)
    assert s.telegram_placeholders
    assert not s.telegram_ready


def test_real_looking_keys_enable_telegram():
    s = make(tg_api_id=20481234, tg_api_hash="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    assert s.telegram_ready


def test_missing_keys_disable_telegram():
    assert not make(tg_api_id=None, tg_api_hash="").telegram_ready


def test_supabase_direct_host_is_recognized():
    s = make(database_url="postgresql://postgres:pw@db.sqrprtazaljwrlnscfux.supabase.co:5432/postgres")
    assert s.supabase_direct_ref == "sqrprtazaljwrlnscfux"


def test_supabase_pooler_host_is_fine():
    s = make(
        database_url="postgresql://postgres.abc:pw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
    )
    assert s.supabase_direct_ref is None


def test_extra_body_parsed():
    assert make(llm_extra_body='{"enable_thinking": false}').llm_extra_body_dict == {
        "enable_thinking": False
    }
    assert make(llm_extra_body="").llm_extra_body_dict == {}


def test_extra_body_invalid_json_explains_itself():
    with pytest.raises(SystemExit, match="невалидный JSON"):
        _ = make(llm_extra_body="{enable_thinking: false}").llm_extra_body_dict
