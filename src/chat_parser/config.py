from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

WHERE_TO_GET = {
    "database_url": "Supabase -> Project Settings -> Database -> Connection string (URI, Session pooler)",
    "author_salt": 'python -c "import secrets; print(secrets.token_hex(16))"',
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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

    # --- Прочее ---
    author_salt: str
    ingest_pause: float = 1.2
    extract_concurrency: int = 4
    cluster_batch: int = 120
    daily_run_hour_utc: int = 6

    # --- Пороги нормализации ---
    thread_gap_minutes: int = 10
    thread_max_messages: int = 80
    min_words_standalone: int = 8

    @property
    def telegram_ready(self) -> bool:
        return bool(self.tg_api_id and self.tg_api_hash)

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
        missing = [
            str(err["loc"][0]) for err in e.errors() if err["type"] == "missing"
        ]
        if not missing:
            raise
        lines = ["", "Не заполнен .env — не хватает переменных:", ""]
        for name in missing:
            hint = WHERE_TO_GET.get(name, "")
            lines.append(f"  {name.upper()}" + (f"  — {hint}" if hint else ""))
        lines += ["", f"Файл .env ожидается здесь: {Path('.env').resolve()}"]
        if not Path(".env").is_file():
            lines.append("Его нет. Создай:  cp .env.example .env")
        lines.append("")
        raise SystemExit("\n".join(lines)) from None


settings = _load()
