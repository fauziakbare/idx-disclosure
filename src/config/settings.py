from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Role 1: Triage & Vision
    TRIAGE_BASE_URL: str | None = Field(default=None, description="Base URL for Triage LLM API")
    TRIAGE_API_KEY: str = Field(default="", description="API key for Triage LLM")
    TRIAGE_MODEL_NAME: str = Field(default="", description="Model name for Triage LLM")
    TRIAGE_VISION_MODEL_NAME: str = Field(default="", description="Vision model name for PDF fallback")

    # Role 2: Extractor, Reasoner & Writer
    REASONER_BASE_URL: str | None = Field(default=None, description="Base URL for Reasoner LLM API")
    REASONER_API_KEY: str = Field(default="", description="API key for Reasoner LLM")
    REASONER_MODEL_NAME: str = Field(default="", description="Model name for Reasoner LLM")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = Field(default="", description="Telegram bot token")
    TELEGRAM_CHAT_ID: str = Field(default="", description="Telegram chat ID for notifications")
    ALLOWED_TELEGRAM_USER_ID: str = Field(default="", description="Allowed Telegram user ID for auth guard")

    # IDX Fetch
    IDX_API_BASE_URL: str = Field(
        default="https://www.idx.co.id/umbraco/Surface/ListedCompany/GetAnnouncement",
        description="IDX disclosure API base URL",
    )
    FETCH_DATE_FROM: str = Field(default="", description="Fetch start date (YYYY-MM-DD). Empty = today.")
    FETCH_DATE_TO: str = Field(default="", description="Fetch end date (YYYY-MM-DD). Empty = today.")
    FETCH_PAGE_SIZE: int = Field(default=30, description="Jumlah pengumuman yang ditarik per request")
    FETCH_INDEX_FROM: int = Field(default=1, description="Halaman awal penarikan IDX")

    # Proxy Configuration (Optional - specifically for IDX Fetcher & PDF Parser)
    PROXY_LIST_URL: str | None = Field(default=None, description="Webshare dynamic proxy list URL (plain text: ip:port:user:pass)")

    # Database
    DATABASE_URL: str = Field(default="sqlite+aiosqlite:///./idx_watcher.db", description="Database connection URL")
    TURSO_AUTH_TOKEN: str = Field(default="", description="Turso (libsql) auth token for cloud database")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }

settings = Settings()
