from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from collections.abc import Iterable
from typing import Any, Optional

from monitor.alpaca import AlpacaClient
from monitor.config import Settings
from monitor.marketcap import MarketCapClient
from monitor.moomoo_client import (
    MoomooClient,
    afterhours_leaders,
    afterhours_touch_session,
    is_upward_halt,
    observed_session_peak,
    peak_leaders,
    regular_touch_change_pct,
    unchanged_symbols,
)
from monitor.models import (
    Alert,
    ConditionType,
    DayGainTier,
    ImportantConfig,
    MonitorStatus,
    Quote,
    Rule,
    RulesConfig,
)
from monitor.notify import NotifyRoutes
from monitor.quotes import apply_quote, apply_trade, previous_price
from monitor.problems import ProblemLatch, problem_notice
from monitor.pushover import PushoverClient
from monitor.rules import (
    evaluate_afterhours_scan,
    evaluate_last_hour_scan,
    evaluate_premarket_scan,
    evaluate_regular_day_scan,
    evaluate_regular_peak_fade,
    evaluate_stable_afterhours,
    evaluate_rule,
    hour_change_pct,
    fade_leaderboard_line,
    leaderboard_line,
    load_rules,
    matched_day_gain_tier,
    peak_leaderboard_line,
    price_age_seconds_ago,
    render_message,
    save_rules,
    trim_price_samples,
)
from monitor.session import (
    AFTERHOURS_START,
    ET,
    PREMARKET_START,
    REGULAR_START,
    CHECK_REMINDER_NOTIFY,
    CheckReminder,
    afterhours_board_slot,
    afterhours_just_opened,
    afterhours_open_window,
    session_board_slot,
    session_late_window,
    afterhours_stable_due,
    afterhours_stable_missed,
    check_reminder_text,
    due_check_reminders,
    et_session_name,
    extended_session_open,
    format_nz_clock,
    is_afterhours,
    is_premarket,
    is_regular_session,
    is_trading_day,
    last_hour_phase,
    now_et,
    premarket_mark,
    recent_trading_days,
    regular_close_approach,
    seconds_until_extended_open,
)
from monitor.slack import SlackClient
from monitor.quote_link import quote_url
from monitor.repeat_alerts import with_repeat_history
from monitor import store as store_mod
from monitor.store import Store
from monitor.stream import AlpacaStream, StreamEvent
from monitor.tonghuashun import TonghuashunClient
from monitor.universe import UniverseCache

logger = logging.getLogger("personaltrader.monitor")


def _symbols_text(symbols: Iterable[str]) -> str:
    """Comma-separated symbols for rule logs; empty → '0' so greps stay consistent."""
    items = [symbol for symbol in symbols if symbol]
    return ", ".join(items) if items else "0"


def _is_trend_rule(rule: Rule) -> bool:
    return rule.condition.skip_market_cap or rule.condition.require_uptrend


def _is_spike_rule(rule: Rule) -> bool:
    return (
        rule.condition.min_prior_change is not None
        and not rule.condition.require_new_high
    )


def _is_peak_fade_rule(rule: Rule) -> bool:
    # Digests with top_n use the leaderboard path; do not treat them as singles.
    return (
        rule.condition.min_fade_from_high is not None
        and not _is_leaderboard_rule(rule)
        and rule.condition.type
        in {
            ConditionType.AFTERHOURS_GAIN,
            ConditionType.PREMARKET_GAIN,
            ConditionType.REGULAR_GAIN,
        }
    )


def _is_peak_fade_board(rule: Rule) -> bool:
    """Touched-pool digest ranked by peak − current (highest giveback first)."""
    return (
        _is_leaderboard_rule(rule)
        and _is_touched_leaderboard(rule)
        and rule.condition.min_fade_from_high is not None
    )


def _is_new_high_rule(rule: Rule) -> bool:
    return rule.condition.require_new_high


def _is_breaker_rule(rule: Rule) -> bool:
    return rule.condition.require_circuit_breaker


def _is_close_rule(rule: Rule) -> bool:
    return rule.condition.type is ConditionType.REGULAR_CLOSE


def _is_symbol_close_rule(rule: Rule) -> bool:
    """Named-symbol close check (e.g. IXIC ±2%), not a scanner pool."""
    return _is_close_rule(rule) and rule.symbol not in {"*", "ALL"}


def _rule_notify_key(rule: Rule) -> str:
    return (rule.notify or rule.id).strip() or rule.id


def _alert_cooldown_key(rule: Rule, symbol: str, *, pool: bool = False) -> str:
    """Cooldown / once-per-session key for a rule+symbol.

    Default: share across 盘前/盘中/盘后 copies via notify.
    once_per_session: scope to this rule id + session day (same rule once per 时段).
    """
    notify = _rule_notify_key(rule)
    symbol = symbol.strip().upper()
    if pool:
        return f"{notify}:pool:{symbol}"
    if rule.once_per_session:
        session = rule.condition_session or rule.group or ""
        day = now_et().date().isoformat()
        return f"{rule.id}:{session}:{day}:{symbol}"
    return f"{notify}:{symbol}"


def _active_session_condition_type() -> ConditionType | None:
    if is_afterhours():
        return ConditionType.AFTERHOURS_GAIN
    if is_premarket():
        return ConditionType.PREMARKET_GAIN
    if is_regular_session():
        return ConditionType.REGULAR_GAIN
    return None


def _rules_for_active_session(rules: list[Rule]) -> list[Rule]:
    """Keep only rules whose condition matches the open US session."""
    want = _active_session_condition_type()
    if want is None:
        return []
    return [rule for rule in rules if rule.condition.type is want]


def _is_regular_gain_rule(rule: Rule) -> bool:
    return rule.condition.type is ConditionType.REGULAR_GAIN


def _is_leaderboard_rule(rule: Rule) -> bool:
    return rule.condition.top_n is not None


def _is_touched_leaderboard(rule: Rule) -> bool:
    condition = rule.condition
    return (
        condition.min_afterhours_touch is not None
        or condition.min_regular_touch is not None
        or condition.include_circuit_breaker
    )


def _is_pool_gain_board(rule: Rule) -> bool:
    """Half-hour board: halt/day pool ranked by current session gain (not peak fade)."""
    return (
        _is_leaderboard_rule(rule)
        and _is_touched_leaderboard(rule)
        and rule.condition.min_fade_from_high is None
    )


def _is_peak_top_board(rule: Rule) -> bool:
    """Plain top-N board ranked by session peak gain (not touch-pool / fade)."""
    return (
        _is_leaderboard_rule(rule)
        and not _is_touched_leaderboard(rule)
        and not _is_day_spike_rule(rule)
        and rule.condition.min_fade_from_high is None
    )


def _is_day_pool_board(rule: Rule) -> bool:
    """Half-hour board: prior-day gain pool ranked by current session gain."""
    return (
        _is_leaderboard_rule(rule)
        and _is_day_spike_rule(rule)
        and not _is_touched_leaderboard(rule)
        and rule.condition.min_fade_from_high is None
    )


def _board_session_key(rule: Rule) -> str | None:
    kind = rule.condition.type
    if kind is ConditionType.AFTERHOURS_GAIN:
        return "afterhours"
    if kind is ConditionType.PREMARKET_GAIN:
        return "premarket"
    if kind is ConditionType.REGULAR_GAIN:
        return "regular"
    return None


def _board_slot_for_rule(rule: Rule) -> datetime | None:
    session = _board_session_key(rule)
    if session is None:
        return None
    interval = rule.condition.board_interval_minutes or 30
    return session_board_slot(session, interval_minutes=interval)


def _board_min_volume(rule: Rule, slot: datetime) -> int | None:
    """Waive volume only on the session-open board slot."""
    minimum = rule.condition.min_volume
    if minimum is None:
        return None
    kind = rule.condition.type
    open_at = {
        ConditionType.AFTERHOURS_GAIN: AFTERHOURS_START,
        ConditionType.PREMARKET_GAIN: PREMARKET_START,
        ConditionType.REGULAR_GAIN: REGULAR_START,
    }.get(kind)
    if open_at is not None and slot.time() == open_at:
        return None
    return int(minimum)


def _is_day_spike_rule(rule: Rule) -> bool:
    return (
        rule.condition.min_recent_day_change is not None
        or bool(rule.condition.day_gain_tiers)
    )


def _is_day_spike_singles_rule(rule: Rule) -> bool:
    """day25 / 5d-boost singles — not half-hour boards that also carry day-gain floors."""
    return _is_day_spike_rule(rule) and not _is_leaderboard_rule(rule)


def _day_gain_floors(rule: Rule) -> list[float]:
    """Distinct prior-day gain floors this rule needs recorded / pooled."""
    if rule.condition.day_gain_tiers:
        found: list[float] = []
        seen: set[float] = set()
        for tier in rule.condition.day_gain_tiers:
            floor = tier.min_recent_day_change
            if floor in seen:
                continue
            seen.add(floor)
            found.append(floor)
        return found
    if rule.condition.min_recent_day_change is not None:
        return [rule.condition.min_recent_day_change]
    return []


def _day_gain_include_today(rule: Rule, *, session_allows: bool) -> bool:
    """Whether the day-gain lookback may include the current ET date.

    「过去N日」(N>1 or day_gain_tiers) always means completed sessions only — today
    is covered by today-touch / halt rules instead. Single-day floors (day25) may
    include today after the regular close (after-hours only).
    """
    if not session_allows:
        return False
    if rule.condition.day_gain_tiers:
        return False
    return rule.condition.recent_day_count <= 1


def _is_halt_or_day_rule(rule: Rule) -> bool:
    # Leaderboard variants are digest boards, not single-name pool alerts.
    return (
        _is_breaker_rule(rule)
        and _is_day_spike_rule(rule)
        and not _is_leaderboard_rule(rule)
    )


def _sorts_ths_by_peak_fade(rule: Rule) -> bool:
    """Halt/50% 同花顺 / fade boards: sort by peak % − current %, larger giveback first."""
    if _is_peak_fade_board(rule):
        return True
    if _is_pool_gain_board(rule):
        return False
    # halt-or-day50 singles skip a dedicated rule group (我的自选 only).
    if _is_halt_or_day_rule(rule):
        return False
    return (
        _is_touched_leaderboard(rule)
        and rule.condition.min_regular_touch is not None
        and rule.condition.include_circuit_breaker
    )


def _skips_ths_rule_group(rule: Rule) -> bool:
    """Singles that alert into 我的自选 only — no dedicated Tonghuashun rule group."""
    if _is_leaderboard_rule(rule):
        return False
    if rule.skip_ths_group:
        return True
    # halt-or-day50 / 5d-boost
    return _is_halt_or_day_rule(rule) or bool(rule.condition.day_gain_tiers)


def peak_fade_score(peak: float | None, close: float | None) -> float:
    """How much the name gave back from its intraday high to the regular close."""
    if peak is None or close is None:
        return float("-inf")
    return peak - close


def _is_ah_touch_board_rule(rule: Rule) -> bool:
    """Half-hour board among names that once cleared an after-hours touch."""
    return _is_leaderboard_rule(rule) and rule.condition.min_afterhours_touch is not None


def _is_ah_touch_singles_rule(rule: Rule) -> bool:
    """Also alert each name the first time AH clears `value` (with volume after the open)."""
    return _is_ah_touch_board_rule(rule) and rule.condition.value > 0


def _afterhours_scan_rule(rule: Rule) -> Rule:
    """Copy of ``rule`` with volume waived at the 16:00 after-hours open."""
    slot = afterhours_board_slot()
    if (
        rule.condition.min_volume is None
        or slot is None
        or slot.time() != AFTERHOURS_START
    ):
        return rule
    scan_rule = rule.model_copy(deep=True)
    scan_rule.condition.min_volume = None
    return scan_rule


def _confirm_scan_rule(rule: Rule) -> Rule:
    """After the first arm touch, scan at confirm_value (e.g. 0%) instead of arm threshold."""
    if rule.confirm_delay_minutes <= 0 or rule.confirm_value is None:
        return rule
    scan_rule = rule.model_copy(deep=True)
    scan_rule.condition.value = rule.confirm_value
    if scan_rule.condition.day_gain_tiers:
        scan_rule.condition.day_gain_tiers = [
            DayGainTier(
                min_recent_day_change=tier.min_recent_day_change,
                value=rule.confirm_value,
            )
            for tier in scan_rule.condition.day_gain_tiers
        ]
    return scan_rule

def _is_stable_rule(rule: Rule) -> bool:
    condition = rule.condition
    return (
        condition.stable_minutes is not None
        and condition.stable_low is not None
        and condition.stable_high is not None
    )


def _is_last_hour_rule(rule: Rule) -> bool:
    return rule.condition.type is ConditionType.LATE_SESSION_GAIN


def _is_sudden_rule(rule: Rule) -> bool:
    return rule.condition.type is ConditionType.SUDDEN_GAIN


def _shared_rule_group_names(config: RulesConfig) -> list[str]:
    """Rule group names used in more than one of 盘前/盘中/盘后."""
    sessions_by_name: dict[str, set[str]] = {}
    for rule in config.rules:
        if not rule.enabled or not rule.name:
            continue
        if _skips_ths_rule_group(rule):
            continue
        session = (rule.group or rule.condition_session or "").strip()
        if session not in {"盘前", "盘中", "盘后"}:
            continue
        sessions_by_name.setdefault(rule.name, set()).add(session)
    return sorted(name for name, sessions in sessions_by_name.items() if len(sessions) > 1)


def _important_reminders(config: ImportantConfig) -> list[CheckReminder]:
    from datetime import time as time_cls

    reminders: list[CheckReminder] = []
    for item in config.reminders:
        hour_s, _, minute_s = item.at.partition(":")
        reminders.append(
            CheckReminder(
                item.key,
                time_cls(int(hour_s), int(minute_s)),
                tuple(item.rule_ids),
                item.label,
            )
        )
    return reminders


def _wants_important_watchlist(
    important: ImportantConfig,
    rule: Rule,
    *,
    board: bool = False,
    moment: datetime | None = None,
) -> bool:
    """Whether this alert's symbols should also land in 我的自选.

    All single-ticket alerts go to 我的自选. Half-hour boards only if listed in
    ``watchlist.board`` (usually empty).
    """
    del moment  # kept for call-site compatibility
    watch = important.watchlist
    if board:
        return rule.id in watch.board
    # Digests use board=True; never treat a top_n rule as a single.
    if rule.condition.top_n is not None:
        return False
    return True


def _is_high_priority(
    important: ImportantConfig,
    rule: Rule,
    *,
    moment: datetime | None = None,
) -> bool:
    """High-priority Pushover, except afterhours_open rules during the open window.

    Those still go to 我的自选, but use normal priority for the first N minutes.
    """
    if rule.id not in important.high_priority_rule_ids:
        return False
    watch = important.watchlist
    if rule.id in watch.afterhours_open and afterhours_open_window(
        moment, minutes=watch.afterhours_open_minutes
    ):
        return False
    return True


def opend_is_down(exc: BaseException) -> bool:
    text = str(exc).lower()
    if "network interruption" in text:
        return True
    return "moomoo" in text and any(
        part in text for part in ("disconnect", "interruption", "connect", "timeout", "timed out")
    )


class MonitorEngine:
    def __init__(
        self,
        settings: Settings,
        alpaca: AlpacaClient,
        slack: SlackClient,
        store: Store,
        verbose: bool = False,
        moomoo: MoomooClient | None = None,
        pushover: PushoverClient | None = None,
    ) -> None:
        self.settings = settings
        self.routes = NotifyRoutes.load(settings)
        self.alpaca = alpaca
        self.moomoo = moomoo
        self.slack = slack
        self.pushover = pushover
        self.ths = TonghuashunClient(settings)
        self._ths_tasks: set[asyncio.Task] = set()
        self._ths_lock = asyncio.Lock()
        # session (盘前/盘中/盘后) -> ET date last cleared for that session's groups
        self._ths_cleared_session_day: dict[str, str] = {}
        self._ths_groups_synced = False
        self.store = store
        self.verbose = verbose
        self.quotes: dict[str, Quote] = {}
        self.last_poll_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.market = None
        self._running = False
        self._lock = asyncio.Lock()
        # Serialize cross-rule same-symbol notify claims (parallel scanners).
        self._symbol_alert_lock = asyncio.Lock()
        self._last_scan_at: Optional[datetime] = None
        self._universe = UniverseCache(settings.data_dir)
        self._marketcap = MarketCapClient()
        self._watchlist_only = False
        self._stream_all_trades = False
        self._trade_count = 0
        self._cap_cache: dict[str, float] = {}
        self._config_loaded_at: Optional[datetime] = None
        self._cached_config: Optional[RulesConfig] = None
        self._last_market_log: str | None = None
        self._scan_log: dict[str, tuple[tuple[str, ...], datetime]] = {}
        self._uptrend_cache: dict[tuple[str, str], bool] = {}
        self._day_spike_cache: dict[tuple[str, bool, str], bool] = {}
        self._late_baselines: dict[str, dict[str, float]] = {}
        self._late_begin: dict[str, int] = {}
        self._late_day: str | None = None
        self._late_phase: dict[str, str] = {}
        self._sudden_pool: dict[str, set[str]] = {}
        self._sudden_prices: dict[str, dict[str, list[tuple[float, float]]]] = {}
        self._sudden_session: dict[str, float] = {}
        self._sudden_discovered_at: dict[str, float] = {}
        self._sudden_day: str | None = None
        self._kline_blocked_until: Optional[datetime] = None
        self._trend_moomoo = MoomooClient(settings) if moomoo is not None else None
        self._trend_loop_running = False
        self._spike_loop_running = False
        self._regular_loop_running = False
        self._day_spike_loop_running = False
        self._opend_down_notified = False
        self._opend_ok_at: Optional[datetime] = None
        self._problems = ProblemLatch()
        self._board_retry_at: Optional[datetime] = None
        self._breaker_checked_at: Optional[datetime] = None
        self._close_checked_at: Optional[datetime] = None
        self._day_high_checked_at: Optional[datetime] = None
        self._day_high_cursor: dict[tuple[float, float | None], int] = {}
        self._halt_marks: dict[str, float] = {}
        self._halt_marks_day: str | None = None
        self._closing_gains_day: str | None = None
        self._closing_gains_attempted_at: Optional[datetime] = None
        self._trend_watch_at: Optional[datetime] = None
        self._stable_pool: dict[str, dict[str, Quote]] = {}
        self._stable_ready_day: dict[str, str] = {}
        self._stable_done: set[str] = set()
        self._stable_captured_at: Optional[datetime] = None
        self._stable_frozen_day: str | None = None
        self._check_reminder_inflight: set[str] = set()

    def load_config(self, force: bool = False) -> RulesConfig:
        now = datetime.now(timezone.utc)
        if (
            not force
            and self._cached_config is not None
            and self._config_loaded_at
            and (now - self._config_loaded_at).total_seconds() < 15
        ):
            return self._cached_config
        self._cached_config = load_rules(self.settings.rules_path)
        self._config_loaded_at = now
        return self._cached_config

    def save_config(self, config: RulesConfig) -> RulesConfig:
        save_rules(self.settings.rules_path, config)
        self._cached_config = config
        self._config_loaded_at = datetime.now(timezone.utc)
        return config

    def status(self) -> MonitorStatus:
        try:
            config = self.load_config()
            enabled = sum(1 for rule in config.rules if rule.enabled)
            watchlist = config.all_symbols()
        except FileNotFoundError:
            enabled = 0
            watchlist = []
        return MonitorStatus(
            running=self._running,
            last_poll_at=self.last_poll_at,
            last_error=self.last_error,
            market=self.market,
            watchlist=watchlist,
            enabled_rules=enabled,
            slack_configured=self.settings.slack_configured,
            alpaca_configured=self.settings.alpaca_configured,
        )

    async def poll_once(self) -> list[Alert]:
        async with self._lock:
            return await self._poll_once()

    async def _poll_once(self) -> list[Alert]:
        config = self.load_config()
        symbols = config.all_symbols()
        if self.moomoo is None and not self.settings.alpaca_configured:
            self.last_error = "Alpaca API keys are not set"
            return []

        try:
            if self.moomoo is not None:
                self.market = await self.moomoo.clock()
            else:
                self.market = await self.alpaca.clock()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch market clock: %s", exc)
            await self._note_problem(str(exc))
            self.market = None

        if self.market and not self.market.is_open and not config.poll_when_closed:
            self.last_poll_at = datetime.now(timezone.utc)
            self.last_error = None
            return []

        quotes: dict[str, Quote] = {}
        if symbols:
            if self.moomoo is not None:
                quotes = await self._moomoo_snapshots(self.moomoo, symbols)
            else:
                quotes = await self.alpaca.snapshots(
                    symbols, config.feed or self.settings.alpaca_feed
                )
        self.quotes = quotes
        self._log_market()
        if symbols:
            self._log_quotes(quotes)
        state = self.store.load_state()
        fired: list[Alert] = []
        slack_error: str | None = None

        for rule in config.rules:
            if not rule.enabled:
                continue
            if rule.is_scanner:
                continue
            quote = quotes.get(rule.symbol)
            if quote is None:
                logger.warning("No quote for %s (rule %s)", rule.symbol, rule.id)
                continue
            previous = self.store.last_price(state, rule.symbol)
            evaluation = evaluate_rule(rule, quote, previous)
            if self.verbose:
                logger.info("  %s: %s", rule.id, evaluation.reason)
            if not evaluation.matched:
                continue
            alert = await self._fire_alert(rule, quote, state, extra=None)
            if alert is None:
                continue
            slack_error = slack_error or self.last_error
            fired.append(alert)

        if self._should_scan(config):
            open_rules = [
                rule
                for rule in config.rules
                if rule.enabled and rule.is_scanner and self._rule_session_open(rule)
            ]
            afterhours_rules = [
                rule
                for rule in open_rules
                if self.moomoo is not None and rule.condition.type is ConditionType.AFTERHOURS_GAIN
            ]
            trend_rules = [rule for rule in afterhours_rules if _is_trend_rule(rule)]
            ranked_rules = [
                rule
                for rule in afterhours_rules
                if not _is_trend_rule(rule)
                and not _is_spike_rule(rule)
                and not _is_peak_fade_rule(rule)
                and not _is_new_high_rule(rule)
                and not _is_breaker_rule(rule)
                and not _is_leaderboard_rule(rule)
                and not _is_day_spike_rule(rule)
                and not _is_stable_rule(rule)
            ]
            stable_rules = [rule for rule in afterhours_rules if _is_stable_rule(rule)]
            if stable_rules and self.moomoo is not None and self._stable_freeze_due():
                for rule in stable_rules:
                    await self._capture_stable_names(rule, self.moomoo)
                self._stable_frozen_day = now_et().date().isoformat()
            for rule in stable_rules:
                matched = await self._match_stable_afterhours(rule)
                if matched:
                    fired.extend(await self._emit_trend_matches(rule, matched, state))
            if ranked_rules:
                scanned, scan_error = await self._run_afterhours_rules(ranked_rules, state)
                fired.extend(scanned)
                slack_error = slack_error or scan_error
            if trend_rules and not self._trend_loop_running:
                for rule in trend_rules:
                    matched = await self._match_saved_trend(rule)
                    fired.extend(await self._emit_trend_matches(rule, matched, state))
            spike_rules = [rule for rule in open_rules if _is_spike_rule(rule)]
            if spike_rules and not self._spike_loop_running:
                for rule in spike_rules:
                    matched = await self._match_spike_rule(rule)
                    fired.extend(await self._emit_trend_matches(rule, matched, state))
            fade_rules = [rule for rule in open_rules if _is_peak_fade_rule(rule)]
            if fade_rules:
                for rule in fade_rules:
                    matched = await self._match_peak_fade_rule(rule)
                    fired.extend(await self._emit_trend_matches(rule, matched, state))
            new_high_rules = [rule for rule in open_rules if _is_new_high_rule(rule)]
            if new_high_rules and not self._spike_loop_running and not self._regular_loop_running:
                for rule in new_high_rules:
                    matched = await self._match_new_high_rule(rule)
                    fired.extend(await self._emit_trend_matches(rule, matched, state))
            day_spike_rules = [
                rule
                for rule in open_rules
                if _is_day_spike_singles_rule(rule) and not _is_breaker_rule(rule)
            ]
            if day_spike_rules and not self._day_spike_loop_running:
                for rule in day_spike_rules:
                    matched = await self._match_saved_day(rule)
                    fired.extend(await self._emit_trend_matches(rule, matched, state))
            breaker_rules = [
                rule
                for rule in open_rules
                if _is_breaker_rule(rule) and not _is_day_spike_rule(rule)
            ]
            if breaker_rules and not self._regular_loop_running:
                for rule in breaker_rules:
                    matched = await self._match_breaker_afterhours(rule)
                    fired.extend(await self._emit_trend_matches(rule, matched, state))
            halt_or_day_rules = [rule for rule in open_rules if _is_halt_or_day_rule(rule)]
            if halt_or_day_rules and not self._regular_loop_running:
                for rule in halt_or_day_rules:
                    matched = await self._match_halt_or_day_afterhours(rule)
                    fired.extend(await self._emit_trend_matches(rule, matched, state))
            ah_touch_singles = [
                rule for rule in open_rules if _is_ah_touch_singles_rule(rule)
            ]
            if ah_touch_singles and not self._regular_loop_running:
                for rule in ah_touch_singles:
                    matched = await self._match_afterhours_touch_singles(rule)
                    fired.extend(await self._emit_trend_matches(rule, matched, state))
            for rule in open_rules:
                if (
                    rule in afterhours_rules
                    or _is_spike_rule(rule)
                    or _is_peak_fade_rule(rule)
                    or _is_new_high_rule(rule)
                    or _is_breaker_rule(rule)
                    or _is_leaderboard_rule(rule)
                    or _is_day_spike_rule(rule)
                    or _is_regular_gain_rule(rule)
                    # Sudden / last-hour have dedicated loops; never treat as session-gain screens.
                    or _is_sudden_rule(rule)
                    or _is_last_hour_rule(rule)
                    or _is_halt_or_day_rule(rule)
                    or _is_ah_touch_singles_rule(rule)
                    or _is_stable_rule(rule)
                    or _is_trend_rule(rule)
                ):
                    continue
                scanned, scan_error = await self._run_scanner(rule, config, state)
                fired.extend(scanned)
                slack_error = slack_error or scan_error
            self._last_scan_at = datetime.now(timezone.utc)

        if self.moomoo is not None:
            if is_afterhours():
                for rule in config.rules:
                    if (
                        rule.enabled
                        and _is_leaderboard_rule(rule)
                        and rule.condition.type is ConditionType.AFTERHOURS_GAIN
                    ):
                        await self._maybe_send_leaderboard(rule, state)
            elif is_premarket():
                for rule in config.rules:
                    if (
                        rule.enabled
                        and (
                            _is_peak_top_board(rule)
                            or _is_day_pool_board(rule)
                        )
                        and rule.condition.type is ConditionType.PREMARKET_GAIN
                    ):
                        await self._maybe_send_leaderboard(rule, state)

        for symbol, quote in quotes.items():
            self.store.set_last_price(state, symbol, quote.price)
        self.store.save_state(state)
        self.last_poll_at = datetime.now(timezone.utc)
        self.last_error = slack_error
        if self.moomoo is not None:
            self._note_opend_ok()
        return fired

    async def run_forever(self, transport: str = "rest") -> None:
        if transport == "rest":
            await self._run_rest_loop()
            return
        await self._run_stream_loop()

    async def _run_rest_loop(self) -> None:
        self._running = True
        self._trend_loop_running = self._trend_moomoo is not None
        self._spike_loop_running = self._trend_moomoo is not None
        self._regular_loop_running = self._trend_moomoo is not None
        self._day_spike_loop_running = self._trend_moomoo is not None
        trend_task = asyncio.create_task(self._trend_scan_loop()) if self._trend_loop_running else None
        spike_task = asyncio.create_task(self._spike_scan_loop()) if self._spike_loop_running else None
        regular_task = (
            asyncio.create_task(self._regular_scan_loop()) if self._regular_loop_running else None
        )
        day_spike_task = (
            asyncio.create_task(self._day_spike_scan_loop()) if self._day_spike_loop_running else None
        )
        logger.info("Monitor loop started (REST poll)")
        idle_logged = False
        try:
            await self._rest_poll_loop(idle_logged)
        finally:
            self._trend_loop_running = False
            self._spike_loop_running = False
            self._regular_loop_running = False
            self._day_spike_loop_running = False
            tasks = [
                task
                for task in (trend_task, spike_task, regular_task, day_spike_task)
                if task is not None
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _rest_poll_loop(self, idle_logged: bool) -> None:
        while self._running:
            interval = 15
            try:
                config = self.load_config(force=True)
                interval = config.poll_interval_seconds
                await self._maybe_sync_tonghuashun_groups(config)
                if self.moomoo is not None and not extended_session_open():
                    if not idle_logged:
                        wait = seconds_until_extended_open()
                        nxt = now_et() + timedelta(seconds=wait)
                        logger.info(
                            "Outside pre-market and after-hours. Next session %s ET",
                            nxt.strftime("%a %H:%M"),
                        )
                        idle_logged = True
                    await asyncio.sleep(min(60.0, max(1.0, seconds_until_extended_open())))
                    continue
                if idle_logged:
                    logger.info("Session open; scanning every %ss", interval)
                    idle_logged = False
                await self._maybe_send_check_reminders(config)
                await self._run_watched_symbols(config)
                await self.poll_once()
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                if "429" in str(exc):
                    logger.warning("Alpaca rate limit; backing off 30s")
                    await self._note_problem(str(exc))
                    await asyncio.sleep(30)
                    continue
                logger.exception("Poll failed: %s", exc)
                await self._note_opend_failure(exc)
                await self._note_problem(str(exc))
            await asyncio.sleep(interval)

    async def _trend_scan_loop(self) -> None:
        logger.info("Trend rules scan on a separate OpenD connection")
        while self._running:
            interval = 1
            try:
                config = self.load_config()
                interval = config.background_scan_interval_seconds
                if not is_afterhours():
                    await asyncio.sleep(30)
                    continue
                rules = [
                    rule
                    for rule in config.rules
                    if rule.enabled and _is_trend_rule(rule) and self._rule_session_open(rule)
                ]
                if not rules:
                    await asyncio.sleep(interval)
                    continue
                for rule in rules:
                    matched = await self._match_saved_trend(rule)
                    self._note_opend_ok()
                    if not matched:
                        continue
                    async with self._lock:
                        state = self.store.load_state()
                        await self._emit_trend_matches(rule, matched, state)
                        self.store.save_state(state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Trend scan failed: %s", exc)
                await self._note_opend_failure(exc)
                await self._note_problem(str(exc))
            await asyncio.sleep(interval)

    async def _spike_scan_loop(self) -> None:
        logger.info("Pullback rules scan on a separate OpenD connection")
        while self._running:
            interval = 1
            try:
                config = self.load_config()
                interval = config.background_scan_interval_seconds
                if not extended_session_open():
                    await asyncio.sleep(30)
                    continue
                rules = [
                    rule
                    for rule in config.rules
                    if rule.enabled and _is_spike_rule(rule) and self._rule_session_open(rule)
                ]
                new_high_rules = [
                    rule
                    for rule in config.rules
                    if rule.enabled
                    and _is_new_high_rule(rule)
                    and rule.condition.type is not ConditionType.REGULAR_GAIN
                    and self._rule_session_open(rule)
                ]
                if not rules and not new_high_rules:
                    await asyncio.sleep(interval)
                    continue
                for rule in rules:
                    matched = await self._match_spike_rule(rule)
                    self._note_opend_ok()
                    if not matched:
                        continue
                    async with self._lock:
                        state = self.store.load_state()
                        await self._emit_trend_matches(rule, matched, state)
                        self.store.save_state(state)
                for rule in new_high_rules:
                    matched = await self._match_new_high_rule(rule)
                    self._note_opend_ok()
                    if not matched:
                        continue
                    async with self._lock:
                        state = self.store.load_state()
                        await self._emit_trend_matches(rule, matched, state)
                        self.store.save_state(state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Pullback scan failed: %s", exc)
                await self._note_opend_failure(exc)
                await self._note_problem(str(exc))
            await asyncio.sleep(interval)

    async def _day_spike_scan_loop(self) -> None:
        logger.info("Recent day-gain rules scan on a separate OpenD connection")
        while self._running:
            interval = 1
            try:
                config = self.load_config()
                interval = config.background_scan_interval_seconds
                if not extended_session_open():
                    await asyncio.sleep(30)
                    continue
                rules = [
                    rule
                    for rule in config.rules
                    if rule.enabled
                    and self._rule_session_open(rule)
                    and (
                        (_is_day_spike_singles_rule(rule) and not _is_breaker_rule(rule))
                        or (
                            _is_halt_or_day_rule(rule)
                            and rule.condition.type is ConditionType.PREMARKET_GAIN
                        )
                    )
                ]
                if not rules:
                    await asyncio.sleep(interval)
                    continue
                for rule in rules:
                    if _is_halt_or_day_rule(rule):
                        matched = await self._match_halt_or_day(rule)
                    else:
                        matched = await self._match_saved_day(rule)
                    self._note_opend_ok()
                    if not matched:
                        continue
                    async with self._lock:
                        state = self.store.load_state()
                        await self._emit_trend_matches(rule, matched, state)
                        self.store.save_state(state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Day-gain scan failed: %s", exc)
                await self._note_opend_failure(exc)
                await self._note_problem(str(exc))
            await asyncio.sleep(interval)

    async def _regular_scan_loop(self) -> None:
        logger.info("Regular-session watch on a separate OpenD connection")
        while self._running:
            try:
                config = self.load_config()
                await self._maybe_send_check_reminders(config)
                await self._run_watched_symbols(config)
                rules = [
                    rule
                    for rule in config.rules
                    if rule.enabled and _is_breaker_rule(rule) and not _is_day_spike_rule(rule)
                ]
                halt_or_day_rules = [
                    rule for rule in config.rules if rule.enabled and _is_halt_or_day_rule(rule)
                ]
                last_hour_rules = [
                    rule for rule in config.rules if rule.enabled and _is_last_hour_rule(rule)
                ]
                sudden_rules = [
                    rule for rule in config.rules if rule.enabled and _is_sudden_rule(rule)
                ]
                stable_rules = [
                    rule for rule in config.rules if rule.enabled and _is_stable_rule(rule)
                ]
                day_gain_rules = [
                    rule
                    for rule in config.rules
                    if rule.enabled and _is_day_spike_singles_rule(rule)
                ]
                closing_rules = [
                    rule
                    for rule in day_gain_rules
                    if not rule.condition.intraday_touch
                ]
                trend_rules = [
                    rule for rule in config.rules if rule.enabled and _is_trend_rule(rule)
                ]
                gain_rules = [
                    rule for rule in config.rules if rule.enabled and _is_regular_gain_rule(rule)
                ]
                if (
                    not rules
                    and not halt_or_day_rules
                    and not last_hour_rules
                    and not sudden_rules
                    and not stable_rules
                    and not day_gain_rules
                    and not trend_rules
                    and not gain_rules
                ):
                    await asyncio.sleep(30)
                    continue
                if is_regular_session():
                    for rule in gain_rules:
                        if (
                            _is_new_high_rule(rule)
                            or _is_peak_fade_rule(rule)
                            or _is_leaderboard_rule(rule)
                            or _is_day_spike_rule(rule)
                        ):
                            continue
                        matched = await self._match_regular_gain(rule)
                        self._note_opend_ok()
                        if not matched:
                            continue
                        async with self._lock:
                            state = self.store.load_state()
                            await self._emit_trend_matches(rule, matched, state)
                            self.store.save_state(state)
                    regular_day = [
                        rule
                        for rule in day_gain_rules
                        if rule.condition.type is ConditionType.REGULAR_GAIN
                        and not _is_breaker_rule(rule)
                    ]
                    for rule in regular_day:
                        matched = await self._match_saved_day(rule)
                        self._note_opend_ok()
                        if not matched:
                            continue
                        async with self._lock:
                            state = self.store.load_state()
                            await self._emit_trend_matches(rule, matched, state)
                            self.store.save_state(state)
                    regular_halt_or_day = [
                        rule
                        for rule in halt_or_day_rules
                        if rule.condition.type is ConditionType.REGULAR_GAIN
                    ]
                    for rule in regular_halt_or_day:
                        matched = await self._match_halt_or_day(rule)
                        self._note_opend_ok()
                        if not matched:
                            continue
                        async with self._lock:
                            state = self.store.load_state()
                            await self._emit_trend_matches(rule, matched, state)
                            self.store.save_state(state)
                    regular_fade = [
                        rule
                        for rule in config.rules
                        if rule.enabled
                        and _is_peak_fade_rule(rule)
                        and rule.condition.type is ConditionType.REGULAR_GAIN
                    ]
                    for rule in regular_fade:
                        matched = await self._match_peak_fade_rule(rule)
                        self._note_opend_ok()
                        if not matched:
                            continue
                        async with self._lock:
                            state = self.store.load_state()
                            await self._emit_trend_matches(rule, matched, state)
                            self.store.save_state(state)
                    regular_new_high = [
                        rule
                        for rule in config.rules
                        if rule.enabled
                        and _is_new_high_rule(rule)
                        and rule.condition.type is ConditionType.REGULAR_GAIN
                    ]
                    for rule in regular_new_high:
                        matched = await self._match_new_high_rule(rule)
                        self._note_opend_ok()
                        if not matched:
                            continue
                        async with self._lock:
                            state = self.store.load_state()
                            await self._emit_trend_matches(rule, matched, state)
                            self.store.save_state(state)
                    # day25 / 5d / halt: only 盘中 copies feed RTH day-gain + pool alerts.
                    active_day = _rules_for_active_session(day_gain_rules)
                    active_halt = _rules_for_active_session(halt_or_day_rules)
                    touch_rules = [
                        rule for rule in active_day if rule.condition.intraday_touch
                    ]
                    touch_rules.extend(
                        rule
                        for rule in _rules_for_active_session(
                            [
                                item
                                for item in config.rules
                                if item.enabled and _is_leaderboard_rule(item)
                            ]
                        )
                        if rule.condition.min_regular_touch is not None
                    )
                    if touch_rules:
                        await self._record_day_gains(touch_rules)
                        self._note_opend_ok()
                    if stable_rules and self._stable_capture_due():
                        client = self._trend_moomoo or self.moomoo
                        if client is not None:
                            for rule in stable_rules:
                                await self._capture_stable_names(rule, client)
                                self._note_opend_ok()
                    if (
                        trend_rules
                        and regular_close_approach()
                        and self._trend_watch_due()
                    ):
                        for rule in trend_rules:
                            await self._capture_trend_watch(rule)
                            self._note_opend_ok()
                    halt_record_rules = rules + [
                        rule for rule in active_halt if not _is_close_rule(rule)
                    ]
                    if halt_record_rules and self._breaker_due():
                        newly_halted: list[Quote] = []
                        seen_halt: set[str] = set()
                        for rule in halt_record_rules:
                            for quote in await self._record_circuit_breakers(rule):
                                if quote.symbol in seen_halt:
                                    continue
                                seen_halt.add(quote.symbol)
                                newly_halted.append(quote)
                            self._note_opend_ok()
                        for rule in active_halt:
                            if _is_close_rule(rule):
                                continue
                            await self._emit_halt_or_day_pool_entries(rule, newly_halted)
                    close_rules = [
                        rule
                        for rule in config.rules
                        if rule.enabled
                        and _is_close_rule(rule)
                        and regular_close_approach(minutes=rule.condition.close_minutes)
                    ]
                    if close_rules and self._close_due():
                        for rule in close_rules:
                            if _is_symbol_close_rule(rule):
                                matched = await self._match_symbol_close(rule)
                            elif _is_breaker_rule(rule) and not _is_day_spike_rule(rule):
                                matched = await self._match_breaker_close(rule)
                            elif _is_day_spike_singles_rule(rule) and not _is_breaker_rule(rule):
                                matched = await self._match_day_gain_close(rule)
                            else:
                                continue
                            self._note_opend_ok()
                            if not matched:
                                continue
                            async with self._lock:
                                state = self.store.load_state()
                                await self._emit_trend_matches(rule, matched, state)
                                self.store.save_state(state)
                    await self._run_late_rules(last_hour_rules)
                    await self._run_sudden_rules(
                        [rule for rule in sudden_rules if rule.condition.late_session == "regular"]
                    )
                    regular_boards = [
                        rule
                        for rule in config.rules
                        if rule.enabled
                        and _is_leaderboard_rule(rule)
                        and rule.condition.type is ConditionType.REGULAR_GAIN
                    ]
                    if regular_boards:
                        async with self._lock:
                            state = self.store.load_state()
                            for rule in regular_boards:
                                await self._maybe_send_leaderboard(rule, state)
                            self.store.save_state(state)
                elif is_afterhours():
                    if closing_rules and self._closing_gains_due():
                        await self._record_closing_gains(closing_rules)
                        self._closing_gains_day = now_et().date().isoformat()
                        self._note_opend_ok()
                    # Catch names that spiked above the touch then faded before the live screen saw them.
                    # Only 盘后 copies of day25 / 5d / halt — not 盘前/盘中 variants.
                    active_day = _rules_for_active_session(day_gain_rules)
                    afterhours_touch_day = [
                        rule for rule in active_day if rule.condition.intraday_touch
                    ]
                    afterhours_touch_day.extend(
                        rule
                        for rule in _rules_for_active_session(
                            [
                                item
                                for item in config.rules
                                if item.enabled and _is_leaderboard_rule(item)
                            ]
                        )
                        if rule.condition.min_regular_touch is not None
                    )
                    if afterhours_touch_day:
                        await self._record_day_gains(afterhours_touch_day)
                        self._note_opend_ok()
                    board_touch_rules = [
                        rule
                        for rule in config.rules
                        if rule.enabled
                        and _is_leaderboard_rule(rule)
                        and rule.condition.min_afterhours_touch is not None
                    ]
                    if board_touch_rules:
                        await self._record_afterhours_touches(board_touch_rules)
                        self._note_opend_ok()
                    ah_touch_singles = [
                        rule
                        for rule in config.rules
                        if rule.enabled and _is_ah_touch_singles_rule(rule)
                    ]
                    if ah_touch_singles and self._breaker_due():
                        for rule in ah_touch_singles:
                            matched = await self._match_afterhours_touch_singles(rule)
                            self._note_opend_ok()
                            if not matched:
                                continue
                            async with self._lock:
                                state = self.store.load_state()
                                await self._emit_trend_matches(rule, matched, state)
                                self.store.save_state(state)
                    afterhours_breakers = [rule for rule in rules if not _is_close_rule(rule)]
                    if afterhours_breakers and self._breaker_due():
                        for rule in afterhours_breakers:
                            matched = await self._match_breaker_afterhours(rule)
                            self._note_opend_ok()
                            if not matched:
                                continue
                            async with self._lock:
                                state = self.store.load_state()
                                await self._emit_trend_matches(rule, matched, state)
                                self.store.save_state(state)
                    afterhours_halt_or_day = [
                        rule
                        for rule in halt_or_day_rules
                        if not _is_close_rule(rule)
                        and rule.condition.type is ConditionType.AFTERHOURS_GAIN
                    ]
                    if afterhours_halt_or_day and self._breaker_due():
                        for rule in afterhours_halt_or_day:
                            matched = await self._match_halt_or_day_afterhours(rule)
                            self._note_opend_ok()
                            if not matched:
                                continue
                            async with self._lock:
                                state = self.store.load_state()
                                await self._emit_trend_matches(rule, matched, state)
                                self.store.save_state(state)
                    await self._run_late_rules(last_hour_rules)
                    await self._run_sudden_rules(
                        [rule for rule in sudden_rules if rule.condition.late_session == "afterhours"]
                    )
                elif is_premarket():
                    await self._run_late_rules(last_hour_rules)
                    await self._run_sudden_rules(
                        [rule for rule in sudden_rules if rule.condition.late_session == "premarket"]
                    )
                else:
                    await asyncio.sleep(30)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Circuit breaker scan failed: %s", exc)
                await self._note_opend_failure(exc)
                await self._note_problem(str(exc))
                # Back off when the shared stock-filter budget is exhausted.
                if "high frequency" in str(exc).lower() or "too frequent" in str(exc).lower():
                    await asyncio.sleep(20)
                    continue
            await asyncio.sleep(15)

    def _closing_gains_due(self) -> bool:
        day = now_et().date().isoformat()
        if self._closing_gains_day == day:
            return False
        now = datetime.now(timezone.utc)
        last = self._closing_gains_attempted_at
        if last is not None and (now - last).total_seconds() < 60:
            return False
        self._closing_gains_attempted_at = now
        return True

    def _day_high_check_due(self) -> bool:
        # Snapshot quota is 60 calls / 30s shared with halt checks; one batched
        # call is enough, but keep this to about one call per minute.
        now = datetime.now(timezone.utc)
        last = self._day_high_checked_at
        if last is not None and (now - last).total_seconds() < 60:
            return False
        self._day_high_checked_at = now
        return True

    async def _record_day_gains(self, rules: list[Rule]) -> None:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return
        day = now_et().date().isoformat()
        seen: set[tuple[float, float | None, float | None]] = set()
        for rule in rules:
            minimum = rule.condition.min_recent_day_change
            if minimum is None:
                minimum = rule.condition.min_regular_touch
            if minimum is None:
                continue
            key = (minimum, rule.condition.max_market_cap, rule.condition.min_market_cap)
            if key in seen:
                continue
            seen.add(key)
            use_touch = any(
                (
                    item.condition.intraday_touch
                    or item.condition.min_regular_touch is not None
                )
                and (
                    (item.condition.min_recent_day_change or item.condition.min_regular_touch)
                    == minimum
                )
                and item.condition.max_market_cap == rule.condition.max_market_cap
                and item.condition.min_market_cap == rule.condition.min_market_cap
                for item in rules
            )
            found = await client.regular_gainers(
                minimum,
                max_market_cap=rule.condition.max_market_cap,
                min_market_cap=rule.condition.min_market_cap,
            )
            touched: list[Quote] = list(found)
            for quote in found:
                if quote.change_pct is not None:
                    self.store.note_peak_gain(day, quote.symbol, quote.change_pct)
            if use_touch:
                floor = min(20.0, minimum)
                if floor < minimum and self._day_high_check_due():
                    wide = await client.regular_gainers(
                        floor,
                        max_market_cap=rule.condition.max_market_cap,
                        min_market_cap=rule.condition.min_market_cap,
                    )
                    have = {
                        symbol.upper()
                        for symbol in self.store.day_gain_symbols(minimum, [day])
                    }
                    have.update(quote.symbol.upper() for quote in found)
                    pending = [
                        quote
                        for quote in wide
                        if quote.symbol.upper() not in have
                    ]
                    pending.sort(
                        key=lambda quote: quote.change_pct or 0.0, reverse=True
                    )
                    start = self._day_high_cursor.get(key, 0)
                    if start >= len(pending):
                        start = 0
                    batch = pending[start : start + 20]
                    self._day_high_cursor[key] = start + len(batch)
                    snaps = await self._moomoo_snapshots(
                        client, [quote.symbol for quote in batch]
                    )
                    for snap in snaps.values():
                        peak = regular_touch_change_pct(
                            change_pct=snap.change_pct,
                            high=snap.daily_high,
                            prev_close=snap.prev_close,
                            pre_high=snap.premarket_high,
                        )
                        if peak is None:
                            continue
                        self.store.note_peak_gain(day, snap.symbol, peak)
                        if peak > minimum:
                            snap.change_pct = peak
                            touched.append(snap)
            added = self.store.note_day_gains(
                day, minimum, [quote.symbol for quote in touched]
            )
            if added:
                logger.info("Recorded day gain > %.0f%%: %s", minimum, _symbols_text(added))
                by_symbol = {quote.symbol.upper(): quote for quote in touched}
                fresh = [by_symbol[symbol] for symbol in added if symbol in by_symbol]
                for alert_rule in _rules_for_active_session(rules):
                    if not _is_halt_or_day_rule(alert_rule):
                        continue
                    threshold = alert_rule.condition.min_recent_day_change
                    if threshold is None:
                        threshold = alert_rule.condition.min_regular_touch
                    if threshold != minimum:
                        continue
                    if alert_rule.condition.max_market_cap != rule.condition.max_market_cap:
                        continue
                    if alert_rule.condition.min_market_cap != rule.condition.min_market_cap:
                        continue
                    await self._emit_halt_or_day_pool_entries(alert_rule, fresh)

    async def _emit_halt_or_day_pool_entries(self, rule: Rule, quotes: list[Quote]) -> None:
        """Push once when a name first joins the halt/50% pool (盘前/盘后 only).

        盘中 halt-or-day50 only alerts on the later session +5% path — pool membership
        is still recorded for the fade board / later sessions.
        """
        if not quotes or not _is_halt_or_day_rule(rule):
            return
        if rule.condition.type is ConditionType.REGULAR_GAIN:
            return
        async with self._lock:
            state = self.store.load_state()
            for quote in quotes:
                await self._fire_alert(
                    rule,
                    quote,
                    state,
                    extra=None,
                    cooldown_key=_alert_cooldown_key(rule, quote.symbol, pool=True),
                )
            self.store.save_state(state)

    async def _record_afterhours_touches(self, rules: list[Rule]) -> None:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return
        day = now_et().date().isoformat()
        seen: set[tuple[float, float | None, float | None]] = set()
        seen_at = datetime.now(timezone.utc)
        for rule in rules:
            minimum = rule.condition.min_afterhours_touch
            if minimum is None:
                continue
            key = (minimum, rule.condition.max_market_cap, rule.condition.min_market_cap)
            if key in seen:
                continue
            seen.add(key)
            found = await client.scan_afterhours(minimum, rule.condition.max_market_cap, min_market_cap=rule.condition.min_market_cap)
            session = afterhours_touch_session(minimum)
            added: list[str] = []
            for quote, _cap in found:
                if quote.change_pct is None or quote.change_pct <= minimum:
                    continue
                if self.store.spike_seen_at(day, session, quote.symbol) is None:
                    added.append(quote.symbol)
                self.store.note_spike(day, session, quote.symbol, seen_at)
            if added:
                logger.info(
                    "Recorded after-hours touch > %.0f%%: %s",
                    minimum,
                    _symbols_text(added),
                )

    async def _record_closing_gains(self, rules: list[Rule]) -> None:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return
        day = now_et().date().isoformat()
        seen: set[tuple[float, float | None, float | None, float | None]] = set()
        for rule in rules:
            for minimum in _day_gain_floors(rule):
                key = (
                    minimum,
                    rule.condition.max_market_cap,
                    rule.condition.min_market_cap,
                    rule.condition.min_volume,
                )
                if key in seen:
                    continue
                seen.add(key)
                found = await client.regular_gainers(
                    minimum,
                    max_market_cap=rule.condition.max_market_cap,
                    min_market_cap=rule.condition.min_market_cap,
                    min_volume=rule.condition.min_volume,
                )
                symbols = [quote.symbol for quote in found]
                self.store.replace_day_gains(day, minimum, symbols)
                logger.info(
                    "Closing gain > %.0f%%: %s",
                    minimum,
                    _symbols_text(symbols),
                )

    def _trend_watch_due(self) -> bool:
        now = datetime.now(timezone.utc)
        last = self._trend_watch_at
        if last is not None and (now - last).total_seconds() < 60:
            return False
        self._trend_watch_at = now
        return True

    async def _capture_trend_watch(self, rule: Rule) -> None:
        client = self._trend_moomoo or self.moomoo
        minimum = rule.condition.min_regular_change
        if client is None or minimum is None:
            return
        day = now_et().date().isoformat()
        found = await client.regular_gainers(
            minimum,
            max_market_cap=rule.condition.max_market_cap,
            min_market_cap=rule.condition.min_market_cap,
        )
        if not found:
            self.store.replace_trend_watch(rule.id, day, [])
            return
        snaps = await self._moomoo_snapshots(client, [quote.symbol for quote in found])
        if not snaps:
            return
        volume_min = rule.condition.min_regular_volume
        ready: list[Quote] = []
        for quote in found:
            snap = snaps.get(quote.symbol)
            if snap is None or snap.volume is None:
                continue
            if volume_min is not None and snap.volume <= volume_min:
                continue
            quote.regular_volume = snap.volume
            quote.regular_change_pct = quote.change_pct
            quote.company = snap.company or quote.company
            ready.append(quote)
        if rule.condition.require_uptrend:
            await self._fill_session_uptrends(ready, rule, client, refresh=True)
        previous = {
            str(item["symbol"]): item for item in self.store.trend_watch(rule.id, day)
        }
        found_symbols = {quote.symbol for quote in found}
        rows: list[dict[str, Any]] = []
        kept: set[str] = set()
        for quote in ready:
            uptrend = quote.session_uptrend
            if rule.condition.require_uptrend and uptrend is False:
                continue
            if rule.condition.require_uptrend and uptrend is not True and quote.symbol not in previous:
                continue
            rows.append(
                {
                    "symbol": quote.symbol,
                    "change": quote.regular_change_pct,
                    "volume": quote.regular_volume,
                    "company": quote.company,
                }
            )
            kept.add(quote.symbol)
        for symbol, row in previous.items():
            if symbol in kept or symbol not in found_symbols or symbol in snaps:
                continue
            rows.append(row)
        self.store.replace_trend_watch(rule.id, day, rows)
        if rows:
            logger.info(
                "Trend watch %s: %s",
                rule.id,
                _symbols_text(item["symbol"] for item in rows),
            )

    async def _saved_afterhours_quotes(
        self, client: MoomooClient, symbols: list[str]
    ) -> dict[str, Quote] | None:
        if not symbols:
            return {}
        marks = await client.after_hours_quotes(symbols)
        await self._drain_client_problem(client)
        return marks

    async def _saved_premarket_quotes(
        self, client: MoomooClient, symbols: list[str]
    ) -> dict[str, Quote] | None:
        if not symbols:
            return {}
        marks = await client.premarket_quotes(symbols)
        await self._drain_client_problem(client)
        return marks

    async def _match_saved_day(self, rule: Rule) -> list[Quote]:
        client = self._trend_moomoo or self.moomoo
        floors = _day_gain_floors(rule)
        if client is None or not floors:
            return []
        kind = rule.condition.type
        afterhours = kind is ConditionType.AFTERHOURS_GAIN
        premarket = kind is ConditionType.PREMARKET_GAIN
        regular = kind is ConditionType.REGULAR_GAIN
        if not (afterhours or premarket or regular):
            return []
        # Multi-day windows skip today; single-day (day25) may use today in after-hours.
        days = recent_trading_days(
            now_et().date(),
            rule.condition.recent_day_count,
            include_end=_day_gain_include_today(rule, session_allows=afterhours),
        )
        floors_by_symbol: dict[str, list[float]] = {}
        for minimum in floors:
            for symbol in self.store.day_gain_symbols(minimum, days):
                floors_by_symbol.setdefault(symbol, []).append(minimum)
        symbols = sorted(floors_by_symbol)
        if afterhours:
            marks = await self._saved_afterhours_quotes(client, symbols)
        elif premarket:
            marks = await self._saved_premarket_quotes(client, symbols)
        else:
            marks = await self._moomoo_snapshots(client, symbols)
        if not marks:
            return []
        matched: list[Quote] = []
        for symbol in symbols:
            mark = marks.get(symbol)
            if mark is None:
                continue
            mark.recent_day_gain_floors = floors_by_symbol.get(symbol, [])
            mark.recent_day_spike = bool(mark.recent_day_gain_floors)
            scan_rule = _confirm_scan_rule(rule)
            if afterhours:
                evaluation = evaluate_afterhours_scan(
                    scan_rule, mark, market_cap=None, in_afterhours=True, cap_filtered=True
                )
            elif premarket:
                evaluation = evaluate_premarket_scan(
                    scan_rule, mark, market_cap=None, in_premarket=True, cap_filtered=True
                )
            else:
                mark.regular_change_pct = mark.change_pct
                evaluation = evaluate_regular_day_scan(
                    scan_rule, mark, market_cap=None, cap_filtered=True
                )
            if evaluation.matched:
                matched.append(mark)
        label = {
            ConditionType.AFTERHOURS_GAIN: "Saved after-hours",
            ConditionType.PREMARKET_GAIN: "Saved pre-market",
            ConditionType.REGULAR_GAIN: "Saved regular",
        }.get(kind, "Saved day-gain")
        self._log_saved_matches(rule.id, [quote.symbol for quote in matched], label)
        return matched

    async def _match_saved_trend(self, rule: Rule) -> list[Quote]:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        saved = self.store.trend_watch(rule.id, now_et().date().isoformat())
        marks = await self._saved_afterhours_quotes(
            client, [str(item["symbol"]) for item in saved]
        )
        if not marks:
            return []
        by_symbol = {str(item["symbol"]): item for item in saved}
        matched: list[Quote] = []
        for symbol, mark in marks.items():
            row = by_symbol.get(symbol, {})
            mark.regular_change_pct = row.get("change")
            volume = row.get("volume")
            mark.regular_volume = int(volume) if volume is not None else None
            mark.company = mark.company or row.get("company")
            mark.session_uptrend = True
            evaluation = evaluate_afterhours_scan(
                rule, mark, market_cap=None, in_afterhours=True, cap_filtered=True
            )
            if evaluation.matched:
                matched.append(mark)
        self._log_saved_matches(rule.id, [quote.symbol for quote in matched], "Saved trend")
        return matched

    def _log_saved_matches(self, rule_id: str, symbols: list[str], label: str) -> None:
        signature = tuple(sorted(symbols))
        now = datetime.now(timezone.utc)
        previous = self._scan_log.get(rule_id)
        if (
            previous is not None
            and previous[0] == signature
            and (now - previous[1]).total_seconds() < 60
        ):
            return
        logger.info("%s matches for %s: %s", label, rule_id, _symbols_text(symbols))
        self._scan_log[rule_id] = (signature, now)

    def _breaker_due(self) -> bool:
        now = datetime.now(timezone.utc)
        last = self._breaker_checked_at
        if last is not None and (now - last).total_seconds() < 60:
            return False
        self._breaker_checked_at = now
        return True

    def _note_opend_ok(self) -> None:
        self._opend_ok_at = datetime.now(timezone.utc)
        self._opend_down_notified = False

    async def _note_opend_failure(self, exc: BaseException) -> None:
        if not opend_is_down(exc) or self._opend_down_notified:
            return
        if self._opend_ok_at is not None:
            quiet = (datetime.now(timezone.utc) - self._opend_ok_at).total_seconds()
            if quiet < 15:
                return
        self._opend_down_notified = True
        text = f"OpenD 行情断开，扫描已暂停。{format_nz_clock()}"
        await self._send_system("OpenD 断开", text)

    async def _note_problem(self, text: str) -> None:
        if opend_is_down(RuntimeError(text)):
            await self._note_opend_failure(RuntimeError(text))
            return
        notice = problem_notice(text)
        if notice is None:
            return
        key, title, body = notice
        if not self._problems.should_send(key, datetime.now(timezone.utc)):
            return
        message = f"{body} {format_nz_clock()}"
        await self._send_system(title, message)

    async def _maybe_send_check_reminders(self, config: RulesConfig) -> None:
        due = due_check_reminders(extra=_important_reminders(config.important))
        if not due:
            return
        day = now_et().date().isoformat()
        sync_by_key = {
            item.key: item.sync_watchlist for item in config.important.reminders
        }
        pending: list[tuple[str, str, str, list[Rule], bool]] = []
        async with self._lock:
            state = self.store.load_state()
            sent = state.get("check_reminders")
            if not isinstance(sent, dict):
                sent = {}
            for reminder in due:
                stamp = f"{day}:{reminder.key}"
                if sent.get(reminder.key) == day or stamp in self._check_reminder_inflight:
                    continue
                ordered = []
                for rule_id in reminder.rule_ids:
                    match = next(
                        (rule for rule in config.rules if rule.enabled and rule.id == rule_id),
                        None,
                    )
                    if match is not None:
                        ordered.append(match)
                if not ordered:
                    continue
                self._check_reminder_inflight.add(stamp)
                pending.append(
                    (
                        stamp,
                        reminder.key,
                        reminder.label,
                        ordered,
                        bool(sync_by_key.get(reminder.key)),
                    )
                )
        for stamp, key, label, rules, sync_watchlist in pending:
            try:
                await self._deliver_check_reminder(label, [rule.name for rule in rules])
                if sync_watchlist:
                    await self._sync_important_reminder_watchlist(config, rules)
            except Exception as exc:  # noqa: BLE001
                self._check_reminder_inflight.discard(stamp)
                logger.warning("Could not send check reminder: %s", exc)
                continue
            async with self._lock:
                state = self.store.load_state()
                remembered = state.get("check_reminders")
                if not isinstance(remembered, dict):
                    remembered = {}
                remembered[key] = day
                state["check_reminders"] = {
                    key: value for key, value in remembered.items() if value == day
                }
                self.store.save_state(state)
            logger.info("Check reminder sent: %s", label)

    async def _sync_important_reminder_watchlist(
        self, config: RulesConfig, rules: list[Rule]
    ) -> None:
        if not self.ths.configured or not rules:
            return
        self.ths.set_important_group(config.important.ths_group)
        day = now_et().date().isoformat()
        session = self._ths_active_session() or "盘后"
        symbols: list[str] = []
        seen: set[str] = set()
        async with self._ths_lock:
            for rule in rules:
                try:
                    members = await self.ths.members(rule.name, day, [], session)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("同花顺 reminder sync failed for %s: %s", rule.name, exc)
                    continue
                for symbol in members:
                    if symbol not in seen:
                        seen.add(symbol)
                        symbols.append(symbol)
            if not symbols:
                return
            try:
                # Reminder sync replaces 我的自选 with current rule-group leaders.
                await self.ths.replace_important(symbols, session)
                logger.info(
                    "同花顺 %s reminder sync: %s",
                    config.important.ths_group,
                    _symbols_text(symbols),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("同花顺 important reminder sync failed: %s", exc)

    async def _deliver_check_reminder(self, label: str, names: list[str]) -> None:
        text = check_reminder_text(label, names)
        routes = getattr(self, "routes", None)
        notify = CHECK_REMINDER_NOTIFY
        slack_url = ""
        token = ""
        if routes is not None and routes.has_slack(notify):
            slack_url = routes.slack_url(notify)
        if routes is not None and routes.has_pushover(notify):
            token = routes.pushover_token(notify)
        if self.pushover is None and self.slack is None:
            return
        if not token and not slack_url:
            raise RuntimeError(f"{notify} has no Slack webhook or Pushover token")
        delivered = False
        errors: list[Exception] = []
        if self.pushover is not None and token:
            try:
                await self.pushover.send_text(
                    text,
                    title="重要提醒",
                    token=token,
                    priority=1,
                    sound="pushover",
                )
                delivered = True
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        if self.slack is not None and slack_url:
            try:
                await self.slack.send_text(f"重要提醒\n{text}", webhook_url=slack_url)
                delivered = True
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        for exc in errors:
            logger.warning("Check reminder delivery failed: %s", exc)
        if errors and not delivered:
            raise errors[0]

    async def _run_watched_symbols(self, config: RulesConfig) -> None:
        """Poll named tickers; high-priority alert + 我的自选 when session gain clears the floor."""
        watched = config.important.watched_symbols
        if not watched.symbols or self.moomoo is None:
            return
        if is_premarket():
            session = "盘前"
            quotes = await self.moomoo.premarket_quotes(watched.symbols)
        elif is_regular_session():
            session = "盘中"
            quotes = await self._moomoo_snapshots(self.moomoo, watched.symbols)
        elif is_afterhours():
            session = "盘后"
            quotes = await self.moomoo.after_hours_quotes(watched.symbols)
        else:
            return
        await self._drain_client_problem(self.moomoo)
        if quotes is None:
            return
        notify = watched.notify or CHECK_REMINDER_NOTIFY
        for symbol in watched.symbols:
            quote = quotes.get(symbol)
            if quote is None or quote.change_pct is None:
                continue
            if quote.change_pct < watched.min_change:
                continue
            cooldown_key = f"important-watched:{symbol}"
            async with self._lock:
                state = self.store.load_state()
                last = self.store.last_fired_at(state, cooldown_key)
                if last is not None:
                    age = (datetime.now(timezone.utc) - last).total_seconds()
                    if age < watched.cooldown_minutes * 60:
                        continue
                message = (
                    f"{quote.company or symbol}    {quote.change_pct:+.1f}%    "
                    f"${quote.price:.4g}"
                    if quote.price is not None
                    else f"{quote.company or symbol}    {quote.change_pct:+.1f}%"
                )
                message = f"{message}\n关注股票当前时段涨幅超 {watched.min_change:g}%"
                alert = self.store.new_alert(
                    rule_id="important-watched",
                    rule_name="关注股票",
                    label="重要提醒",
                    notify=notify,
                    symbol=symbol,
                    message=message,
                    price=quote.price,
                    change_pct=quote.change_pct,
                )
                alert.url = quote_url(symbol)
                await self._deliver(alert, priority=1, sound="pushover")
                self.store.append_alert(alert)
                self.store.mark_fired(state, cooldown_key, alert.fired_at)
                self.store.save_state(state)
            logger.info("ALERT important-watched | %s", alert.message.replace("\n", " | "))
            if self.ths.configured:
                self.ths.set_important_group(config.important.ths_group)
                task = asyncio.create_task(
                    self._watched_symbol_tonghuashun(symbol, session, config)
                )
                self._ths_tasks.add(task)
                task.add_done_callback(self._ths_tasks.discard)

    async def _watched_symbol_tonghuashun(
        self, symbol: str, session: str, config: RulesConfig
    ) -> None:
        async with self._ths_lock:
            try:
                await self.ths.add_important([symbol], session)
            except Exception as exc:  # noqa: BLE001
                logger.warning("同花顺 watched symbol failed for %s: %s", symbol, exc)

    async def _send_system(self, title: str, message: str) -> None:
        routes = getattr(self, "routes", None)
        token = routes.pushover_token("opend") if routes is not None else None
        slack_url = routes.slack_url("opend") if routes is not None else ""
        delivered = False
        if self.pushover is not None and (token or routes is None):
            try:
                # ttl=0: do not expire with the session; priority=1 so it is hard to miss.
                await self.pushover.send_text(
                    message,
                    title=title,
                    token=token,
                    priority=1,
                    sound="pushover",
                    ttl=0,
                )
                delivered = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not send system alert: %s", exc)
        elif self.pushover is None and title == "OpenD 断开":
            logger.warning("OpenD disconnected")
        if getattr(self, "slack", None) is not None and slack_url:
            try:
                await self.slack.send_text(f"{title}\n{message}", webhook_url=slack_url)
                delivered = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not send system alert to Slack: %s", exc)
        if delivered:
            logger.info("System alert sent: %s", title)
        elif self.pushover is not None or getattr(self, "slack", None) is not None:
            logger.warning("System alert not delivered: %s", title)

    async def _moomoo_snapshots(self, client: MoomooClient, symbols: list[str]) -> dict[str, Quote]:
        quotes = await client.snapshots(symbols)
        await self._drain_client_problem(client)
        return quotes

    async def _drain_client_problem(self, client: MoomooClient) -> None:
        message = client.take_problem()
        if message:
            await self._note_problem(message)

    async def _run_stream_loop(self) -> None:
        self._running = True
        logger.info("Monitor loop started (WebSocket)")
        await self._bootstrap()
        scan_task = asyncio.create_task(self._rest_scan_loop())
        delay = 1
        try:
            while self._running:
                config = self.load_config(force=True)
                feed = config.scanner_feed or config.feed or self.settings.alpaca_feed
                stream = AlpacaStream(self.settings, feed)
                watchlist = config.all_symbols()
                want_all = config.stream_all_trades and not self._watchlist_only
                trades = ["*"] if want_all else watchlist
                try:
                    await stream.listen(
                        trades=trades,
                        quotes=watchlist,
                        on_events=self._handle_events,
                        should_continue=lambda: self._running,
                    )
                    delay = 1
                    self._stream_all_trades = stream.subscribed_all_trades
                except Exception as exc:  # noqa: BLE001
                    message = str(exc).lower()
                    if "symbol limit" in message and not self._watchlist_only:
                        logger.warning(
                            "Stream rejected all-symbol subscribe (%s); "
                            "watchlist socket + REST pre-market scan",
                            exc,
                        )
                        self._watchlist_only = True
                        delay = 1
                        continue
                    if "connection limit" in message:
                        logger.warning("Alpaca allows one IEX stream; retrying in %ss", max(delay, 5))
                        delay = max(delay, 5)
                    self.last_error = str(exc)
                    logger.warning("Stream disconnected: %s", exc)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
        finally:
            scan_task.cancel()
            await asyncio.gather(scan_task, return_exceptions=True)

    async def _bootstrap(self) -> None:
        config = self.load_config(force=True)
        try:
            self.market = await self.alpaca.clock()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch market clock: %s", exc)
            await self._note_problem(str(exc))
        self._log_market()
        symbols = config.all_symbols()
        if symbols:
            quotes = await self.alpaca.snapshots(symbols, config.feed or self.settings.alpaca_feed)
            self.quotes.update(quotes)
            self._log_quotes(quotes)

    async def _load_prev_closes(self, config: RulesConfig) -> None:
        try:
            symbols = await self._universe.symbols(self.alpaca)
            logger.info("Loading previous closes for %s symbols", len(symbols))
            quotes = await self.alpaca.snapshots_many(
                symbols, config.scanner_feed or config.feed or self.settings.alpaca_feed
            )
            for symbol, quote in quotes.items():
                current = self.quotes.get(symbol)
                if current is None:
                    self.quotes[symbol] = quote
                    continue
                if quote.prev_close is not None:
                    current.prev_close = quote.prev_close
                    if current.price is not None:
                        current.change_pct = (
                            (current.price - current.prev_close) / current.prev_close
                        ) * 100
            logger.info("Previous closes ready for %s symbols", len(self.quotes))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not preload previous closes: %s", exc)

    async def _rest_scan_loop(self) -> None:
        await asyncio.sleep(20)
        while self._running:
            try:
                config = self.load_config()
                if self._stream_all_trades:
                    await asyncio.sleep(config.scanner_interval_seconds)
                    continue
                if self._should_scan(config):
                    async with self._lock:
                        state = self.store.load_state()
                        for rule in config.rules:
                            if rule.enabled and rule.is_scanner:
                                await self._run_scanner(rule, config, state)
                        self.store.save_state(state)
                        self._last_scan_at = datetime.now(timezone.utc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("REST pre-market scan failed: %s", exc)
            await asyncio.sleep(15)

    async def _handle_events(self, events: list[StreamEvent]) -> None:
        async with self._lock:
            state = self.store.load_state()
            changed = False
            for event in events:
                if await self._handle_event(event, state):
                    changed = True
            if changed:
                self.store.save_state(state)
            self.last_poll_at = datetime.now(timezone.utc)

    async def _handle_event(self, event: StreamEvent, state: dict) -> bool:
        if event.kind == "trade" and event.price is not None:
            quote = apply_trade(self.quotes.get(event.symbol), event.symbol, event.price, event.at)
            self.quotes[event.symbol] = quote
            self._trade_count += 1
            if self._trade_count <= 5 or self._trade_count % 500 == 0:
                logger.info(
                    "WS trade #%s %s $%s",
                    self._trade_count,
                    event.symbol,
                    f"{event.price:.2f}",
                )
        elif event.kind == "quote":
            quote = apply_quote(
                self.quotes.get(event.symbol), event.symbol, event.bid, event.ask, event.at
            )
            self.quotes[event.symbol] = quote
        else:
            return False

        if quote.prev_close is None:
            await self._fill_prev_close(quote)

        config = self.load_config()
        fired = False
        for rule in config.rules:
            if not rule.enabled:
                continue
            if rule.is_scanner:
                if await self._evaluate_scanner_tick(rule, quote, state):
                    fired = True
                continue
            if rule.symbol != quote.symbol:
                continue
            evaluation = evaluate_rule(rule, quote, previous_price(quote))
            if self.verbose:
                logger.info("  %s: %s", rule.id, evaluation.reason)
            if not evaluation.matched:
                continue
            alert = await self._fire_alert(rule, quote, state, extra=None)
            if alert is not None:
                fired = True
        return fired

    async def _fill_prev_close(self, quote: Quote) -> None:
        config = self.load_config()
        try:
            snapshots = await self.alpaca.snapshots(
                [quote.symbol], config.feed or self.settings.alpaca_feed
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch previous close for %s: %s", quote.symbol, exc)
            return
        fetched = snapshots.get(quote.symbol)
        if fetched and fetched.prev_close:
            quote.prev_close = fetched.prev_close
            if quote.price is not None:
                quote.change_pct = ((quote.price - quote.prev_close) / quote.prev_close) * 100

    async def _evaluate_scanner_tick(self, rule: Rule, quote: Quote, state: dict) -> bool:
        if not is_premarket() or not is_trading_day(self.market):
            return False
        price, change, _stamped = premarket_mark(quote)
        if price is None or change is None or change < rule.condition.value:
            return False
        quote.price = price
        quote.change_pct = change
        cap = await self._total_cap(quote)
        evaluation = evaluate_premarket_scan(
            rule, quote, market_cap=cap, in_premarket=True
        )
        if self.verbose or evaluation.matched:
            logger.info("  %s %s: %s", rule.id, quote.symbol, evaluation.reason)
        if not evaluation.matched or cap is None:
            return False
        extra = {"market_cap": cap, "market_cap_m": cap / 1_000_000}
        alert = await self._fire_alert(
            rule,
            quote,
            state,
            extra=extra,
            cooldown_key=_alert_cooldown_key(rule, quote.symbol),
        )
        return alert is not None

    async def _total_cap(self, quote: Quote) -> float | None:
        cached = self._cap_cache.get(quote.symbol)
        if cached is not None:
            return cached
        fundamentals = await self._marketcap.fundamentals([quote.symbol])
        cap = fundamentals.get(quote.symbol, {}).get("market_cap")
        if cap is not None:
            self._cap_cache[quote.symbol] = cap
        return cap

    def stop(self) -> None:
        self._running = False

    def _rule_session_open(self, rule: Rule) -> bool:
        if not is_trading_day(self.market):
            return False
        kind = rule.condition.type
        if kind is ConditionType.AFTERHOURS_GAIN:
            return is_afterhours()
        if kind is ConditionType.PREMARKET_GAIN:
            return is_premarket()
        if kind in {ConditionType.REGULAR_GAIN, ConditionType.REGULAR_CLOSE}:
            return is_regular_session()
        if kind is ConditionType.SUDDEN_GAIN:
            late = rule.condition.late_session
            if late == "afterhours":
                return is_afterhours()
            if late == "premarket":
                return is_premarket()
            return is_regular_session()
        if kind is ConditionType.LATE_SESSION_GAIN:
            late = rule.condition.late_session
            if late == "afterhours":
                return is_afterhours()
            if late == "premarket":
                return is_premarket()
            return is_regular_session()
        return False

    def _should_scan(self, config: RulesConfig) -> bool:
        if not any(
            rule.enabled and rule.is_scanner and self._rule_session_open(rule) for rule in config.rules
        ):
            return False
        if self._last_scan_at is None:
            return True
        elapsed = (datetime.now(timezone.utc) - self._last_scan_at).total_seconds()
        return elapsed >= config.scanner_interval_seconds

    async def _run_scanner(
        self,
        rule: Rule,
        config: RulesConfig,
        state: dict,
    ) -> tuple[list[Alert], str | None]:
        if rule.condition.type is ConditionType.AFTERHOURS_GAIN and self.moomoo is None:
            logger.warning("After-hours scan %s needs Moomoo", rule.id)
            return [], None
        if self.moomoo is not None:
            return await self._run_moomoo_scanner(rule, state)
        symbols = await self._universe.symbols(self.alpaca)
        logger.info("Premarket scan %s over %s symbols", rule.id, len(symbols))
        quotes = await self.alpaca.snapshots_many(
            symbols, config.scanner_feed or config.feed or self.settings.alpaca_feed
        )
        raw_gainers = 0
        live_prints = 0
        gainers: list[Quote] = []
        for quote in quotes.values():
            if quote.change_pct is not None and quote.change_pct >= rule.condition.value:
                raw_gainers += 1
            price, change, _stamped = premarket_mark(quote)
            if price is None or change is None:
                continue
            live_prints += 1
            if change < rule.condition.value:
                continue
            quote.price = price
            quote.change_pct = change
            gainers.append(quote)
        gainers.sort(key=lambda item: item.change_pct or 0, reverse=True)
        logger.info(
            "Premarket prints: %s | raw +%.0f%% names: %s | pre-market +%.0f%%: %s",
            live_prints,
            rule.condition.value,
            raw_gainers,
            rule.condition.value,
            len(gainers),
        )

        fundamentals = await self._marketcap.fundamentals([quote.symbol for quote in gainers])
        matched: list[tuple[Quote, float | None]] = []
        for quote in gainers:
            info = fundamentals.get(quote.symbol, {})
            cap = info.get("market_cap")
            evaluation = evaluate_premarket_scan(
                rule, quote, market_cap=cap, in_premarket=True
            )
            if self.verbose:
                logger.info("  %s %s: %s", rule.id, quote.symbol, evaluation.reason)
            if evaluation.matched and cap is not None:
                matched.append((quote, cap))

        logger.info(
            "Premarket matches after market-cap filter for %s: %s",
            rule.id,
            _symbols_text(quote.symbol for quote, _cap in matched),
        )
        fired: list[Alert] = []
        slack_error: str | None = None
        for quote, cap in matched:
            extra = {"market_cap": cap, "market_cap_m": cap / 1_000_000}
            alert = await self._fire_alert(
                rule,
                quote,
                state,
                extra=extra,
                cooldown_key=_alert_cooldown_key(rule, quote.symbol),
            )
            if alert is None:
                continue
            fired.append(alert)

        return fired, slack_error

    async def _run_afterhours_rules(
        self,
        rules: list[Rule],
        state: dict,
    ) -> tuple[list[Alert], str | None]:
        if self.moomoo is None or not rules:
            return [], None
        caps = {rule.condition.max_market_cap or 100_000_000 for rule in rules}
        if len(caps) > 1:
            fired: list[Alert] = []
            slack_error: str | None = None
            for rule in rules:
                scanned, scan_error = await self._run_moomoo_scanner(rule, state)
                fired.extend(scanned)
                slack_error = slack_error or scan_error
            return fired, slack_error
        mins = {
            rule.condition.min_market_cap
            for rule in rules
            if rule.condition.min_market_cap is not None
        }
        min_cap = min(mins) if mins else None
        found = await self.moomoo.scan_afterhours(
            min(rule.condition.value for rule in rules),
            caps.pop(),
            min_market_cap=min_cap,
        )
        fired = []
        slack_error = None
        for rule in rules:
            scan_rule = _afterhours_scan_rule(rule)
            matched: list[tuple[Quote, float | None]] = []
            for quote, cap in found:
                evaluation = evaluate_afterhours_scan(
                    scan_rule, quote, market_cap=cap, in_afterhours=True, cap_filtered=True
                )
                if self.verbose:
                    logger.info("  %s %s: %s", rule.id, quote.symbol, evaluation.reason)
                if evaluation.matched:
                    matched.append((quote, cap))
            signature = tuple(sorted(quote.symbol for quote, _cap in matched))
            now = datetime.now(timezone.utc)
            previous = self._scan_log.get(rule.id)
            if (
                previous is None
                or previous[0] != signature
                or (now - previous[1]).total_seconds() >= 60
            ):
                logger.info(
                    "After-hours matches for %s: %s",
                    rule.id,
                    _symbols_text(quote.symbol for quote, _cap in matched),
                )
                self._scan_log[rule.id] = (signature, now)
            for quote, cap in matched:
                extra = None
                if cap is not None:
                    extra = {"market_cap": cap, "market_cap_m": cap / 1_000_000}
                alert = await self._fire_alert(
                    rule,
                    quote,
                    state,
                    extra=extra,
                    cooldown_key=_alert_cooldown_key(rule, quote.symbol),
                )
                if alert is not None:
                    fired.append(alert)
        return fired, slack_error

    def _stable_capture_due(self) -> bool:
        if not regular_close_approach():
            return False
        now = datetime.now(timezone.utc)
        last = self._stable_captured_at
        if last is not None and (now - last).total_seconds() < 60:
            return False
        self._stable_captured_at = now
        return True

    def _stable_freeze_due(self) -> bool:
        if not afterhours_just_opened():
            return False
        return self._stable_frozen_day != now_et().date().isoformat()

    async def _capture_stable_names(self, rule: Rule, client: MoomooClient) -> None:
        minimum = rule.condition.min_regular_change
        if minimum is None:
            return
        found = await client.regular_gainers(
            minimum,
            max_market_cap=rule.condition.max_market_cap,
            min_market_cap=rule.condition.min_market_cap,
        )
        pool: dict[str, Quote] = {}
        for quote in found:
            quote.regular_change_pct = quote.change_pct
            quote.change_pct = None
            pool[quote.symbol] = quote
        self._stable_pool[rule.id] = pool
        self._stable_ready_day[rule.id] = now_et().date().isoformat()
        logger.info(
            "%s regular names above %.0f%%: %s",
            rule.id,
            minimum,
            _symbols_text(pool),
        )

    async def _stable_rank_fallback(self, rule: Rule) -> list[Quote]:
        if self.moomoo is None or rule.condition.stable_low is None:
            return []
        found = await self.moomoo.scan_afterhours(
            rule.condition.stable_low,
            rule.condition.max_market_cap,
            max_pages=8,
            min_market_cap=rule.condition.min_market_cap,
        )
        minimum = rule.condition.min_regular_change
        quotes: list[Quote] = []
        for quote, _cap in found:
            if minimum is not None and (
                quote.regular_change_pct is None or quote.regular_change_pct <= minimum
            ):
                continue
            quotes.append(quote)
        logger.info(
            "%s stable fallback from after-hours rank: %s",
            rule.id,
            _symbols_text(quote.symbol for quote in quotes),
        )
        return quotes

    async def _match_stable_afterhours(self, rule: Rule) -> list[Quote]:
        minutes = rule.condition.stable_minutes
        if minutes is None or self.moomoo is None:
            return []
        day = now_et().date().isoformat()
        key = f"{day}:{rule.id}"
        if key in self._stable_done:
            return []
        if not afterhours_stable_due(now_et(), minutes):
            if afterhours_stable_missed(now_et(), minutes):
                self._stable_done.add(key)
            return []
        ready = self._stable_ready_day.get(rule.id) == day
        if ready:
            quotes = list(self._stable_pool.get(rule.id, {}).values())
        else:
            quotes = await self._stable_rank_fallback(rule)
        if not quotes:
            logger.info("%s stable window: no regular-session names above the minimum", rule.id)
            self._stable_done.add(key)
            return []
        marks = await self.moomoo.after_hours_quotes([quote.symbol for quote in quotes])
        await self._drain_client_problem(self.moomoo)
        if marks is None:
            logger.info("%s stable window: snapshot paused, will retry", rule.id)
            return []
        matched: list[Quote] = []
        for quote in quotes:
            mark = marks.get(quote.symbol)
            if mark is None:
                logger.info("  %s %s: no after-hours range", rule.id, quote.symbol)
                continue
            merged = quote.model_copy(
                update={
                    "price": mark.price,
                    "change_pct": mark.change_pct,
                    "volume": mark.volume,
                    "session_low_pct": mark.session_low_pct,
                    "session_high_pct": mark.session_high_pct,
                    "company": mark.company or quote.company,
                }
            )
            evaluation = evaluate_stable_afterhours(rule, merged, cap_filtered=True)
            logger.info("  %s %s: %s", rule.id, quote.symbol, evaluation.reason)
            if evaluation.matched:
                matched.append(merged)
        self._stable_done.add(key)
        logger.info(
            "After-hours stable matches for %s: %s",
            rule.id,
            _symbols_text(quote.symbol for quote in matched),
        )
        return matched

    async def _match_new_high_rule(self, rule: Rule) -> list[Quote]:
        """Alert when gain makes a new high after first clearing `value`, waiting `lag` minutes."""
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        max_cap = rule.condition.max_market_cap or 100_000_000
        kind = rule.condition.type
        if kind is ConditionType.AFTERHOURS_GAIN:
            found_rows = await client.scan_afterhours(rule.condition.value, max_cap, min_market_cap=rule.condition.min_market_cap)
            found = [quote for quote, _cap in found_rows]
        elif kind is ConditionType.PREMARKET_GAIN:
            found_rows = await client.scan_premarket(rule.condition.value, max_cap, min_market_cap=rule.condition.min_market_cap)
            found = [quote for quote, _cap in found_rows]
        elif kind is ConditionType.REGULAR_GAIN:
            found = await client.regular_gainers(
                rule.condition.value,
                max_market_cap=max_cap,
                min_volume=rule.condition.min_volume,
                min_market_cap=rule.condition.min_market_cap,
            )
        else:
            return []
        volume_min = rule.condition.min_volume
        lag = timedelta(minutes=rule.condition.prior_change_lag_minutes)
        day = now_et().date().isoformat()
        now = datetime.now(timezone.utc)
        matched: list[Quote] = []
        for quote in found:
            if quote.change_pct is None or quote.change_pct < rule.condition.value:
                continue
            if volume_min is not None and (quote.volume is None or quote.volume <= volume_min):
                continue
            watched = self.store.new_high_watch(day, rule.id, quote.symbol)
            if watched is None:
                self.store.note_new_high_watch(
                    day, rule.id, quote.symbol, quote.change_pct, now
                )
                logger.info(
                    "New high watch %s: %s %+.1f%%",
                    rule.id,
                    quote.symbol,
                    quote.change_pct,
                )
                continue
            first_at, peak = watched
            if quote.change_pct <= peak:
                continue
            prior = self.store.raise_new_high_peak(
                day, rule.id, quote.symbol, quote.change_pct
            )
            if prior is None:
                continue
            if now - first_at < lag:
                continue
            matched.append(quote)
        self._log_saved_matches(
            rule.id,
            [quote.symbol for quote in matched],
            f"New high after {rule.condition.value:g}%",
        )
        return matched

    async def _match_peak_fade_rule(self, rule: Rule) -> list[Quote]:
        """Session once peaked >= value and has since faded by min_fade_from_high pp."""
        client = self._trend_moomoo or self.moomoo
        fade_min = rule.condition.min_fade_from_high
        if client is None or fade_min is None:
            return []
        kind = rule.condition.type
        day = now_et().date().isoformat()
        if kind is ConditionType.AFTERHOURS_GAIN:
            label = "After-hours peak fade"
            found_rows = await client.scan_afterhours(
                rule.condition.value,
                rule.condition.max_market_cap,
                min_market_cap=rule.condition.min_market_cap,
            )
            found = [quote for quote, _cap in found_rows]
            note_peak = self.store.note_afterhours_peak
            peaks = self.store.afterhours_peaks
            load_marks = self._saved_afterhours_quotes
        elif kind is ConditionType.PREMARKET_GAIN:
            label = "Pre-market peak fade"
            found_rows = await client.scan_premarket(
                rule.condition.value,
                rule.condition.max_market_cap,
                min_market_cap=rule.condition.min_market_cap,
            )
            found = [quote for quote, _cap in found_rows]
            note_peak = self.store.note_premarket_peak
            peaks = self.store.premarket_peaks
            load_marks = self._saved_premarket_quotes
        elif kind is ConditionType.REGULAR_GAIN:
            label = "Regular peak fade"
            found = await client.regular_gainers(
                rule.condition.value,
                max_market_cap=rule.condition.max_market_cap,
                min_market_cap=rule.condition.min_market_cap,
                min_volume=rule.condition.min_volume,
            )
            note_peak = self.store.note_peak_gain
            peaks = self.store.peak_gains

            async def load_marks(_client: MoomooClient, symbols: list[str]):
                return await self._moomoo_snapshots(_client, symbols)
        else:
            return []
        for quote in found:
            observed = quote.change_pct
            if quote.session_high_pct is not None:
                observed = (
                    quote.session_high_pct
                    if observed is None
                    else max(observed, quote.session_high_pct)
                )
            if observed is not None:
                note_peak(day, quote.symbol, observed)
        peak_map = peaks(day)
        pool = sorted(
            symbol for symbol, peak in peak_map.items() if peak >= rule.condition.value
        )
        if not pool:
            self._log_saved_matches(rule.id, [], label)
            return []
        marks = await load_marks(client, pool)
        if not marks:
            return []
        scan_rule = _afterhours_scan_rule(rule) if kind is ConditionType.AFTERHOURS_GAIN else rule
        matched: list[Quote] = []
        for symbol in pool:
            quote = marks.get(symbol)
            if quote is None:
                continue
            peak = peak_map.get(symbol.upper())
            if quote.session_high_pct is not None:
                peak = (
                    quote.session_high_pct
                    if peak is None
                    else max(peak, quote.session_high_pct)
                )
            if quote.change_pct is not None:
                peak = (
                    quote.change_pct if peak is None else max(peak, quote.change_pct)
                )
            if peak is None:
                continue
            peak = note_peak(day, symbol, peak)
            quote.session_high_pct = peak
            if kind is ConditionType.AFTERHOURS_GAIN:
                evaluation = evaluate_afterhours_scan(
                    scan_rule,
                    quote,
                    market_cap=None,
                    in_afterhours=True,
                    cap_filtered=(rule.condition.max_market_cap is not None or rule.condition.min_market_cap is not None),
                )
            elif kind is ConditionType.PREMARKET_GAIN:
                evaluation = evaluate_premarket_scan(
                    scan_rule,
                    quote,
                    market_cap=None,
                    in_premarket=True,
                    cap_filtered=(rule.condition.max_market_cap is not None or rule.condition.min_market_cap is not None),
                )
            else:
                evaluation = evaluate_regular_peak_fade(
                    scan_rule,
                    quote,
                    market_cap=None,
                    cap_filtered=(rule.condition.max_market_cap is not None or rule.condition.min_market_cap is not None),
                )
            if self.verbose:
                logger.info("  %s %s: %s", rule.id, quote.symbol, evaluation.reason)
            if evaluation.matched:
                matched.append(quote)
        matched.sort(
            key=lambda quote: peak_fade_score(quote.session_high_pct, quote.change_pct),
            reverse=True,
        )
        self._log_saved_matches(
            rule.id, [quote.symbol for quote in matched], label
        )
        return matched

    async def _match_spike_rule(self, rule: Rule) -> list[Quote]:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        afterhours = rule.condition.type is ConditionType.AFTERHOURS_GAIN
        max_cap = rule.condition.max_market_cap or 100_000_000
        if afterhours:
            found = await client.scan_afterhours(rule.condition.value, max_cap, min_market_cap=rule.condition.min_market_cap)
        else:
            found = await client.scan_premarket(rule.condition.value, max_cap, min_market_cap=rule.condition.min_market_cap)
        candidates: list[Quote] = []
        volume_min = rule.condition.min_volume
        for quote, _cap in found:
            if quote.change_pct is None or quote.change_pct < rule.condition.value:
                continue
            if volume_min is not None and (quote.volume is None or quote.volume <= volume_min):
                continue
            candidates.append(quote)
        session = "afterhours" if afterhours else "premarket"
        self._mark_observed_spikes(found, rule, session)
        self._apply_observed_spikes(candidates, rule, session)
        matched: list[Quote] = []
        for quote in candidates:
            if afterhours:
                evaluation = evaluate_afterhours_scan(
                    rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
                )
            else:
                evaluation = evaluate_premarket_scan(
                    rule, quote, market_cap=None, in_premarket=True, cap_filtered=True
                )
            if self.verbose:
                logger.info("  %s %s: %s", rule.id, quote.symbol, evaluation.reason)
            if evaluation.matched:
                matched.append(quote)
        signature = tuple(sorted(quote.symbol for quote in matched))
        now = datetime.now(timezone.utc)
        previous = self._scan_log.get(rule.id)
        if (
            previous is None
            or previous[0] != signature
            or (now - previous[1]).total_seconds() >= 60
        ):
            session_name = "After-hours" if afterhours else "Pre-market"
            logger.info(
                "%s pullback matches for %s: %s",
                session_name,
                rule.id,
                _symbols_text(quote.symbol for quote in matched),
            )
            self._scan_log[rule.id] = (signature, now)
        return matched

    async def _run_late_rules(self, rules: list[Rule]) -> None:
        for rule in rules:
            phase = last_hour_phase(
                hours=rule.condition.late_session_hours,
                session=rule.condition.late_session,
            )
            if phase is None:
                continue
            matched = await self._match_last_hour(rule, phase)
            self._note_opend_ok()
            if phase != "alert" or not matched:
                continue
            async with self._lock:
                state = self.store.load_state()
                await self._emit_trend_matches(rule, matched, state)
                self.store.save_state(state)

    async def _match_last_hour(self, rule: Rule, phase: str) -> list[Quote]:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        day = now_et().date().isoformat()
        if self._late_day != day:
            self._late_day = day
            self._late_baselines.clear()
            self._late_begin.clear()
            self._late_phase.clear()
        if self._late_phase.get(rule.id) != phase:
            self._late_begin[rule.id] = 0
            self._late_phase[rule.id] = phase
        session = rule.condition.late_session or "regular"
        max_cap = rule.condition.max_market_cap or 100_000_000
        min_cap = rule.condition.min_market_cap
        min_volume = rule.condition.min_volume if phase == "alert" and session == "regular" else None
        min_price = rule.condition.min_price
        begin = self._late_begin.get(rule.id, 0)
        if begin == 0:
            volume = "" if rule.condition.min_volume is None else f", volume > {rule.condition.min_volume:,.0f}"
            price = "" if min_price is None else f", price > {min_price:g}"
            logger.info(
                "Last %sh %s %s sweep, cap <= %.0f%s%s",
                rule.condition.late_session_hours,
                session,
                phase,
                max_cap,
                volume,
                price,
            )
        quotes, next_begin = await client.smallcap_page(
            begin, max_cap, min_volume, min_price, min_market_cap=min_cap
        )
        if session != "regular":
            marks = await client.session_marks([quote.symbol for quote in quotes], session)
            await self._drain_client_problem(client)
            if marks is None:
                logger.info("Late-session snapshot paused for %s", rule.id)
                return []
            kept: list[Quote] = []
            for quote in quotes:
                mark = marks.get(quote.symbol)
                if mark is None:
                    continue
                quote.price, quote.volume = mark
                kept.append(quote)
            quotes = kept
        self._late_begin[rule.id] = next_begin
        if quotes and all(quote.price is None for quote in quotes):
            logger.warning("Last-hour page returned no prices")
        if phase == "alert" and quotes and all(quote.volume is None for quote in quotes):
            logger.warning("Last-hour page returned no volume")
        baselines = self._late_baselines.setdefault(rule.id, {})
        matched: list[Quote] = []
        for quote in quotes:
            price = quote.price
            if price is None or price <= 0:
                continue
            if min_price is not None and price <= min_price:
                continue
            if phase == "baseline" or quote.symbol not in baselines:
                baselines[quote.symbol] = price
                continue
            change = hour_change_pct(baselines[quote.symbol], price)
            quote.change_pct = change
            evaluation = evaluate_last_hour_scan(
                rule, quote, in_last_hour=True, market_cap=None, cap_filtered=True
            )
            if self.verbose:
                logger.info("  %s %s: %s", rule.id, quote.symbol, evaluation.reason)
            if evaluation.matched:
                matched.append(quote)
        if matched:
            logger.info(
                "Last-hour matches for %s: %s",
                rule.id,
                ", ".join(f"{quote.symbol} {quote.change_pct:+.1f}%" for quote in matched),
            )
        return matched

    async def _run_sudden_rules(self, rules: list[Rule]) -> None:
        for rule in rules:
            hours = rule.condition.late_session_hours or 2
            if not session_late_window(rule.condition.late_session, hours=hours):
                continue
            matched = await self._match_sudden_rule(rule)
            self._note_opend_ok()
            async with self._lock:
                state = self.store.load_state()
                if matched:
                    await self._emit_sudden_first_alerts(rule, matched, state)
                await self._emit_sudden_hold_alerts(rule, state)
                self.store.save_state(state)

    @staticmethod
    def _sudden_confirm_key(rule: Rule, symbol: str) -> str:
        """Arm key scoped to rule id so 盘前/盘中/盘后 sudden do not share holds."""
        return f"confirm:{rule.id}:{symbol.strip().upper()}"

    def _clear_legacy_sudden_confirm(self, state: dict, rule: Rule) -> None:
        """Drop old shared confirm:Sudden-10:* arms that caused cross-session hold floods."""
        notify = (rule.notify or rule.id).strip() or rule.id
        legacy = f"confirm:{notify}:"
        armed = state.get("confirm_armed", {})
        for key in list(armed):
            if key.startswith(legacy):
                armed.pop(key, None)

    async def _emit_sudden_first_alerts(
        self, rule: Rule, quotes: list[Quote], state: dict
    ) -> None:
        """First 1-minute spike: alert + rule group, arm confirm, skip 我的自选/cooldown."""
        self._clear_legacy_sudden_confirm(state, rule)
        for quote in quotes:
            arm_key = self._sudden_confirm_key(rule, quote.symbol)
            if self.store.confirm_armed_at(state, arm_key) is not None:
                continue
            cooldown_key = _alert_cooldown_key(rule, quote.symbol)
            if self.store.in_cooldown(state, cooldown_key, rule.cooldown_minutes):
                continue
            self.store.note_confirm_arm(state, arm_key)
            logger.info(
                "Rule %s first spike for %s — alert + group + 我的自选",
                rule.id,
                quote.symbol,
            )
            await self._fire_alert(
                rule,
                quote,
                state,
                extra=None,
                cooldown_key=cooldown_key,
                apply_cooldown=False,
                skip_confirm=True,
            )

    async def _emit_sudden_hold_alerts(self, rule: Rule, state: dict) -> None:
        """After confirm_delay, remind if session gain is still >= confirm_value → 我的自选."""
        if rule.confirm_delay_minutes <= 0 or rule.confirm_value is None:
            return
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return
        self._clear_legacy_sudden_confirm(state, rule)
        prefix = f"confirm:{rule.id}:"
        hours = rule.condition.late_session_hours or 2
        session = rule.condition.late_session or "regular"
        now = store_mod._now()
        today = now_et(now).date()
        ready: list[str] = []
        armed = state.setdefault("confirm_armed", {})
        for key in list(armed):
            if not key.startswith(prefix):
                continue
            armed_at = self.store.confirm_armed_at(state, key)
            if armed_at is None:
                armed.pop(key, None)
                continue
            # Only holds armed today during this session's late window.
            if armed_at.tzinfo is None:
                armed_at = armed_at.replace(tzinfo=timezone.utc)
            if now_et(armed_at).date() != today or not session_late_window(
                session, moment=armed_at, hours=hours
            ):
                armed.pop(key, None)
                continue
            if (now - armed_at).total_seconds() < rule.confirm_delay_minutes * 60:
                continue
            ready.append(key[len(prefix) :])
        if not ready:
            return
        quotes = await self._sudden_session_quotes(client, ready, session)
        if not quotes:
            return
        floor = rule.confirm_value
        min_price = rule.condition.min_price
        for symbol in ready:
            arm_key = self._sudden_confirm_key(rule, symbol)
            quote = quotes.get(symbol)
            if quote is None or quote.change_pct is None:
                continue
            if quote.change_pct < floor:
                # Spike faded before hold — allow a later spike to re-arm.
                armed.pop(arm_key, None)
                continue
            if min_price is not None and (quote.price is None or quote.price <= min_price):
                continue
            await self._fire_alert(
                rule,
                quote,
                state,
                extra=None,
                cooldown_key=_alert_cooldown_key(rule, symbol),
                skip_confirm=True,
            )
            # Hold is one-shot for this arm.
            armed.pop(arm_key, None)

    async def _match_sudden_rule(self, rule: Rule) -> list[Quote]:
        """Watch discovered names; alert on ~1-minute spikes (no session-gain floor)."""
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        session = rule.condition.late_session or "regular"
        day = now_et().date().isoformat()
        if self._sudden_day != day:
            self._sudden_day = day
            self._sudden_pool.clear()
            self._sudden_prices.clear()
            self._sudden_session.clear()
            self._sudden_discovered_at.clear()
        await self._discover_sudden_pool(rule, client, session)
        pool = self._sudden_pool.get(rule.id) or set()
        if not pool:
            return []
        quotes = await self._sudden_session_quotes(client, sorted(pool), session)
        if quotes is None:
            return []
        now = datetime.now(timezone.utc).timestamp()
        window = rule.condition.sudden_window_seconds
        keep = max(window * 2, 120)
        prices = self._sudden_prices.setdefault(rule.id, {})
        matched: list[Quote] = []
        still: set[str] = set()
        for symbol in pool:
            quote = quotes.get(symbol)
            if quote is None:
                # Missing snapshot this round — keep watching; don't wipe samples.
                still.add(symbol)
                continue
            price = quote.price
            session_change = quote.change_pct
            if price is None or price <= 0:
                still.add(symbol)
                continue
            still.add(symbol)
            if session_change is not None:
                self._sudden_session[f"{rule.id}:{symbol}"] = session_change
            samples = prices.setdefault(symbol, [])
            samples.append((now, price))
            prices[symbol] = trim_price_samples(samples, now=now, keep_seconds=keep)
            prior = price_age_seconds_ago(prices[symbol], now=now, window_seconds=window)
            if prior is None:
                continue
            sudden = hour_change_pct(prior[1], price)
            if sudden is None or sudden <= rule.condition.value:
                continue
            age = now - prior[0]
            alert_quote = Quote(
                symbol=symbol,
                company=quote.company,
                price=price,
                change_pct=session_change,
                volume=quote.volume,
            )
            if self.verbose:
                logger.info(
                    "  %s %s: +%.1f%% in %.0fs (session %+.1f%%)",
                    rule.id,
                    symbol,
                    sudden,
                    age,
                    session_change if session_change is not None else 0.0,
                )
            matched.append(alert_quote)
        self._sudden_pool[rule.id] = still
        dropped = pool - still
        for symbol in dropped:
            prices.pop(symbol, None)
            self._sudden_session.pop(f"{rule.id}:{symbol}", None)
        if dropped:
            logger.info(
                "Sudden %s pool drop %s (now %s)",
                rule.id,
                _symbols_text(sorted(dropped)),
                len(still),
            )
        if matched:
            logger.info(
                "Sudden matches for %s: %s",
                rule.id,
                ", ".join(f"{quote.symbol} {quote.change_pct:+.1f}%" for quote in matched),
            )
        return matched

    async def _discover_sudden_pool(
        self, rule: Rule, client: MoomooClient, session: str
    ) -> None:
        """Seed watch pool via session gain screens (pool_min_change is discover-only)."""
        now = datetime.now(timezone.utc).timestamp()
        last = self._sudden_discovered_at.get(rule.id)
        # Stock-filter API: max 10 / 30s shared with newhigh / halt screens.
        # After-hours / pre-market ranks allow ~60 / 30s, so they can stay faster.
        discover_every = 15 if session in {"afterhours", "premarket"} else 45
        if last is not None and now - last < discover_every:
            return
        self._sudden_discovered_at[rule.id] = now
        max_cap = rule.condition.max_market_cap or 1_000_000_000
        min_volume = rule.condition.min_volume
        min_price = rule.condition.min_price
        pool_floor = (
            rule.condition.pool_min_change
            if rule.condition.pool_min_change is not None
            else 0.0
        )
        if last is None:
            logger.info(
                "Sudden %s discover via %s rank/screen, session gain > %g%%, cap <= %.0f",
                rule.id,
                session,
                pool_floor,
                max_cap,
            )
        try:
            found = await self._sudden_session_gainers(
                client,
                session,
                pool_floor,
                max_cap,
                min_volume,
                min_price,
                min_market_cap=rule.condition.min_market_cap,
            )
        except Exception as exc:
            logger.warning("Sudden %s discover failed: %s", rule.id, exc)
            await self._drain_client_problem(client)
            return
        await self._drain_client_problem(client)
        if not found:
            return
        pool = self._sudden_pool.setdefault(rule.id, set())
        prices = self._sudden_prices.setdefault(rule.id, {})
        keep_samples = max(
            (rule.condition.sudden_window_seconds or 60) * 2,
            120,
        )
        added: list[str] = []
        for quote in found:
            if quote.change_pct is None or quote.change_pct <= pool_floor:
                continue
            if min_volume is not None and (quote.volume is None or quote.volume <= min_volume):
                continue
            if min_price is not None and (quote.price is None or quote.price <= min_price):
                continue
            if quote.symbol not in pool:
                added.append(quote.symbol)
            pool.add(quote.symbol)
            self._sudden_session[f"{rule.id}:{quote.symbol}"] = quote.change_pct
            # Seed/refresh a price sample from the rank so the 1-minute window can start immediately.
            if quote.price is not None and quote.price > 0:
                samples = prices.setdefault(quote.symbol, [])
                samples.append((now, quote.price))
                prices[quote.symbol] = trim_price_samples(
                    samples, now=now, keep_seconds=keep_samples
                )
        if len(pool) > 200:
            ranked = sorted(
                pool,
                key=lambda symbol: self._sudden_session.get(f"{rule.id}:{symbol}", float("-inf")),
                reverse=True,
            )
            keep = set(ranked[:200])
            trimmed = sorted(pool - keep)
            for symbol in pool - keep:
                self._sudden_prices.get(rule.id, {}).pop(symbol, None)
                self._sudden_session.pop(f"{rule.id}:{symbol}", None)
            self._sudden_pool[rule.id] = keep
            pool = keep
            if trimmed:
                logger.info(
                    "Sudden %s pool trim %s (now %s)",
                    rule.id,
                    _symbols_text(trimmed),
                    len(pool),
                )
        if added:
            logger.info(
                "Sudden %s pool +%s %s (now %s)",
                rule.id,
                len(added),
                _symbols_text(added),
                len(pool),
            )

    async def _sudden_session_gainers(
        self,
        client: MoomooClient,
        session: str,
        pool_floor: float,
        max_cap: float,
        min_volume: float | None,
        min_price: float | None = None,
        min_market_cap: float | None = None,
    ) -> list[Quote]:
        """Session-gain leaders already above the sudden pool floor."""
        if session == "afterhours":
            rows = await client.scan_afterhours(
                pool_floor, max_cap, min_market_cap=min_market_cap
            )
            return [quote for quote, _cap in rows]
        if session == "premarket":
            rows = await client.scan_premarket(
                pool_floor, max_cap, min_market_cap=min_market_cap
            )
            return [quote for quote, _cap in rows]
        return await client.regular_gainers(
            pool_floor,
            max_market_cap=max_cap,
            min_volume=min_volume,
            min_price=min_price,
            min_market_cap=min_market_cap,
        )

    async def _sudden_session_quotes(
        self, client: MoomooClient, symbols: list[str], session: str
    ) -> dict[str, Quote] | None:
        if not symbols:
            return {}
        if session == "afterhours":
            found = await client.after_hours_quotes(symbols)
        elif session == "premarket":
            found = await client.premarket_quotes(symbols)
        else:
            found = await self._moomoo_snapshots(client, symbols)
        await self._drain_client_problem(client)
        return found

    async def _record_circuit_breakers(self, rule: Rule) -> list[Quote]:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        found = await client.regular_gainers(
            20,
            max_market_cap=rule.condition.max_market_cap,
            min_market_cap=rule.condition.min_market_cap,
        )
        day = now_et().date().isoformat()
        if self._halt_marks_day != day:
            self._halt_marks = {}
            self._halt_marks_day = day
        current, frozen = unchanged_symbols(self._halt_marks, found)
        self._halt_marks = current
        # A halt freezes the price. One batch stays under the 60-snapshot limit.
        symbols = frozen[:50]
        if symbols:
            logger.info(
                "Halt check: %s unchanged (%s), snapshot %s",
                len(frozen),
                _symbols_text(frozen),
                _symbols_text(symbols),
            )
        snaps = await self._moomoo_snapshots(client, symbols)
        seen_at = datetime.now(timezone.utc)
        noted: list[str] = []
        newly: list[Quote] = []
        for symbol, snap in snaps.items():
            if not is_upward_halt(snap):
                continue
            is_new = self.store.spike_seen_at(day, "upward_halt", symbol) is None
            if is_new:
                change = snap.change_pct
                shown = f"{change:+.1f}%" if change is not None else "n/a"
                noted.append(f"{symbol} {shown}")
                newly.append(snap)
            if snap.change_pct is not None:
                self.store.note_peak_gain(day, symbol, snap.change_pct)
            self.store.note_spike(day, "upward_halt", symbol, seen_at)
        signature = tuple(sorted(noted))
        now = datetime.now(timezone.utc)
        previous = self._scan_log.get(f"{rule.id}:halt")
        if noted and (previous is None or previous[0] != signature):
            logger.info("Noted upward halts: %s", _symbols_text(noted))
            self._scan_log[f"{rule.id}:halt"] = (signature, now)
        return newly

    def _close_due(self) -> bool:
        now = datetime.now(timezone.utc)
        last = self._close_checked_at
        if last is not None and (now - last).total_seconds() < 60:
            return False
        self._close_checked_at = now
        return True

    async def _match_regular_gain(self, rule: Rule) -> list[Quote]:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        found = await client.regular_gainers(
            rule.condition.value,
            max_market_cap=rule.condition.max_market_cap,
                    min_market_cap=rule.condition.min_market_cap,
            min_volume=rule.condition.min_volume,
        )
        for quote in found:
            quote.regular_change_pct = quote.change_pct
        self._log_saved_matches(
            rule.id, [quote.symbol for quote in found], f"Regular gain > {rule.condition.value:g}%"
        )
        return found

    async def _match_breaker_close(self, rule: Rule) -> list[Quote]:
        day = now_et().date().isoformat()
        halted = self.store.session_symbols(day, "upward_halt")
        return await self._match_close(rule, halted, "Regular-close circuit breaker")

    async def _match_day_gain_close(self, rule: Rule) -> list[Quote]:
        minimum = rule.condition.min_recent_day_change
        if minimum is None:
            return []
        day = now_et().date().isoformat()
        touched = self.store.day_gain_symbols(minimum, [day])
        return await self._match_close(rule, touched, f"Regular-close day gain > {minimum:g}%")

    async def _match_symbol_close(self, rule: Rule) -> list[Quote]:
        """Close-window check for one symbol (Yahoo IXIC, Moomoo ETF, etc.)."""
        from monitor.yahoo import quote as yahoo_quote
        from monitor.yahoo import uses_yahoo_quote

        symbol = rule.symbol.strip().upper()
        if uses_yahoo_quote(symbol):
            quote = await yahoo_quote(symbol)
        else:
            client = self._trend_moomoo or self.moomoo
            if client is None:
                return []
            snaps = await self._moomoo_snapshots(client, [symbol])
            quote = snaps.get(symbol)
        if quote is None or quote.change_pct is None:
            return []
        change = quote.change_pct
        threshold = rule.condition.value
        shown = quote.symbol or symbol
        if rule.condition.abs_change:
            if abs(change) <= threshold:
                return []
            label = f"Regular-close |{shown}| > {threshold:g}%"
        else:
            if change <= threshold:
                return []
            label = f"Regular-close {shown} > {threshold:g}%"
        quote.regular_change_pct = change
        self._log_saved_matches(rule.id, [shown], label)
        return [quote]

    async def _match_close(self, rule: Rule, symbols: list[str], label: str) -> list[Quote]:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        snaps = await self._moomoo_snapshots(client, symbols[:50]) if symbols else {}
        regular_min = rule.condition.min_regular_change
        matched: list[Quote] = []
        for symbol in symbols:
            quote = snaps.get(symbol)
            if quote is None:
                continue
            if regular_min is not None and (
                quote.change_pct is None or quote.change_pct <= regular_min
            ):
                continue
            quote.regular_change_pct = quote.change_pct
            matched.append(quote)
        self._log_saved_matches(rule.id, [quote.symbol for quote in matched], label)
        return matched

    async def _match_breaker_afterhours(self, rule: Rule) -> list[Quote]:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        found = await client.scan_afterhours(rule.condition.value, rule.condition.max_market_cap, min_market_cap=rule.condition.min_market_cap)
        day = now_et().date().isoformat()
        regular_min = rule.condition.min_regular_change
        matched: list[Quote] = []
        for quote, _cap in found:
            if quote.change_pct is None or quote.change_pct <= rule.condition.value:
                continue
            if regular_min is not None and (
                quote.regular_change_pct is None or quote.regular_change_pct <= regular_min
            ):
                continue
            quote.session_circuit_breaker = (
                self.store.spike_seen_at(day, "upward_halt", quote.symbol) is not None
            )
            evaluation = evaluate_afterhours_scan(
                rule,
                quote,
                market_cap=None,
                in_afterhours=True,
                cap_filtered=(rule.condition.max_market_cap is not None or rule.condition.min_market_cap is not None),
            )
            if self.verbose:
                logger.info("  %s %s: %s", rule.id, quote.symbol, evaluation.reason)
            if evaluation.matched:
                matched.append(quote)
        signature = tuple(sorted(quote.symbol for quote in matched))
        now = datetime.now(timezone.utc)
        previous = self._scan_log.get(rule.id)
        if (
            previous is None
            or previous[0] != signature
            or (now - previous[1]).total_seconds() >= 60
        ):
            logger.info(
                "After-hours circuit breaker matches for %s: %s",
                rule.id,
                _symbols_text(quote.symbol for quote in matched),
            )
            self._scan_log[rule.id] = (signature, now)
        return matched

    async def _match_halt_or_day_afterhours(self, rule: Rule) -> list[Quote]:
        """AH > value among upward-halt names or intraday day-gain touches."""
        return await self._match_halt_or_day(rule)

    async def _match_halt_or_day(self, rule: Rule) -> list[Quote]:
        """Session > value among upward-halt names or day-gain touches."""
        client = self._trend_moomoo or self.moomoo
        floors = _day_gain_floors(rule)
        if client is None or not floors:
            return []
        kind = rule.condition.type
        afterhours = kind is ConditionType.AFTERHOURS_GAIN
        premarket = kind is ConditionType.PREMARKET_GAIN
        regular = kind is ConditionType.REGULAR_GAIN
        if not (afterhours or premarket or regular):
            return []
        today = now_et().date().isoformat()
        days = recent_trading_days(
            now_et().date(),
            rule.condition.recent_day_count,
            include_end=_day_gain_include_today(rule, session_allows=afterhours),
        )
        symbols: set[str] = set()
        # Today's halts always count for halt-or-day matching.
        symbols.update(self.store.session_symbols(today, "upward_halt"))
        for day in days:
            symbols.update(self.store.session_symbols(day, "upward_halt"))
        for minimum in floors:
            symbols.update(self.store.day_gain_symbols(minimum, days))
        label = {
            ConditionType.AFTERHOURS_GAIN: "Halt-or-day after-hours",
            ConditionType.PREMARKET_GAIN: "Halt-or-day pre-market",
            ConditionType.REGULAR_GAIN: "Halt-or-day regular",
        }.get(kind, "Halt-or-day")
        if not symbols:
            self._log_saved_matches(rule.id, [], label)
            return []
        ordered = sorted(symbols)
        if afterhours:
            marks = await self._saved_afterhours_quotes(client, ordered)
            scan_rule = _confirm_scan_rule(_afterhours_scan_rule(rule))
        elif premarket:
            marks = await self._saved_premarket_quotes(client, ordered)
            scan_rule = _confirm_scan_rule(rule)
        else:
            marks = await self._moomoo_snapshots(client, ordered)
            scan_rule = _confirm_scan_rule(rule)
        if not marks:
            return []
        matched: list[Quote] = []
        for symbol in ordered:
            quote = marks.get(symbol)
            if quote is None:
                continue
            quote.session_circuit_breaker = any(
                self.store.spike_seen_at(day, "upward_halt", symbol) is not None
                for day in days
            ) or (self.store.spike_seen_at(today, "upward_halt", symbol) is not None)
            quote.recent_day_spike = any(
                self.store.saw_day_gain(symbol, minimum, days) for minimum in floors
            )
            if quote.recent_day_spike:
                quote.recent_day_gain_floors = [
                    minimum for minimum in floors if self.store.saw_day_gain(symbol, minimum, days)
                ]
            if afterhours:
                evaluation = evaluate_afterhours_scan(
                    scan_rule,
                    quote,
                    market_cap=None,
                    in_afterhours=True,
                    cap_filtered=(
                        rule.condition.max_market_cap is not None
                        or rule.condition.min_market_cap is not None
                    ),
                )
            elif premarket:
                evaluation = evaluate_premarket_scan(
                    scan_rule,
                    quote,
                    market_cap=None,
                    in_premarket=True,
                    cap_filtered=True,
                )
            else:
                quote.regular_change_pct = quote.change_pct
                evaluation = evaluate_regular_day_scan(
                    scan_rule, quote, market_cap=None, cap_filtered=True
                )
            if self.verbose:
                logger.info("  %s %s: %s", rule.id, quote.symbol, evaluation.reason)
            if evaluation.matched:
                matched.append(quote)
        self._log_saved_matches(
            rule.id, [quote.symbol for quote in matched], label
        )
        return matched

    async def _match_afterhours_touch_singles(self, rule: Rule) -> list[Quote]:
        """AH > value among names that have touched the after-hours threshold today."""
        client = self._trend_moomoo or self.moomoo
        minimum = rule.condition.min_afterhours_touch
        if client is None or minimum is None:
            return []
        await self._record_afterhours_touches([rule])
        day = now_et().date().isoformat()
        symbols = self.store.session_symbols(day, afterhours_touch_session(minimum))
        if not symbols:
            self._log_saved_matches(rule.id, [], "After-hours touch singles")
            return []
        marks = await self._saved_afterhours_quotes(client, sorted(symbols))
        if not marks:
            return []
        scan_rule = _afterhours_scan_rule(rule)
        matched: list[Quote] = []
        for symbol in symbols:
            quote = marks.get(symbol)
            if quote is None:
                continue
            evaluation = evaluate_afterhours_scan(
                scan_rule,
                quote,
                market_cap=None,
                in_afterhours=True,
                cap_filtered=(rule.condition.max_market_cap is not None or rule.condition.min_market_cap is not None),
            )
            if self.verbose:
                logger.info("  %s %s: %s", rule.id, quote.symbol, evaluation.reason)
            if evaluation.matched:
                matched.append(quote)
        self._log_saved_matches(
            rule.id, [quote.symbol for quote in matched], "After-hours touch singles"
        )
        return matched

    async def _maybe_send_leaderboard(self, rule: Rule, state: dict) -> None:
        slot = _board_slot_for_rule(rule)
        if slot is None or self.moomoo is None:
            return
        if rule.condition.top_n is None:
            return
        slot_key = f"{rule.id}:{slot.strftime('%Y-%m-%dT%H:%M')}"
        if self.store.last_fired_at(state, slot_key) is not None:
            return
        now = datetime.now(timezone.utc)
        if self._board_retry_at is not None and now < self._board_retry_at:
            return
        min_volume = _board_min_volume(rule, slot)
        peak_board = _is_peak_top_board(rule)
        fade_board = _is_peak_fade_board(rule)
        day_pool_board = _is_day_pool_board(rule)
        kind = rule.condition.type
        try:
            if _is_touched_leaderboard(rule):
                if kind is ConditionType.REGULAR_GAIN:
                    leaders = await self._touched_regular_fade_leaders(rule, min_volume)
                else:
                    leaders = await self._touched_afterhours_leaders(rule, min_volume)
            elif day_pool_board:
                leaders = await self._day_pool_session_leaders(rule, min_volume)
            elif kind is ConditionType.AFTERHOURS_GAIN:
                leaders = await self._top_afterhours_by_peak(rule, min_volume)
            elif kind is ConditionType.PREMARKET_GAIN:
                leaders = await self._top_premarket_by_peak(rule, min_volume)
            elif kind is ConditionType.REGULAR_GAIN:
                leaders = await self._top_regular_by_peak(rule, min_volume)
            else:
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Leaderboard failed for %s: %s", rule.id, exc)
            self._board_retry_at = now + timedelta(seconds=30)
            await self._note_opend_failure(exc)
            await self._note_problem(str(exc))
            return
        self._board_retry_at = None
        self._note_opend_ok()
        need = (
            1
            if _is_touched_leaderboard(rule) or day_pool_board
            else (rule.condition.top_n or 1)
        )
        if len(leaders) < need:
            logger.info(
                "Leaderboard %s %s has %s priced names, waiting",
                rule.id,
                slot.strftime("%H:%M"),
                len(leaders),
            )
            self._board_retry_at = now + timedelta(seconds=30)
            return
        pin = [quote.symbol for quote in leaders]
        if rule.board_notify:
            if fade_board:
                line = fade_leaderboard_line
            elif peak_board:
                line = peak_leaderboard_line
            else:
                line = leaderboard_line
            rows = [(quote.symbol, line(quote)) for quote in leaders]
            title = f"{format_nz_clock(slot)} {rule.name}"
            reason = rule.board_summary
            delivered = await self._deliver_digest(
                rule.id, title, rows, rule.notify, reason=reason
            )
            if not delivered:
                self._board_retry_at = datetime.now(timezone.utc) + timedelta(seconds=30)
                return
            # Digest keeps tickers; persist company names so repeat-history bodies stay consistent.
            message = "\n".join(line(quote, company=True) for quote in leaders)
            if reason and reason not in message:
                message = f"{message}\n{reason}"
            alert = self.store.new_alert(
                rule_id=rule.id,
                rule_name=rule.board_summary,
                label=rule.name,
                notify=rule.notify,
                symbol=leaders[0].symbol,
                message=message,
                price=leaders[0].price,
                change_pct=leaders[0].change_pct,
            )
            self.store.append_alert(alert)
            self.store.mark_fired(state, slot_key, alert.fired_at)
            logger.info("ALERT %s | %s", rule.id, message.replace("\n", " | "))
        else:
            self.store.mark_fired(state, slot_key, datetime.now(timezone.utc))
            logger.info(
                "Board %s %s recorded %s names (no digest)",
                rule.id,
                slot.strftime("%H:%M"),
                len(leaders),
            )
        if _sorts_ths_by_peak_fade(rule):
            # Halt/50%: keep the full pool, sort by peak−close fade (no clear/replace).
            self._add_to_tonghuashun(
                rule,
                self._halt_or_day_pool_symbols(rule),
                board=True,
                pin_symbols=pin,
            )
        elif _is_ah_touch_board_rule(rule):
            self._add_to_tonghuashun(
                rule,
                self._ah_touch_pool_symbols(rule),
                board=True,
                pin_symbols=pin,
            )
        else:
            # Plain *-top5 / 5d50 boards replace the group with current leaders only.
            self._add_to_tonghuashun(rule, leaders, board=True, pin_symbols=pin)

    def _day_pool_symbols(self, rule: Rule) -> list[str]:
        """Prior-day gain pool for day-pool boards (no halt / intraday-touch mix-in)."""
        floors = _day_gain_floors(rule)
        if not floors:
            return []
        days = recent_trading_days(
            now_et().date(),
            rule.condition.recent_day_count,
            include_end=_day_gain_include_today(rule, session_allows=False),
        )
        symbols: set[str] = set()
        for minimum in floors:
            symbols.update(self.store.day_gain_symbols(minimum, days))
        return sorted(symbols)

    async def _day_pool_session_leaders(
        self, rule: Rule, min_volume: float | None
    ) -> list[Quote]:
        """Rank day-gain pool by current session change, highest first."""
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        symbols = self._day_pool_symbols(rule)
        if not symbols:
            return []
        kind = rule.condition.type
        if kind is ConditionType.AFTERHOURS_GAIN:
            marks = await self._saved_afterhours_quotes(client, symbols)
        elif kind is ConditionType.PREMARKET_GAIN:
            marks = await self._saved_premarket_quotes(client, symbols)
        elif kind is ConditionType.REGULAR_GAIN:
            marks = await self._moomoo_snapshots(client, symbols)
        else:
            return []
        if not marks:
            return []
        volume = int(min_volume) if min_volume is not None else None
        # Day-gain pool with live session % above the rule floor (default >0%).
        return afterhours_leaders(
            list(marks.values()),
            rule.condition.top_n or 50,
            volume,
            min_change=rule.condition.value,
        )

    def _halt_or_day_pool_symbols(self, rule: Rule) -> list[str]:
        """Symbols that hit halt or the regular touch / day-gain threshold."""
        day = now_et().date().isoformat()
        symbols: set[str] = set()
        regular_touch = rule.condition.min_regular_touch
        if regular_touch is not None:
            symbols.update(self.store.day_gain_symbols(regular_touch, [day]))
        floors = _day_gain_floors(rule)
        if floors:
            # Today already added via min_regular_touch above; multi-day legs skip today.
            days = recent_trading_days(
                now_et().date(),
                rule.condition.recent_day_count,
                include_end=_day_gain_include_today(rule, session_allows=True),
            )
            for minimum in floors:
                symbols.update(self.store.day_gain_symbols(minimum, days))
        if rule.condition.include_circuit_breaker or rule.condition.require_circuit_breaker:
            symbols.update(self.store.session_symbols(day, "upward_halt"))
        return sorted(symbols)

    def _ah_touch_pool_symbols(self, rule: Rule) -> list[str]:
        """Symbols that once cleared the after-hours touch threshold today."""
        minimum = rule.condition.min_afterhours_touch
        if minimum is None:
            return []
        day = now_et().date().isoformat()
        return self.store.session_symbols(day, afterhours_touch_session(minimum))

    async def _touched_afterhours_leaders(
        self, rule: Rule, min_volume: float | None
    ) -> list[Quote]:
        """Top N by current AH gain among names that once hit the touch thresholds."""
        if rule.condition.min_afterhours_touch is not None:
            await self._record_afterhours_touches([rule])
        day = now_et().date().isoformat()
        symbols: set[str] = set()
        ah_touch = rule.condition.min_afterhours_touch
        if ah_touch is not None:
            symbols.update(self.store.session_symbols(day, afterhours_touch_session(ah_touch)))
        if (
            _sorts_ths_by_peak_fade(rule)
            or _is_pool_gain_board(rule)
            or _day_gain_floors(rule)
            or rule.condition.min_regular_touch is not None
            or rule.condition.include_circuit_breaker
            or rule.condition.require_circuit_breaker
        ):
            symbols.update(self._halt_or_day_pool_symbols(rule))
        if not symbols:
            return []
        marks = await self._saved_afterhours_quotes(self.moomoo, sorted(symbols))
        if not marks:
            return []
        volume = int(min_volume) if min_volume is not None else None
        if _is_peak_fade_board(rule):
            # Pool membership is halt/touch; do not re-filter by current session gain.
            return self._fade_leaders(
                list(marks.values()),
                rule.condition.top_n or 5,
                volume,
                min_fade=rule.condition.min_fade_from_high,
            )
        return afterhours_leaders(
            list(marks.values()),
            rule.condition.top_n or 5,
            volume,
            min_change=rule.condition.value,
        )

    async def _touched_regular_fade_leaders(
        self, rule: Rule, min_volume: float | None
    ) -> list[Quote]:
        """Top N by peak−current among halt / day-touch names in the regular session."""
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        symbols = self._halt_or_day_pool_symbols(rule)
        if not symbols:
            return []
        day = now_et().date().isoformat()
        peaks = self.store.peak_gains(day)
        snaps = await self._moomoo_snapshots(client, sorted(symbols))
        by_symbol: dict[str, Quote] = {}
        for symbol in symbols:
            key = symbol.upper()
            quote = (snaps or {}).get(key) or (snaps or {}).get(symbol)
            if quote is None:
                quote = Quote(symbol=key)
            peak = regular_touch_change_pct(
                change_pct=quote.change_pct,
                high=quote.daily_high,
                prev_close=quote.prev_close,
                pre_high=quote.premarket_high,
            )
            stored = peaks.get(key)
            candidates = [
                value
                for value in (peak, stored, quote.session_high_pct, quote.change_pct)
                if value is not None
            ]
            if candidates:
                best = max(candidates)
                quote.session_high_pct = best
                self.store.note_peak_gain(day, key, best)
            by_symbol[key] = quote
        volume = int(min_volume) if min_volume is not None else None
        return self._fade_leaders(
            list(by_symbol.values()),
            rule.condition.top_n or 5,
            volume,
            min_fade=rule.condition.min_fade_from_high,
        )

    @staticmethod
    def _fade_leaders(
        quotes: list[Quote],
        limit: int,
        min_volume: int | None = None,
        min_change: float | None = None,
        min_fade: float | None = None,
    ) -> list[Quote]:
        """Rank by peak − current giveback, largest first."""
        ready: list[Quote] = []
        for quote in quotes:
            if quote.price is None or quote.price <= 0:
                continue
            if quote.change_pct is None or quote.session_high_pct is None:
                continue
            if min_change is not None and quote.change_pct <= min_change:
                continue
            fade = peak_fade_score(quote.session_high_pct, quote.change_pct)
            if min_fade is not None and fade < min_fade:
                continue
            if min_volume is not None and (quote.volume is None or quote.volume <= min_volume):
                continue
            ready.append(quote)
        ready.sort(
            key=lambda item: peak_fade_score(item.session_high_pct, item.change_pct),
            reverse=True,
        )
        return ready[:limit]

    async def _top_afterhours_by_peak(
        self, rule: Rule, min_volume: float | None
    ) -> list[Quote]:
        """Top N by after-hours current gain, highest first (peak kept for the line)."""
        if self.moomoo is None:
            return []
        max_cap = rule.condition.max_market_cap or 100_000_000
        limit = rule.condition.top_n or 5
        volume = int(min_volume) if min_volume is not None else None
        found_rows = await self.moomoo.scan_afterhours(-1_000_000, max_cap, min_market_cap=rule.condition.min_market_cap)
        day = now_et().date().isoformat()
        peaks = self.store.afterhours_peaks(day)
        by_symbol = {
            quote.symbol.upper(): quote for quote, _cap in found_rows
        }
        for symbol in peaks:
            by_symbol.setdefault(symbol.upper(), Quote(symbol=symbol.upper()))
        candidates = self._peak_board_candidates(by_symbol, peaks, volume)
        marks = await self._saved_afterhours_quotes(self.moomoo, sorted(candidates))
        if marks:
            for symbol, quote in marks.items():
                by_symbol[symbol.upper()] = quote
        for quote in by_symbol.values():
            observed = observed_session_peak(quote)
            if observed is not None:
                self.store.note_afterhours_peak(day, quote.symbol, observed)
        peaks = self.store.afterhours_peaks(day)
        return peak_leaders(list(by_symbol.values()), peaks, limit, volume)

    async def _top_premarket_by_peak(
        self, rule: Rule, min_volume: float | None
    ) -> list[Quote]:
        """Top N by premarket current gain, highest first (peak kept for the line)."""
        if self.moomoo is None:
            return []
        max_cap = rule.condition.max_market_cap or 100_000_000
        limit = rule.condition.top_n or 5
        volume = int(min_volume) if min_volume is not None else None
        found_rows = await self.moomoo.scan_premarket(-1_000_000, max_cap, min_market_cap=rule.condition.min_market_cap)
        day = now_et().date().isoformat()
        peaks = self.store.premarket_peaks(day)
        by_symbol = {
            quote.symbol.upper(): quote for quote, _cap in found_rows
        }
        for symbol in peaks:
            by_symbol.setdefault(symbol.upper(), Quote(symbol=symbol.upper()))
        candidates = self._peak_board_candidates(by_symbol, peaks, volume)
        marks = await self._saved_premarket_quotes(self.moomoo, sorted(candidates))
        if marks:
            for symbol, quote in marks.items():
                by_symbol[symbol.upper()] = quote
        for quote in by_symbol.values():
            observed = observed_session_peak(quote)
            if observed is not None:
                self.store.note_premarket_peak(day, quote.symbol, observed)
        peaks = self.store.premarket_peaks(day)
        return peak_leaders(list(by_symbol.values()), peaks, limit, volume)

    async def _top_regular_by_peak(
        self, rule: Rule, min_volume: float | None
    ) -> list[Quote]:
        """Top N by regular-session current gain, highest first (peak kept for the line)."""
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        max_cap = rule.condition.max_market_cap or 100_000_000
        limit = rule.condition.top_n or 5
        volume = int(min_volume) if min_volume is not None else None
        found = await client.regular_gainers(
            0,
            max_market_cap=max_cap,
            min_volume=volume,
            min_market_cap=rule.condition.min_market_cap,
        )
        day = now_et().date().isoformat()
        peaks = self.store.peak_gains(day)
        by_symbol = {quote.symbol.upper(): quote for quote in found}
        for symbol in peaks:
            by_symbol.setdefault(symbol.upper(), Quote(symbol=symbol.upper()))
        candidates = self._peak_board_candidates(by_symbol, peaks, volume)
        snaps = await self._moomoo_snapshots(client, sorted(candidates))
        if snaps:
            for symbol, snap in snaps.items():
                peak = regular_touch_change_pct(
                    change_pct=snap.change_pct,
                    high=snap.daily_high,
                    prev_close=snap.prev_close,
                    pre_high=snap.premarket_high,
                )
                if peak is not None:
                    snap.session_high_pct = peak
                by_symbol[symbol.upper()] = snap
        for quote in by_symbol.values():
            observed = observed_session_peak(quote)
            if observed is not None:
                self.store.note_peak_gain(day, quote.symbol, observed)
        peaks = self.store.peak_gains(day)
        return peak_leaders(list(by_symbol.values()), peaks, limit, volume)

    @staticmethod
    def _peak_board_candidates(
        by_symbol: dict[str, Quote],
        peaks: dict[str, float],
        volume: int | None,
    ) -> list[str]:
        candidates: list[str] = []
        for symbol, quote in by_symbol.items():
            if symbol in peaks:
                candidates.append(symbol)
            elif volume is None:
                candidates.append(symbol)
            elif quote.volume is not None and quote.volume > volume:
                candidates.append(symbol)
        return candidates

    async def _deliver_digest(
        self,
        rule_id: str,
        title: str,
        rows: list[tuple[str, str]],
        notify: str | None = None,
        reason: str | None = None,
    ) -> bool:
        delivered = False
        slack_url = self.routes.slack_url(notify)
        if slack_url:
            try:
                await self.slack.send_digest(
                    title, rows, webhook_url=slack_url, reason=reason
                )
                delivered = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Slack leaderboard failed for %s: %s", rule_id, exc)
                self.last_error = f"Slack send failed: {exc}"
        token = self.routes.pushover_token(notify)
        if self.pushover is not None and token and self.settings.pushover_user:
            try:
                await self.pushover.send_digest(
                    title, rows, token=token, reason=reason
                )
                delivered = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Pushover leaderboard failed for %s: %s", rule_id, exc)
                self.last_error = f"Pushover send failed: {exc}"
        return delivered

    def _mark_observed_spikes(
        self,
        found: list[tuple[Quote, float | None]],
        rule: Rule,
        session: str,
    ) -> None:
        minimum = rule.condition.min_prior_change
        if minimum is None:
            return
        day = now_et().date().isoformat()
        seen_at = datetime.now(timezone.utc)
        for quote, _cap in found:
            if quote.change_pct is None or quote.change_pct <= minimum:
                continue
            self.store.note_spike(day, session, quote.symbol, seen_at)

    def _apply_observed_spikes(self, quotes: list[Quote], rule: Rule, session: str) -> None:
        minimum = rule.condition.min_prior_change
        if minimum is None:
            return
        day = now_et().date().isoformat()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=rule.condition.prior_change_lag_minutes)
        for quote in quotes:
            seen = self.store.spike_seen_at(day, session, quote.symbol)
            quote.prior_session_spike = seen is not None and seen <= cutoff

    async def _emit_trend_matches(self, rule: Rule, quotes: list[Quote], state: dict) -> list[Alert]:
        fired: list[Alert] = []
        for quote in quotes:
            alert = await self._fire_alert(
                rule,
                quote,
                state,
                extra=None,
                cooldown_key=_alert_cooldown_key(rule, quote.symbol),
            )
            if alert is not None:
                fired.append(alert)
        return fired

    async def _fill_session_uptrends(
        self, quotes: list[Quote], rule: Rule, client: MoomooClient, *, refresh: bool = False
    ) -> None:
        minimum = rule.condition.min_regular_volume
        session_day = now_et().date().isoformat()
        now = datetime.now(timezone.utc)
        checked = 0
        for quote in quotes:
            if minimum is not None and (
                quote.regular_volume is None or quote.regular_volume <= minimum
            ):
                continue
            key = (session_day, quote.symbol)
            if not refresh:
                cached = self._uptrend_cache.get(key)
                if cached is not None:
                    quote.session_uptrend = cached
                    continue
            if self._kline_blocked_until is not None and now < self._kline_blocked_until:
                quote.session_uptrend = self._uptrend_cache.get(key)
                continue
            if checked >= 30:
                quote.session_uptrend = self._uptrend_cache.get(key)
                continue
            result = await client.session_uptrend(quote.symbol, session_day)
            await self._drain_client_problem(client)
            checked += 1
            if result is None:
                # Daily kline quota is tight; wait out the window instead of retrying.
                self._kline_blocked_until = datetime.now(timezone.utc) + timedelta(minutes=5)
                quote.session_uptrend = self._uptrend_cache.get(key)
                break
            self._uptrend_cache[key] = result
            quote.session_uptrend = result

    async def _run_moomoo_scanner(
        self,
        rule: Rule,
        state: dict,
    ) -> tuple[list[Alert], str | None]:
        if self.moomoo is None:
            return [], None
        # Sudden / last-hour must not fall through to session-gain rank screens.
        if rule.condition.type in {
            ConditionType.SUDDEN_GAIN,
            ConditionType.LATE_SESSION_GAIN,
        }:
            logger.warning(
                "Skipping Moomoo session-gain scanner for %s (%s); use dedicated path",
                rule.id,
                rule.condition.type.value,
            )
            return [], None
        afterhours = rule.condition.type is ConditionType.AFTERHOURS_GAIN
        max_cap = rule.condition.max_market_cap or 100_000_000
        if afterhours:
            found = await self.moomoo.scan_afterhours(rule.condition.value, max_cap, min_market_cap=rule.condition.min_market_cap)
        else:
            found = await self.moomoo.scan_premarket(rule.condition.value, max_cap, min_market_cap=rule.condition.min_market_cap)
        matched: list[tuple[Quote, float | None]] = []
        scan_rule = _afterhours_scan_rule(rule) if afterhours else rule
        for quote, cap in found:
            if afterhours:
                evaluation = evaluate_afterhours_scan(
                    scan_rule, quote, market_cap=cap, in_afterhours=True, cap_filtered=True
                )
            else:
                evaluation = evaluate_premarket_scan(
                    rule, quote, market_cap=cap, in_premarket=True, cap_filtered=True
                )
            if self.verbose:
                logger.info("  %s %s: %s", rule.id, quote.symbol, evaluation.reason)
            if not evaluation.matched:
                continue
            matched.append((quote, cap))
        signature = tuple(sorted(quote.symbol for quote, _cap in matched))
        now = datetime.now(timezone.utc)
        previous = self._scan_log.get(rule.id)
        if (
            previous is None
            or previous[0] != signature
            or (now - previous[1]).total_seconds() >= 60
        ):
            session_name = "After-hours" if afterhours else "Pre-market"
            logger.info(
                "%s matches for %s: %s",
                session_name,
                rule.id,
                _symbols_text(quote.symbol for quote, _cap in matched),
            )
            self._scan_log[rule.id] = (signature, now)
        fired: list[Alert] = []
        slack_error: str | None = None
        for quote, cap in matched:
            extra = None
            if cap is not None:
                extra = {"market_cap": cap, "market_cap_m": cap / 1_000_000}
            alert = await self._fire_alert(
                rule,
                quote,
                state,
                extra=extra,
                cooldown_key=_alert_cooldown_key(rule, quote.symbol),
            )
            if alert is None:
                continue
            fired.append(alert)
        return fired, slack_error

    async def preview_premarket(self) -> list[tuple[Quote, float]]:
        if self.moomoo is None:
            return []
        config = self.load_config()
        shown: list[tuple[Quote, float]] = []
        for rule in config.rules:
            if not rule.enabled or not rule.is_scanner or self._rule_session_open(rule):
                continue
            if _is_close_rule(rule) or _is_regular_gain_rule(rule):
                continue
            afterhours = rule.condition.type is ConditionType.AFTERHOURS_GAIN
            max_cap = None if rule.condition.skip_market_cap else (rule.condition.max_market_cap or 100_000_000)
            if afterhours:
                found = await self.moomoo.scan_afterhours(rule.condition.value, max_cap, min_market_cap=rule.condition.min_market_cap)
            else:
                found = await self.moomoo.scan_premarket(rule.condition.value, max_cap, min_market_cap=rule.condition.min_market_cap)
            matched = []
            for quote, cap in found:
                if afterhours:
                    ok = evaluate_afterhours_scan(
                        rule, quote, market_cap=cap, in_afterhours=True, cap_filtered=True
                    ).matched
                else:
                    ok = evaluate_premarket_scan(
                        rule, quote, market_cap=cap, in_premarket=True, cap_filtered=True
                    ).matched
                if ok:
                    matched.append((quote, cap))
            logger.info(
                "Moomoo preview %s: %s names >= %.0f%%, matched %s (not alerting)",
                rule.id,
                len(found),
                rule.condition.value,
                _symbols_text(quote.symbol for quote, _cap in matched),
            )
            matched.sort(key=lambda item: item[0].change_pct or 0, reverse=True)
            for quote, cap in matched[:15]:
                if cap is None:
                    logger.info(
                        "  %-6s %+.1f%%  $%.2f  vol %s",
                        quote.symbol,
                        quote.change_pct or 0,
                        quote.price or 0,
                        f"{quote.volume:,}" if quote.volume is not None else "n/a",
                    )
                else:
                    logger.info(
                        "  %-6s %+.1f%%  $%.2f  cap $%.1fM",
                        quote.symbol,
                        quote.change_pct or 0,
                        quote.price or 0,
                        cap / 1_000_000,
                    )
            shown.extend(matched)
        return shown

    async def _fill_prior_session(self, rule: Rule, quote: Quote) -> None:
        """Copy the previous session's close onto the quote used in the body."""
        session = rule.group or rule.condition_session
        if session is None or self.moomoo is None:
            return
        try:
            snaps = await self._moomoo_snapshots(self.moomoo, [quote.symbol])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Prior-session quote unavailable for %s: %s", quote.symbol, exc)
            return
        snap = snaps.get(quote.symbol)
        if snap is None:
            return
        if session == "盘后":
            if snap.regular_price:
                quote.regular_price = snap.regular_price
            if snap.regular_change_pct is not None:
                quote.regular_change_pct = snap.regular_change_pct
        elif session == "盘前":
            if snap.after_hours_price:
                quote.after_hours_price = snap.after_hours_price
            if snap.after_hours_change_pct is not None:
                quote.after_hours_change_pct = snap.after_hours_change_pct
        elif session == "盘中":
            if snap.premarket_price:
                quote.premarket_price = snap.premarket_price
            if snap.premarket_change_pct is not None:
                quote.premarket_change_pct = snap.premarket_change_pct

    def _blocked_by_confirm_delay(
        self, rule: Rule, quote: Quote, state: dict, cooldown_key: str
    ) -> bool:
        """Arm on first touch above condition.value; alert after delay when > confirm_value."""
        if rule.confirm_delay_minutes <= 0:
            return False
        if ":pool:" in cooldown_key:
            return False
        notify = (rule.notify or rule.id).strip() or rule.id
        arm_key = f"confirm:{notify}:{quote.symbol.upper()}"
        now = datetime.now(timezone.utc)
        armed_at = self.store.confirm_armed_at(state, arm_key)
        change = quote.change_pct
        sudden = rule.condition.type is ConditionType.SUDDEN_GAIN
        if armed_at is None:
            if sudden:
                # 1-minute spike already matched; arm regardless of session gain.
                arm_floor = rule.condition.value
            elif rule.condition.day_gain_tiers:
                if matched_day_gain_tier(rule, quote) is None:
                    return True
                arm_floor = rule.condition.value
            else:
                if change is None or change <= rule.condition.value:
                    return True
                arm_floor = rule.condition.value
            self.store.note_confirm_arm(state, arm_key, now)
            logger.info(
                "Rule %s first +%.0f%% for %s — armed, no alert yet",
                rule.id,
                change if change is not None else arm_floor,
                quote.symbol,
            )
            return True
        elapsed = (now - armed_at).total_seconds()
        if elapsed < rule.confirm_delay_minutes * 60:
            logger.info(
                "Rule %s matched %s but confirm delay %.0f/%.0f min",
                rule.id,
                quote.symbol,
                elapsed / 60,
                rule.confirm_delay_minutes,
            )
            return True
        if sudden and rule.confirm_value is not None:
            if change is None or change < rule.confirm_value:
                return True
        return False

    async def _fire_alert(
        self,
        rule: Rule,
        quote: Quote,
        state: dict,
        extra: dict | None,
        cooldown_key: str | None = None,
        *,
        apply_cooldown: bool = True,
        skip_important: bool = False,
        skip_confirm: bool = False,
    ) -> Alert | None:
        key = cooldown_key or rule.id
        if not skip_confirm and self._blocked_by_confirm_delay(rule, quote, state, key):
            return None
        if rule.once_per_session:
            if self.store.last_fired_at(state, key) is not None:
                logger.info(
                    "Rule %s matched %s but already alerted this session",
                    rule.id,
                    quote.symbol,
                )
                return None
        elif self.store.in_cooldown(state, key, rule.cooldown_minutes):
            logger.info("Rule %s matched %s but is in cooldown", rule.id, quote.symbol)
            return None
        moment = now_et()
        session = et_session_name(moment)
        # Same symbol hitting several rules at once → one Slack/Pushover ping.
        # Short coalesce window only; later separate hits still notify.
        suppress_deliver = False
        blocked_by: str | None = None
        lock = getattr(self, "_symbol_alert_lock", None)
        if lock is None:
            self._symbol_alert_lock = asyncio.Lock()
            lock = self._symbol_alert_lock
        async with lock:
            live = self.store.load_state()
            blocked_by = self.store.claim_symbol_coalesce_alert(
                live,
                symbol=quote.symbol,
                rule_id=_rule_notify_key(rule),
            )
            state.setdefault("symbol_coalesce_alerts", {}).update(
                live.get("symbol_coalesce_alerts", {})
            )
            self.store.save_state(live)
            suppress_deliver = blocked_by is not None
        if suppress_deliver:
            logger.info(
                "Rule %s matched %s but skipped notify — simultaneous hit already sent via %s",
                rule.id,
                quote.symbol,
                blocked_by,
            )
            if apply_cooldown:
                self.store.mark_fired(state, key, datetime.now(timezone.utc))
            if not _is_symbol_close_rule(rule):
                important_symbols: list[str] = [] if skip_important else [quote.symbol]
                pin = [quote.symbol]
                if _sorts_ths_by_peak_fade(rule):
                    self._add_to_tonghuashun(
                        rule,
                        self._halt_or_day_pool_symbols(rule),
                        board=False,
                        important_symbols=important_symbols,
                        pin_symbols=pin,
                    )
                elif _is_ah_touch_board_rule(rule):
                    self._add_to_tonghuashun(
                        rule,
                        self._ah_touch_pool_symbols(rule),
                        board=False,
                        important_symbols=important_symbols,
                        pin_symbols=pin,
                    )
                else:
                    self._add_to_tonghuashun(
                        rule,
                        [quote],
                        board=False,
                        important_symbols=important_symbols,
                        pin_symbols=pin,
                    )
            return None
        await self._fill_prior_session(rule, quote)
        # Persist the trigger move for later history; show prior-session on the live top line.
        stored_message = render_message(rule, quote, extra, headline="trigger")
        display_message = render_message(rule, quote, extra, headline="prior")
        alert = self.store.new_alert(
            rule_id=rule.id,
            rule_name=rule.summary,
            label=rule.name,
            notify=rule.notify,
            symbol=quote.symbol,
            message=stored_message,
            price=quote.price,
            change_pct=quote.change_pct,
        )
        alert.url = quote_url(quote.symbol)
        prior = (
            self.store.symbol_alerts_in_session(quote.symbol, session, moment.date())
            if session
            else []
        )
        delivery = with_repeat_history(
            alert.model_copy(update={"message": display_message}), prior
        )
        alert.repeat_count = delivery.repeat_count
        important = self.load_config().important
        high = _is_high_priority(important, rule, moment=moment)
        await self._deliver(
            delivery,
            priority=1 if high else None,
            sound="pushover" if high else None,
        )
        self.store.append_alert(alert)
        if apply_cooldown:
            self.store.mark_fired(state, key, alert.fired_at)
        logger.info("ALERT %s | %s", rule.id, alert.message)
        if not _is_symbol_close_rule(rule):
            important_symbols = [] if skip_important else [quote.symbol]
            pin = [quote.symbol]
            if _sorts_ths_by_peak_fade(rule):
                self._add_to_tonghuashun(
                    rule,
                    self._halt_or_day_pool_symbols(rule),
                    board=False,
                    important_symbols=important_symbols,
                    pin_symbols=pin,
                )
            elif _is_ah_touch_board_rule(rule):
                self._add_to_tonghuashun(
                    rule,
                    self._ah_touch_pool_symbols(rule),
                    board=False,
                    important_symbols=important_symbols,
                    pin_symbols=pin,
                )
            else:
                self._add_to_tonghuashun(
                    rule,
                    [quote],
                    board=False,
                    important_symbols=important_symbols,
                    pin_symbols=pin,
                )
        return alert

    def _ths_active_session(self) -> str | None:
        if is_premarket():
            return "盘前"
        if is_regular_session():
            return "盘中"
        if is_afterhours():
            return "盘后"
        return None

    async def _maybe_sync_tonghuashun_groups(self, config: RulesConfig) -> None:
        """Create groups once; clear each session's rule groups on that session's next day.

        Shared rule groups and 我的自选 clear the previous session's names when the next
        session starts.
        """
        if not self.ths.configured:
            return
        self.ths.set_important_group(config.important.ths_group)
        day = now_et().date().isoformat()
        names = [
            rule.name
            for rule in config.rules
            if rule.enabled
            and rule.name
            and not _is_symbol_close_rule(rule)
            and not _skips_ths_rule_group(rule)
        ]
        session = self._ths_active_session()
        need_structure = not self._ths_groups_synced
        need_session_clear = bool(session) and self._ths_cleared_session_day.get(session) != day
        if not need_structure and not need_session_clear:
            return
        async with self._ths_lock:
            try:
                if need_structure:
                    result = await self.ths.sync_groups(names)
                    self._ths_groups_synced = True
                    if result.get("created") or result.get("deleted"):
                        logger.info(
                            "同花顺 groups synced created=%s deleted=%s",
                            result.get("created") or [],
                            result.get("deleted") or [],
                        )
                if need_session_clear and session is not None:
                    session_names = [
                        rule.name
                        for rule in config.rules
                        if rule.enabled
                        and rule.name
                        and (rule.group or rule.condition_session) == session
                        and not _skips_ths_rule_group(rule)
                    ]
                    shared_names = _shared_rule_group_names(config)
                    cleared = await self.ths.clear_session_day(
                        session, day, session_names, shared_rule_names=shared_names
                    )
                    self._ths_cleared_session_day[session] = day
                    if cleared:
                        logger.info(
                            "同花顺 cleared %s groups for %s: %s",
                            session,
                            day,
                            ", ".join(cleared),
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning("同花顺 group sync failed: %s", exc)
                await self._note_problem(f"Tonghuashun add failed: {exc}")

    def _add_to_tonghuashun(
        self,
        rule: Rule,
        quotes: list[Quote] | list[str],
        *,
        board: bool = False,
        important_symbols: list[str] | None = None,
        pin_symbols: list[str] | None = None,
    ) -> None:
        if not self.ths.configured:
            return
        task = asyncio.create_task(
            self._tonghuashun_sync(
                rule,
                quotes,
                board=board,
                important_symbols=important_symbols,
                pin_symbols=pin_symbols,
            )
        )
        self._ths_tasks.add(task)
        task.add_done_callback(self._ths_tasks.discard)

    async def _tonghuashun_sync(
        self,
        rule: Rule,
        quotes: list[Quote] | list[str],
        *,
        board: bool = False,
        important_symbols: list[str] | None = None,
        pin_symbols: list[str] | None = None,
    ) -> None:
        group = rule.name
        day = now_et().date().isoformat()
        session = rule.group or rule.condition_session or self._ths_active_session() or "盘中"
        symbols = [
            item.symbol if isinstance(item, Quote) else str(item).upper() for item in quotes
        ]
        pin = (
            [symbol.strip().upper() for symbol in pin_symbols if symbol and str(symbol).strip()]
            if pin_symbols is not None
            else [symbol for symbol in symbols if symbol.strip()]
        )
        important = self.load_config().important
        self.ths.set_important_group(important.ths_group)
        want_important = _wants_important_watchlist(important, rule, board=board)
        if not board and _skips_ths_rule_group(rule):
            # halt-or-day50 / 5d-boost: alerted names → 我的自选 only.
            target = [
                symbol.strip().upper()
                for symbol in (important_symbols if important_symbols is not None else pin)
                if symbol and str(symbol).strip()
            ]
            if not (want_important and target):
                return
            async with self._ths_lock:
                try:
                    await self.ths.add_important(target, session)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("同花顺 update failed for 我的自选 (%s): %s", rule.id, exc)
                    await self._note_problem(
                        f"Tonghuashun add failed for {', '.join(dict.fromkeys(target))}: {exc}"
                    )
            return
        replace_board = board and (
            _is_peak_top_board(rule)
            or _is_pool_gain_board(rule)
            or _is_day_pool_board(rule)
        )
        if replace_board:
            # Plain *-top5 groups keep only the current leaders.
            symbols = [symbol for symbol in pin if symbol]
            pin = list(symbols)
        async with self._ths_lock:
            try:
                members = await self.ths.members(
                    group,
                    day,
                    symbols,
                    session,
                    bump_symbols=pin,
                    replace=replace_board,
                )
                if replace_board:
                    # Peak top-N: group is only the current leaders, ranked high→low.
                    ordered = list(pin)
                elif _sorts_ths_by_peak_fade(rule):
                    peaks = self.store.peak_gains(day)
                    closes = await self._regular_close_changes(members)
                    ordered = sorted(
                        members,
                        key=lambda symbol: peak_fade_score(
                            peaks.get(symbol), closes.get(symbol)
                        ),
                        reverse=True,
                    )
                elif _is_peak_fade_rule(rule):
                    if rule.condition.type is ConditionType.AFTERHOURS_GAIN:
                        peaks = self.store.afterhours_peaks(day)
                    elif rule.condition.type is ConditionType.PREMARKET_GAIN:
                        peaks = self.store.premarket_peaks(day)
                    else:
                        peaks = self.store.peak_gains(day)
                    changes = {
                        item.symbol: item.change_pct
                        for item in quotes
                        if isinstance(item, Quote)
                    }
                    changes.update(await self._session_changes(rule, members))
                    ordered = sorted(
                        members,
                        key=lambda symbol: peak_fade_score(
                            peaks.get(symbol), changes.get(symbol)
                        ),
                        reverse=True,
                    )
                elif _is_peak_top_board(rule):
                    changes = {
                        item.symbol: item.change_pct
                        for item in quotes
                        if isinstance(item, Quote)
                    }
                    changes.update(await self._session_changes(rule, members))
                    ordered = sorted(
                        members,
                        key=lambda symbol: (
                            changes[symbol]
                            if changes.get(symbol) is not None
                            else float("-inf")
                        ),
                        reverse=True,
                    )
                else:
                    changes = {
                        item.symbol: item.change_pct
                        for item in quotes
                        if isinstance(item, Quote)
                    }
                    changes.update(await self._session_changes(rule, members))
                    ordered = sorted(
                        members,
                        key=lambda symbol: (
                            changes[symbol]
                            if changes.get(symbol) is not None
                            else float("-inf")
                        ),
                        reverse=True,
                    )
                # Just-alerted names (including repeats) stay at the top of the rule group.
                if not replace_board:
                    alerted = {symbol for symbol in pin if symbol}
                    if alerted:
                        ordered = [s for s in ordered if s in alerted] + [
                            s for s in ordered if s not in alerted
                        ]
                await self.ths.arrange(
                    group,
                    ordered,
                    session=session,
                    tag_symbols=pin if not replace_board else ordered,
                )
                if want_important:
                    target = [
                        symbol.strip().upper()
                        for symbol in (important_symbols if important_symbols is not None else pin)
                        if symbol and str(symbol).strip()
                    ]
                    if target:
                        await self.ths.add_important(target, session)
            except Exception as exc:  # noqa: BLE001
                logger.warning("同花顺 update failed for %s: %s", group, exc)
                failed = [
                    symbol.strip().upper()
                    for symbol in (pin or symbols)
                    if symbol and str(symbol).strip()
                ]
                if failed:
                    joined = ", ".join(dict.fromkeys(failed))
                    await self._note_problem(f"Tonghuashun add failed for {joined}: {exc}")
                else:
                    await self._note_problem(f"Tonghuashun add failed: {exc}")

    async def _session_changes(self, rule: Rule, symbols: list[str]) -> dict[str, float | None]:
        """Current gain for the rule's session, so the group sorts high to low."""
        client = self._trend_moomoo or self.moomoo
        if client is None or not symbols:
            return {}
        session = rule.group or rule.condition_session
        if session == "盘后":
            found = await client.after_hours_quotes(symbols)
        elif session == "盘前":
            found = await client.premarket_quotes(symbols)
        else:
            found = await client.snapshots(symbols)
        return {symbol: quote.change_pct for symbol, quote in (found or {}).items()}

    async def _regular_close_changes(self, symbols: list[str]) -> dict[str, float | None]:
        """Regular-session close change % for after-hours 同花顺 fade sorting."""
        client = self._trend_moomoo or self.moomoo
        if client is None or not symbols:
            return {}
        if is_afterhours() or afterhours_just_opened():
            found = await client.after_hours_quotes(symbols)
        else:
            found = await client.snapshots(symbols)
        out: dict[str, float | None] = {}
        for symbol, quote in (found or {}).items():
            if quote.regular_change_pct is not None:
                out[symbol] = quote.regular_change_pct
            elif not is_afterhours() and not afterhours_just_opened():
                out[symbol] = quote.change_pct
            else:
                out[symbol] = None
        return out

    def _scanner_digest(self, rule: Rule, alerts: list[Alert]) -> str:
        lines = [f":chart_with_upwards_trend: *{rule.summary}*"]
        lines.extend(f"• {alert.message}" for alert in alerts)
        return "\n".join(lines)

    def upsert_rule(self, rule: Rule) -> RulesConfig:
        config = self.load_config()
        remaining = [item for item in config.rules if item.id != rule.id]
        remaining.append(rule)
        config.rules = remaining
        if rule.symbol not in config.watchlist and rule.symbol not in {"*", "ALL"}:
            config.watchlist.append(rule.symbol)
        return self.save_config(config)

    async def close(self) -> None:
        await self._marketcap.close()
        if self._trend_moomoo is not None:
            await self._trend_moomoo.close()
        if self.moomoo is not None:
            await self.moomoo.close()

    def delete_rule(self, rule_id: str) -> RulesConfig:
        config = self.load_config()
        config.rules = [item for item in config.rules if item.id != rule_id]
        return self.save_config(config)

    async def force_alert(self, symbol: str) -> Alert:
        symbol = symbol.strip().upper()
        config = self.load_config()
        if self.moomoo is not None:
            quotes = await self._moomoo_snapshots(self.moomoo, [symbol])
        else:
            quotes = await self.alpaca.snapshots(
                [symbol], config.feed or self.settings.alpaca_feed
            )
        quote = quotes.get(symbol)
        if quote is None or quote.price is None:
            raise RuntimeError(f"No quote for {symbol}")
        alert = self.store.new_alert(
            rule_id="manual-test",
            rule_name="Manual test alert",
            symbol=symbol,
            message=f"Manual test: {symbol} is ${quote.price:.2f}",
            price=quote.price,
            change_pct=quote.change_pct,
        )
        alert.url = quote_url(symbol)
        moment = now_et()
        session = et_session_name(moment)
        prior = (
            self.store.symbol_alerts_in_session(symbol, session, moment.date())
            if session
            else []
        )
        delivery = with_repeat_history(alert, prior)
        alert.repeat_count = delivery.repeat_count
        await self._deliver(delivery)
        self.store.append_alert(alert)
        logger.info("Sent test alert for %s at %s", symbol, quote.price)
        return alert

    async def _deliver(
        self,
        alert: Alert,
        *,
        priority: int | None = None,
        sound: str | None = None,
    ) -> None:
        errors: list[str] = []
        slack_url = self.routes.slack_url(alert.notify)
        if slack_url:
            try:
                await self.slack.send_alert(alert, webhook_url=slack_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Slack send failed for %s: %s", alert.rule_id, exc)
                errors.append(f"Slack send failed: {exc}")
                await self._note_problem(f"Slack send failed: {exc}")
        token = self.routes.pushover_token(alert.notify)
        if self.pushover is not None and token and self.settings.pushover_user:
            try:
                await self.pushover.send_alert(
                    alert, token=token, priority=priority, sound=sound
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Pushover send failed for %s: %s", alert.rule_id, exc)
                errors.append(f"Pushover send failed: {exc}")
                await self._note_problem(f"Pushover send failed: {exc}")
        if errors:
            self.last_error = "; ".join(errors)

    def _log_market(self) -> None:
        if self.market is None:
            message = "Market clock unavailable"
        elif self.market.is_open:
            message = "US market is open"
        else:
            nxt = self.market.next_open.isoformat() if self.market.next_open else "unknown"
            message = f"US market is closed | next open {nxt}"
        if message == self._last_market_log:
            return
        self._last_market_log = message
        logger.info(message)

    def _log_quotes(self, quotes: dict[str, Quote]) -> None:
        if not quotes:
            logger.info("No quotes returned")
            return
        for symbol in sorted(quotes):
            quote = quotes[symbol]
            price = f"${quote.price:.2f}" if quote.price is not None else "n/a"
            change = f"{quote.change_pct:+.2f}%" if quote.change_pct is not None else "n/a"
            logger.info("%-5s %s  %s", symbol, price, change)
