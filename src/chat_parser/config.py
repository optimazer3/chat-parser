import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

WHERE_TO_GET = {
    "database_url": "Supabase -> Project Settings -> Database -> Connection string (URI, Session pooler)",
    "author_salt": 'python -c "import secrets; print(secrets.token_hex(16))"',
}


# Значения-заглушки из прежних версий .env.example: их копировали вместе
# с файлом, и код принимал их за настоящие ключи.
PLACEHOLDER_API_IDS = {1234567}
SUPABASE_DIRECT_RE = re.compile(r"@db\.([a-z0-9]+)\.supabase\.co\b")


def is_placeholder_hash(value: str) -> bool:
    return bool(value) and set(value.lower()) == {"x"}


class Settings(BaseSettings):
    # env_ignore_empty: строка вида «TG_API_ID=» значит «не задано», а не
    # «пустая строка». Иначе любое пустое числовое поле роняет запуск.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # --- Telegram: аккаунт-сборщик (MTProto) ---
    # Необязательны: без них работает всё, кроме выгрузки из Telegram.
    # Данные можно залить вручную: chat-parser import-json
    tg_api_id: int | None = None
    tg_api_hash: str = ""
    tg_session: str = "data/optics"
    tg_session_string: str = ""

    # --- Telegram: бот-панель (отдельная сущность, свой токен от BotFather) ---
    tg_bot_token: str = ""
    tg_admin_ids: str = ""  # "123456789,987654321" — кому можно командовать ботом

    # --- Supabase / Postgres ---
    database_url: str

    # --- LLM: любой OpenAI-совместимый шлюз ---
    llm_base_url: str = "https://polza.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = ""  # точный id смотри через `chat-parser models`
    llm_model_synth: str = ""  # для кластеризации/карточек; пусто -> llm_model
    llm_json_mode: str = "auto"  # auto | json_schema | json_object | text
    llm_max_context_tokens: int = 32000  # для нарезки батчей кластеризации
    # JSON с параметрами конкретного провайдера, уходит в запрос как есть.
    # Например, выключить рассуждения у «думающих» моделей.
    llm_extra_body: str = ""

    # --- Прочее ---
    author_salt: str
    ingest_pause: float = 1.2
    extract_concurrency: int = 4
    # Лимит ответа на один тред. «Думающие» модели изредка рассуждают дольше —
    # тогда тред повторяется один раз с лимитом EXTRACT_MAX_TOKENS_RETRY.
    extract_max_tokens: int = 8000
    extract_max_tokens_retry: int = 24000
    cluster_batch: int = 120
    daily_run_hour_utc: int = 6

    # --- Пороги нормализации ---
    thread_gap_minutes: int = 10
    thread_max_messages: int = 80
    min_words_standalone: int = 8

    @property
    def telegram_placeholders(self) -> bool:
        return self.tg_api_id in PLACEHOLDER_API_IDS or is_placeholder_hash(self.tg_api_hash)

    @property
    def telegram_ready(self) -> bool:
        return (
            bool(self.tg_api_id and self.tg_api_hash) and not self.telegram_placeholders
        )

    @property
    def supabase_direct_ref(self) -> str | None:
        """project ref, если в DATABASE_URL прямое подключение db.<ref>.supabase.co.

        У новых проектов оно работает только по IPv6 и с обычного
        домашнего интернета не резолвится.
        """
        m = SUPABASE_DIRECT_RE.search(self.database_url)
        return m.group(1) if m else None

    @property
    def llm_extra_body_dict(self) -> dict[str, Any]:
        if not self.llm_extra_body.strip():
            return {}
        try:
            value = json.loads(self.llm_extra_body)
        except json.JSONDecodeError as e:
            raise SystemExit(
                f"LLM_EXTRA_BODY — невалидный JSON: {e}\n"
                f'пример: LLM_EXTRA_BODY={{"enable_thinking": false}}'
            ) from None
        if not isinstance(value, dict):
            raise SystemExit("LLM_EXTRA_BODY должен быть JSON-объектом: {...}")
        return value

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.tg_admin_ids.replace(" ", "").split(",") if x}

    @property
    def synth_model(self) -> str:
        return self.llm_model_synth or self.llm_model


def _load() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as e:
        missing, invalid = [], []
        for err in e.errors():
            name = str(err["loc"][0]).upper() if err["loc"] else "?"
            if err["type"] == "missing":
                missing.append(name)
            else:
                invalid.append(f"  {name} = {err.get('input')!r} — {err['msg']}")
        lines = [""]
        if missing:
            lines += ["Не заполнен .env — не хватает переменных:", ""]
            for name in missing:
                hint = WHERE_TO_GET.get(name.lower(), "")
                lines.append(f"  {name}" + (f"  — {hint}" if hint else ""))
            lines.append("")
        if invalid:
            lines += ["В .env неверные значения:", "", *invalid, ""]
        lines.append(f"Файл .env ожидается здесь: {Path('.env').resolve()}")
        if not Path(".env").is_file():
            lines.append("Его нет. Создай:  copy .env.example .env  (в Linux/macOS: cp)")
        lines.append("")
        raise SystemExit("\n".join(lines)) from None


settings = _load()
