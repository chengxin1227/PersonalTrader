from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_data_url: str = "https://data.alpaca.markets"
    alpaca_trade_url: str = "https://paper-api.alpaca.markets"
    alpaca_feed: str = "iex"
    slack_webhook_url: str = ""
    moomoo_host: str = "127.0.0.1"
    moomoo_port: int = 11111
    monitor_host: str = "0.0.0.0"
    monitor_port: int = 8080
    rules_path: Path = Field(default=ROOT / "config" / "rules.yaml")
    data_dir: Path = Field(default=ROOT / "data")

    @property
    def alpaca_configured(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_api_secret)

    @property
    def slack_configured(self) -> bool:
        return bool(self.slack_webhook_url)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
