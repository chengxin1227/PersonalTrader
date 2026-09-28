from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ConditionType(str, Enum):
    PRICE_ABOVE = "price_above"
    PRICE_BELOW = "price_below"
    PERCENT_CHANGE = "percent_change"
    CROSSES_ABOVE = "crosses_above"
    CROSSES_BELOW = "crosses_below"
    PREMARKET_GAIN = "premarket_gain"
    AFTERHOURS_GAIN = "afterhours_gain"


class Condition(BaseModel):
    type: ConditionType
    value: float
    max_market_cap: Optional[float] = None
    min_volume: Optional[float] = None


class Rule(BaseModel):
    id: str
    name: str
    symbol: str
    enabled: bool = True
    cooldown_minutes: int = Field(default=60, ge=0)
    condition: Condition
    slack_message: Optional[str] = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @property
    def is_scanner(self) -> bool:
        return self.condition.type in {
            ConditionType.PREMARKET_GAIN,
            ConditionType.AFTERHOURS_GAIN,
        } or self.symbol in {"*", "ALL"}

    @field_validator("id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("rule id cannot be empty")
        return cleaned


class RulesConfig(BaseModel):
    poll_interval_seconds: int = Field(default=1, ge=1)
    scanner_interval_seconds: int = Field(default=1, ge=1)
    poll_when_closed: bool = True
    feed: str = "iex"
    scanner_feed: str = "iex"
    stream_all_trades: bool = False
    watchlist: list[str] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)

    @field_validator("watchlist")
    @classmethod
    def normalize_watchlist(cls, symbols: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for symbol in symbols:
            cleaned = symbol.strip().upper()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                ordered.append(cleaned)
        return ordered

    def all_symbols(self) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for symbol in [
            *self.watchlist,
            *[rule.symbol for rule in self.rules if not rule.is_scanner],
        ]:
            if symbol in {"*", "ALL"} or symbol in seen:
                continue
            seen.add(symbol)
            ordered.append(symbol)
        return ordered


class Quote(BaseModel):
    symbol: str
    price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    daily_open: Optional[float] = None
    daily_high: Optional[float] = None
    daily_low: Optional[float] = None
    prev_close: Optional[float] = None
    change_pct: Optional[float] = None
    volume: Optional[int] = None
    updated_at: Optional[datetime] = None
    trade_at: Optional[datetime] = None
    quote_at: Optional[datetime] = None


class MarketClock(BaseModel):
    is_open: bool
    next_open: Optional[datetime] = None
    next_close: Optional[datetime] = None
    timestamp: Optional[datetime] = None


class Alert(BaseModel):
    id: str
    rule_id: str
    rule_name: str
    symbol: str
    message: str
    price: Optional[float] = None
    change_pct: Optional[float] = None
    fired_at: datetime


class RuleEvaluation(BaseModel):
    matched: bool
    reason: str = ""


class MonitorStatus(BaseModel):
    running: bool
    last_poll_at: Optional[datetime] = None
    last_error: Optional[str] = None
    market: Optional[MarketClock] = None
    watchlist: list[str] = Field(default_factory=list)
    enabled_rules: int = 0
    slack_configured: bool = False
    alpaca_configured: bool = False
