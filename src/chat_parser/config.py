from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram
    tg_api_id: int
    tg_api_hash: str
    tg_session: str = "data/optics"
    tg_session_string: str = ""

    # Supabase / Postgres
    database_url: str

    # Anthropic
    anthropic_api_key: str | None = None
    model_extract: str = "claude-opus-5"
    model_synth: str = "claude-opus-5"
    extract_concurrency: int = 4

    # Прочее
    author_salt: str
    ingest_pause: float = 1.2

    # Пороги нормализации
    thread_gap_minutes: int = 10
    thread_max_messages: int = 80
    min_words_standalone: int = 8


settings = Settings()  # type: ignore[call-arg]
