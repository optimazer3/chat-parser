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
