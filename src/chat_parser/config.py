from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Telegram: аккаунт-сборщик (MTProto) ---
    tg_api_id: int
    tg_api_hash: str
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
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.tg_admin_ids.replace(" ", "").split(",") if x}

    @property
    def synth_model(self) -> str:
        return self.llm_model_synth or self.llm_model


settings = Settings()  # type: ignore[call-arg]
