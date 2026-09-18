"""Клиент к OpenAI-совместимому шлюзу (polza.ai и любой другой).

Шлюзы отличаются тем, какой режим структурированного вывода поддерживают:
одни умеют json_schema со strict, другие только json_object, третьи вообще
ничего. Поэтому режим определяется на лету: пробуем от строгого к слабому,
первый сработавший запоминаем на процесс. Определённый режим печатает
`chat-parser llm-check` — его можно зафиксировать в LLM_JSON_MODE, чтобы
не тратить попытки на каждом старте.
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from openai import APIStatusError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from .config import settings

T = TypeVar("T", bound=BaseModel)

MODES = ("json_schema", "json_object", "text")
# Коды, по которым понятно: шлюз не умеет этот режим, а не запрос плохой.
UNSUPPORTED = {400, 404, 415, 422, 501}
FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.M)


class LLMError(RuntimeError):
    pass


def harden_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema под strict-режим: никаких лишних полей, всё обязательно."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: walk(v) for k, v in node.items()}
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            return node
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(model.model_json_schema())


def extract_json(text: str) -> str:
    """Вытаскивает JSON из ответа: модели любят обрамлять его ```json и болтовнёй."""
    cleaned = FENCE_RE.sub("", text).strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("в ответе модели нет JSON-объекта")
    return cleaned[start : end + 1]


class LLM:
    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        json_mode: str | None = None,
    ) -> None:
        self.model = model or settings.llm_model
        self.client = AsyncOpenAI(
            base_url=base_url or settings.llm_base_url,
            api_key=api_key or settings.llm_api_key,
            timeout=180.0,
            max_retries=2,
        )
        mode = json_mode or settings.llm_json_mode
        self.mode: str | None = None if mode == "auto" else mode

    async def list_models(self) -> list[str]:
        page = await self.client.models.list()
        return sorted(m.id for m in page.data)

    async def _raw(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        mode: str,
        max_tokens: int,
        repair: tuple[str, str] | None = None,
    ) -> str:
        sys_text = system
        if mode != "json_schema":
            # Шлюз схему не примет — значит, она должна быть в промпте.
            sys_text += (
                "\n\nОтветь СТРОГО одним JSON-объектом по схеме, без пояснений "
                "и без markdown-обрамления:\n"
                + json.dumps(harden_schema(schema), ensure_ascii=False)
            )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": sys_text},
            {"role": "user", "content": user},
        ]
        if repair:
            messages += [
                {"role": "assistant", "content": repair[0][:4000]},
                {
                    "role": "user",
                    "content": (
                        f"Ответ не прошёл валидацию: {repair[1]}\n"
                        "Верни ТОЛЬКО исправленный JSON по схеме."
                    ),
                },
            ]

        kwargs: dict[str, Any] = {}
        if mode == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": harden_schema(schema),
                },
            }
        elif mode == "json_object":
            kwargs["response_format"] = {"type": "json_object"}

        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
            temperature=0,
            **kwargs,
        )
        return resp.choices[0].message.content or ""

    async def structured(
        self, system: str, user: str, schema: type[T], max_tokens: int = 8000
    ) -> T:
        """Возвращает провалидированный объект или бросает LLMError."""
        modes = [self.mode] if self.mode else list(MODES)
        last: Exception | None = None

        for mode in modes:
            try:
                text = await self._raw(system, user, schema, mode, max_tokens)
            except APIStatusError as e:
                if e.status_code in UNSUPPORTED and mode != MODES[-1] and not self.mode:
                    last = e
                    continue  # режим не поддерживается — пробуем более слабый
                raise

            try:
                obj = schema.model_validate_json(extract_json(text))
                self.mode = mode
                return obj
            except (ValidationError, ValueError, json.JSONDecodeError) as e:
                last = e

            # Одна попытка починить ответ в том же режиме.
            try:
                text2 = await self._raw(
                    system, user, schema, mode, max_tokens, repair=(text, str(last))
                )
                obj = schema.model_validate_json(extract_json(text2))
                self.mode = mode
                return obj
            except (ValidationError, ValueError, json.JSONDecodeError, APIStatusError) as e:
                last = e

        raise LLMError(f"не удалось получить валидный JSON ({self.model}): {last}")


def build_llm(model: str | None = None) -> LLM:
    return LLM(model=model)
