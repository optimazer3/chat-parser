import httpx2
import pytest
from openai import APIStatusError
from pydantic import BaseModel

from chat_parser.llm import LLM, LLMError, extract_json, harden_schema


class Inner(BaseModel):
    a: int
    b: str


class Outer(BaseModel):
    ok: bool
    items: list[Inner]


def test_harden_schema_locks_objects_down():
    """strict-режим требует additionalProperties=false и все поля обязательными."""
    schema = harden_schema(Outer)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"ok", "items"}
    inner = schema["$defs"]["Inner"]
    assert inner["additionalProperties"] is False
    assert set(inner["required"]) == {"a", "b"}


@pytest.mark.parametrize(
    "raw",
    [
        '{"ok": true, "items": []}',
        '```json\n{"ok": true, "items": []}\n```',
        'Конечно! Вот результат:\n{"ok": true, "items": []}\nГотово.',
    ],
)
def test_extract_json_survives_model_chatter(raw):
    assert extract_json(raw) == '{"ok": true, "items": []}'


def test_extract_json_raises_without_object():
    with pytest.raises(ValueError):
        extract_json("никакого JSON тут нет")


def _llm(mode="auto"):
    return LLM(model="m", base_url="http://gateway.invalid/v1", api_key="k", json_mode=mode)


def _status_error(code):
    request = httpx2.Request("POST", "http://gateway.invalid/v1/chat/completions")
    return APIStatusError("nope", response=httpx2.Response(code, request=request), body=None)


@pytest.mark.asyncio
async def test_falls_back_when_gateway_rejects_json_schema(monkeypatch):
    """Шлюз не умеет json_schema -> молча переходим на json_object."""
    tried = []

    async def fake_raw(self, system, user, schema, mode, max_tokens, repair=None):
        tried.append(mode)
        if mode == "json_schema":
            raise _status_error(400)
        return '{"ok": true, "items": []}'

    monkeypatch.setattr(LLM, "_raw", fake_raw)
    llm = _llm()
    result = await llm.structured("sys", "user", Outer)
    assert result.ok is True
    assert tried == ["json_schema", "json_object"]
    assert llm.mode == "json_object"  # запомнили рабочий режим


@pytest.mark.asyncio
async def test_working_mode_is_reused(monkeypatch):
    tried = []

    async def fake_raw(self, system, user, schema, mode, max_tokens, repair=None):
        tried.append(mode)
        return '{"ok": true, "items": []}'

    monkeypatch.setattr(LLM, "_raw", fake_raw)
    llm = _llm()
    await llm.structured("sys", "user", Outer)
    await llm.structured("sys", "user", Outer)
    assert tried == ["json_schema", "json_schema"]


@pytest.mark.asyncio
async def test_invalid_json_is_repaired_once(monkeypatch):
    calls = []

    async def fake_raw(self, system, user, schema, mode, max_tokens, repair=None):
        calls.append(repair is not None)
        return '{"ok": true, "items": []}' if repair else "мусор без json"

    monkeypatch.setattr(LLM, "_raw", fake_raw)
    result = await _llm("json_object").structured("sys", "user", Outer)
    assert result.ok is True
    assert calls == [False, True]  # первый ответ битый, второй — починка


@pytest.mark.asyncio
async def test_gives_up_with_clear_error(monkeypatch):
    async def fake_raw(self, system, user, schema, mode, max_tokens, repair=None):
        return "никакого json"

    monkeypatch.setattr(LLM, "_raw", fake_raw)
    with pytest.raises(LLMError, match="валидный JSON"):
        await _llm().structured("sys", "user", Outer)


@pytest.mark.asyncio
async def test_pinned_mode_does_not_fall_back(monkeypatch):
    """Если режим зафиксирован в LLM_JSON_MODE, ошибку надо показать, а не глушить."""

    async def fake_raw(self, system, user, schema, mode, max_tokens, repair=None):
        raise _status_error(400)

    monkeypatch.setattr(LLM, "_raw", fake_raw)
    with pytest.raises(APIStatusError):
        await _llm("json_schema").structured("sys", "user", Outer)


# ---------- «думающие» модели и пустые ответы ----------

from types import SimpleNamespace as NS  # noqa: E402

from chat_parser.llm import TokenBudgetExhausted  # noqa: E402


def _resp(content, finish="stop", reasoning=None):
    msg = NS(content=content, reasoning_content=reasoning, model_extra={})
    return NS(choices=[NS(message=msg, finish_reason=finish)], usage=None)


@pytest.mark.asyncio
async def test_budget_eaten_by_reasoning_fails_fast(monkeypatch):
    """Лимит съели рассуждения -> понятная ошибка сразу, без перебора режимов."""
    llm = _llm()
    calls = []

    async def create(**kw):
        calls.append(kw.get("response_format"))
        return _resp("", finish="length", reasoning="думаю " * 50)

    monkeypatch.setattr(llm.client.chat.completions, "create", create)
    with pytest.raises(TokenBudgetExhausted, match="рассуждения"):
        await llm.structured("sys", "user", Outer, max_tokens=200)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_empty_answer_tries_next_mode(monkeypatch):
    llm = _llm()
    answers = iter([_resp("", finish="stop"), _resp('{"ok": true, "items": []}')])

    async def create(**kw):
        return next(answers)

    monkeypatch.setattr(llm.client.chat.completions, "create", create)
    result = await llm.structured("sys", "user", Outer)
    assert result.ok is True
    assert llm.mode == "json_object"


@pytest.mark.asyncio
async def test_extra_body_reaches_request(monkeypatch):
    llm = _llm()
    llm.extra_body = {"enable_thinking": False}
    seen = {}

    async def create(**kw):
        seen.update(kw)
        return _resp('{"ok": true, "items": []}')

    monkeypatch.setattr(llm.client.chat.completions, "create", create)
    await llm.structured("sys", "user", Outer)
    assert seen["extra_body"] == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_final_error_shows_what_model_said(monkeypatch):
    llm = _llm()

    async def create(**kw):
        return _resp("Извините, я не могу ответить в формате JSON")

    monkeypatch.setattr(llm.client.chat.completions, "create", create)
    with pytest.raises(LLMError, match="не могу ответить"):
        await llm.structured("sys", "user", Outer)
