from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Application & Auth
    APP_NAME: str = "Relicta"
    SECRET_KEY: str = Field(
        default="insecure-dev-secret-key-replace-immediately-in-env",
        min_length=16,
    )
    ALLOW_REGISTRATION: bool = True
    SESSION_COOKIE_NAME: str = "relicta_session"
    SESSION_MAX_AGE_SECONDS: int = 60 * 60 * 24 * 30  # 30 days
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/relicta.db"

    # Currency & Localization
    DEFAULT_BASE_CURRENCY: str = "CZK"
    SUPPORTED_CURRENCIES: tuple[str, ...] = ("CZK", "EUR", "USD", "GBP", "PLN")
    CNB_RATES_URL: str = (
        "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/"
        "kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt"
    )
    ECB_FALLBACK_URL: str = "https://api.frankfurter.app/latest?from=EUR&to=CZK,USD,GBP,PLN"
    FX_REFRESH_INTERVAL_SECONDS: int = 86400  # 24 hours

    # Scraper & Scheduler Behavior
    SCRAPER_REQUEST_TIMEOUT_SECONDS: float = 20.0
    SCRAPER_MIN_DELAY_SECONDS: float = 3.0
    SCRAPER_MAX_DELAY_SECONDS: float = 7.0
    SCHEDULER_JITTER_SECONDS: int = 45
    SCHEDULER_TICK_SECONDS: float = 5.0
    MAX_CONSECUTIVE_FAILURES: int = 5
    SEEN_LISTINGS_RETENTION_DAYS: int = 45

    # Price Drop Thresholds (in CZK equivalent or percentage)
    PRICE_DROP_MIN_PERCENT: float = 1.0
    PRICE_DROP_MIN_ABSOLUTE_CZK: float = 50.0

    # Facebook Marketplace Settings (cookie-based authentication)
    FB_COOKIE_C_USER: str = ""
    FB_COOKIE_XS: str = ""
    FB_COOKIE_DATR: str = ""
    FB_MARKETPLACE_LOCATION: str = "prague"

    # eBay API Settings
    EBAY_CLIENT_ID: str = ""
    EBAY_CLIENT_SECRET: str = ""
    EBAY_MARKETPLACE_ID: str = "EBAY_DE"
    EBAY_DEFAULT_SHIP_TO_COUNTRY: str = "CZ"
    EBAY_OAUTH_URL: str = "https://api.ebay.com/identity/v1/oauth2/token"
    EBAY_BROWSE_SEARCH_URL: str = "https://api.ebay.com/buy/browse/v1/item_summary/search"

    # Notification Defaults
    TELEGRAM_DEFAULT_BOT_TOKEN: str = ""
    NTFY_DEFAULT_SERVER_URL: str = "https://ntfy.sh"

    # SMTP Relay Settings
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "alerts@localhost"
    SMTP_USE_TLS: bool = True

    @field_validator("DEFAULT_BASE_CURRENCY", mode="before")
    @classmethod
    def normalize_currency(cls, v: str) -> str:
        return v.strip().upper()

    def ensure_data_dir(self) -> None:
        if self.DATABASE_URL.startswith("sqlite"):
            raw_path = self.DATABASE_URL.split("///")[-1]
            db_path = Path(raw_path).resolve()
            db_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_data_dir()
    return settings