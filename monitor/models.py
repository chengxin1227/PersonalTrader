from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Session = Literal["盘前", "盘中", "盘后"]
_LATE_SESSIONS: dict[str, Session] = {"premarket": "盘前", "regular": "盘中", "afterhours": "盘后"}


class ConditionType(str, Enum):
    PRICE_ABOVE = "price_above"
    PRICE_BELOW = "price_below"
    PERCENT_CHANGE = "percent_change"
    CROSSES_ABOVE = "crosses_above"
    CROSSES_BELOW = "crosses_below"
    PREMARKET_GAIN = "premarket_gain"
    AFTERHOURS_GAIN = "afterhours_gain"
    LATE_SESSION_GAIN = "late_session_gain"
    SUDDEN_GAIN = "sudden_gain"
    REGULAR_CLOSE = "regular_close"
    REGULAR_GAIN = "regular_gain"


class DayGainTier(BaseModel):
    """One OR-branch: prior day gain floor + current session gain floor."""

    min_recent_day_change: float
    value: float


class Condition(BaseModel):
    type: ConditionType
    value: float = 0
    max_market_cap: Optional[float] = None
    min_market_cap: Optional[float] = None
    min_volume: Optional[float] = None
    min_regular_change: Optional[float] = None
    min_regular_volume: Optional[float] = None
    min_prior_change: Optional[float] = None
    min_fade_from_high: Optional[float] = None
    prior_change_lag_minutes: int = Field(default=5, ge=0)
    require_new_high: bool = False
    require_uptrend: bool = False
    require_circuit_breaker: bool = False
    include_circuit_breaker: bool = False
    skip_market_cap: bool = False
    top_n: Optional[int] = Field(default=None, ge=1)
    min_afterhours_touch: Optional[float] = None
    min_regular_touch: Optional[float] = None
    min_recent_day_change: Optional[float] = None
    day_gain_tiers: list[DayGainTier] = Field(default_factory=list)
    recent_day_count: int = Field(default=5, ge=1)
    intraday_touch: bool = False
    stable_minutes: Optional[int] = Field(default=None, ge=1)
    stable_low: Optional[float] = None
    stable_high: Optional[float] = None
    late_session_hours: int = Field(default=1, ge=1)
    late_session: str = "regular"
    sudden_window_seconds: int = Field(default=60, ge=15)
    pool_min_change: Optional[float] = None
    min_price: Optional[float] = None
    close_minutes: int = Field(default=5, ge=1)
    # When true with regular_close + a concrete symbol: match if abs(change) > value.
    abs_change: bool = False


class Rule(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    board_description: Optional[str] = None
    label: Optional[str] = None
    notify: Optional[str] = None
    group: Optional[Session] = None
    symbol: str
    enabled: bool = True
    cooldown_minutes: int = Field(default=60, ge=0)
    # First match (condition.value) is silent; after this delay, alert when gain > confirm_value.
    confirm_delay_minutes: int = Field(default=0, ge=0)
    confirm_value: Optional[float] = None
    condition: Condition
    slack_message: Optional[str] = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @property
    def condition_session(self) -> Session | None:
        """The session the condition actually runs in."""
        kind = self.condition.type
        if kind is ConditionType.PREMARKET_GAIN:
            return "盘前"
        if kind is ConditionType.AFTERHOURS_GAIN:
            return "盘后"
        if kind in {ConditionType.LATE_SESSION_GAIN, ConditionType.SUDDEN_GAIN}:
            return _LATE_SESSIONS.get(self.condition.late_session)
        if kind in {ConditionType.REGULAR_CLOSE, ConditionType.REGULAR_GAIN}:
            return "盘中"
        return None

    @property
    def summary(self) -> str:
        """Reason under single-stock alerts (and fallback label text)."""
        return (self.description or self.name).strip()

    @property
    def board_summary(self) -> str:
        """Reason under half-hour top-N digests."""
        return (self.board_description or self.description or self.name).strip()

    @model_validator(mode="after")
    def group_matches_condition(self) -> "Rule":
        actual = self.condition_session
        if self.group is not None and actual is not None and self.group != actual:
            raise ValueError(f"rule {self.id}: group is {self.group} but the condition runs {actual}")
        return self

    @property
    def is_scanner(self) -> bool:
        return self.condition.type in {
            ConditionType.PREMARKET_GAIN,
            ConditionType.AFTERHOURS_GAIN,
            ConditionType.LATE_SESSION_GAIN,
            ConditionType.SUDDEN_GAIN,
            ConditionType.REGULAR_CLOSE,
            ConditionType.REGULAR_GAIN,
        } or self.symbol in {"*", "ALL"}

    @field_validator("id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("rule id cannot be empty")
        return cleaned


class ImportantWatchlistConfig(BaseModel):
    """Which alerted rules also land in the important 同花顺 group (我的自选)."""

    always: list[str] = Field(default_factory=list)
    event: list[str] = Field(default_factory=list)
    # Half-hour / digest boards whose leaders also go to 我的自选.
    board: list[str] = Field(default_factory=list)
    afterhours_open_minutes: int = Field(default=15, ge=1)
    afterhours_open: list[str] = Field(default_factory=list)
    # Last N hours of the rule's own session (盘前/盘中/盘后).
    session_late_hours: int = Field(default=2, ge=1)
    session_late: list[str] = Field(default_factory=list)
    # Deprecated aliases kept for older yaml.
    afterhours_late_hours: int = Field(default=2, ge=1)
    afterhours_late: list[str] = Field(default_factory=list)
    reminder_sync: list[str] = Field(default_factory=list)


class ImportantWatchedSymbolsConfig(BaseModel):
    """Named tickers polled each open session; alert when session gain clears the floor."""

    min_change: float = 10
    cooldown_minutes: int = Field(default=240, ge=0)
    notify: str = "Important-reminder"
    symbols: list[str] = Field(default_factory=list)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, symbols: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for symbol in symbols:
            cleaned = symbol.strip().upper()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                ordered.append(cleaned)
        return ordered


class ImportantReminderConfig(BaseModel):
    key: str
    at: str
    label: str
    rule_ids: list[str] = Field(default_factory=list)
    sync_watchlist: bool = False

    @field_validator("key", "label")
    @classmethod
    def strip_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("cannot be empty")
        return cleaned

    @field_validator("at")
    @classmethod
    def parse_clock(cls, value: str) -> str:
        text = value.strip()
        hour_s, _, minute_s = text.partition(":")
        hour = int(hour_s)
        minute = int(minute_s)
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError(f"invalid reminder time: {value}")
        return f"{hour:02d}:{minute:02d}"


class ImportantConfig(BaseModel):
    ths_group: str = "我的自选"
    high_priority_rule_ids: list[str] = Field(default_factory=list)
    watchlist: ImportantWatchlistConfig = Field(default_factory=ImportantWatchlistConfig)
    watched_symbols: ImportantWatchedSymbolsConfig = Field(
        default_factory=ImportantWatchedSymbolsConfig
    )
    reminders: list[ImportantReminderConfig] = Field(default_factory=list)


class RulesConfig(BaseModel):
    poll_interval_seconds: int = Field(default=1, ge=1)
    scanner_interval_seconds: int = Field(default=1, ge=1)
    background_scan_interval_seconds: int = Field(default=5, ge=1)
    poll_when_closed: bool = True
    feed: str = "iex"
    scanner_feed: str = "iex"
    stream_all_trades: bool = False
    watchlist: list[str] = Field(default_factory=list)
    important: ImportantConfig = Field(default_factory=ImportantConfig)
    rules: list[Rule] = Field(default_factory=list)

    @field_validator("rules", mode="before")
    @classmethod
    def flatten_sessions(cls, rules: object) -> object:
        # `rules:` with every entry commented out loads as None.
        if rules is None:
            return []
        if not isinstance(rules, dict):
            return rules
        flat: list[object] = []
        for section, items in rules.items():
            session = _LATE_SESSIONS.get(section)
            if session is None:
                raise ValueError(f"rules section {section} must be one of {', '.join(_LATE_SESSIONS)}")
            for item in items or []:
                if isinstance(item, dict):
                    if item.get("group") not in (None, session):
                        raise ValueError(f"rule {item.get('id')} is under {section} but says {item['group']}")
                    item = {**item, "group": session}
                flat.append(item)
        return flat

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
    company: Optional[str] = None
    price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    daily_open: Optional[float] = None
    daily_high: Optional[float] = None
    daily_low: Optional[float] = None
    prev_close: Optional[float] = None
    change_pct: Optional[float] = None
    regular_price: Optional[float] = None
    regular_change_pct: Optional[float] = None
    premarket_price: Optional[float] = None
    premarket_high: Optional[float] = None
    premarket_change_pct: Optional[float] = None
    after_hours_price: Optional[float] = None
    after_hours_change_pct: Optional[float] = None
    volume: Optional[int] = None
    regular_volume: Optional[int] = None
    session_low_pct: Optional[float] = None
    session_high_pct: Optional[float] = None
    session_uptrend: Optional[bool] = None
    prior_session_spike: Optional[bool] = None
    session_circuit_breaker: Optional[bool] = None
    recent_day_spike: Optional[bool] = None
    recent_day_gain_floors: Optional[list[float]] = None
    sec_status: Optional[str] = None
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
    label: Optional[str] = None
    notify: Optional[str] = None
    symbol: str
    message: str
    price: Optional[float] = None
    change_pct: Optional[float] = None
    url: Optional[str] = None
    # Same-symbol alerts in this ET session (盘前/盘中/盘后), including this one. 1 = first.
    repeat_count: Optional[int] = None
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
