from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from monitor.alpaca import AlpacaClient
from monitor.config import Settings
from monitor.marketcap import MarketCapClient
from monitor.moomoo_client import (
    MoomooClient,
    is_upward_halt,
    regular_high_change_pct,
)
from monitor.models import Alert, ConditionType, MonitorStatus, Quote, Rule, RulesConfig
from monitor.notify import NotifyRoutes
from monitor.quotes import apply_quote, apply_trade, previous_price
from monitor.problems import ProblemLatch, problem_notice
from monitor.pushover import PushoverClient
from monitor.rules import (
    evaluate_afterhours_scan,
    evaluate_last_hour_scan,
    evaluate_premarket_scan,
    evaluate_stable_afterhours,
    evaluate_rule,
    hour_change_pct,
    leaderboard_line,
    load_rules,
    render_message,
    save_rules,
)
from monitor.session import (
    AFTERHOURS_START,
    extended_session_open,
    afterhours_just_opened,
    afterhours_stable_due,
    afterhours_stable_missed,
    is_afterhours,
    is_premarket,
    is_regular_session,
    is_trading_day,
    last_hour_phase,
    regular_close_approach,
    now_et,
    recent_trading_days,
    afterhours_board_slot,
    premarket_mark,
    seconds_until_extended_open,
)
from monitor.slack import SlackClient
from monitor.quote_link import quote_url
from monitor.store import Store
from monitor.stream import AlpacaStream, StreamEvent
from monitor.universe import UniverseCache

logger = logging.getLogger("personaltrader.monitor")


def _is_trend_rule(rule: Rule) -> bool:
    return rule.condition.skip_market_cap or rule.condition.require_uptrend


def _is_spike_rule(rule: Rule) -> bool:
    return rule.condition.min_prior_change is not None


def _is_breaker_rule(rule: Rule) -> bool:
    return rule.condition.require_circuit_breaker


def _is_leaderboard_rule(rule: Rule) -> bool:
    return rule.condition.top_n is not None


def _is_day_spike_rule(rule: Rule) -> bool:
    return rule.condition.min_recent_day_change is not None


def _is_stable_rule(rule: Rule) -> bool:
    condition = rule.condition
    return (
        condition.stable_minutes is not None
        and condition.stable_low is not None
        and condition.stable_high is not None
    )


def _is_last_hour_rule(rule: Rule) -> bool:
    return rule.condition.type is ConditionType.LATE_SESSION_GAIN


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
        self.store = store
        self.verbose = verbose
        self.quotes: dict[str, Quote] = {}
        self.last_poll_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.market = None
        self._running = False
        self._lock = asyncio.Lock()
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
        self._day_gain_recorded_at: Optional[datetime] = None
        self._stable_pool: dict[str, dict[str, Quote]] = {}
        self._stable_ready_day: dict[str, str] = {}
        self._stable_done: set[str] = set()
        self._stable_captured_at: Optional[datetime] = None
        self._stable_frozen_day: str | None = None

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
                    matched = await self._match_trend_afterhours(rule)
                    fired.extend(await self._emit_trend_matches(rule, matched, state))
            spike_rules = [rule for rule in open_rules if _is_spike_rule(rule)]
            if spike_rules and not self._spike_loop_running:
                for rule in spike_rules:
                    matched = await self._match_spike_rule(rule)
                    fired.extend(await self._emit_trend_matches(rule, matched, state))
            day_spike_rules = [rule for rule in open_rules if _is_day_spike_rule(rule)]
            if day_spike_rules and not self._day_spike_loop_running:
                for rule in day_spike_rules:
                    matched = await self._match_day_spike_rule(rule)
                    fired.extend(await self._emit_trend_matches(rule, matched, state))
            breaker_rules = [rule for rule in open_rules if _is_breaker_rule(rule)]
            if breaker_rules and not self._regular_loop_running:
                for rule in breaker_rules:
                    matched = await self._match_breaker_afterhours(rule)
                    fired.extend(await self._emit_trend_matches(rule, matched, state))
            for rule in open_rules:
                if (
                    rule in afterhours_rules
                    or _is_spike_rule(rule)
                    or _is_breaker_rule(rule)
                    or _is_leaderboard_rule(rule)
                    or _is_day_spike_rule(rule)
                ):
                    continue
                scanned, scan_error = await self._run_scanner(rule, config, state)
                fired.extend(scanned)
                slack_error = slack_error or scan_error
            self._last_scan_at = datetime.now(timezone.utc)

        if self.moomoo is not None and is_afterhours():
            for rule in config.rules:
                if rule.enabled and _is_leaderboard_rule(rule):
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
                    matched = await self._match_trend_afterhours(rule)
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
                if not rules:
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
                    if rule.enabled and _is_day_spike_rule(rule) and self._rule_session_open(rule)
                ]
                if not rules:
                    await asyncio.sleep(interval)
                    continue
                for rule in rules:
                    matched = await self._match_day_spike_rule(rule)
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
                rules = [
                    rule for rule in config.rules if rule.enabled and _is_breaker_rule(rule)
                ]
                last_hour_rules = [
                    rule for rule in config.rules if rule.enabled and _is_last_hour_rule(rule)
                ]
                stable_rules = [
                    rule for rule in config.rules if rule.enabled and _is_stable_rule(rule)
                ]
                day_gain_rules = [
                    rule for rule in config.rules if rule.enabled and _is_day_spike_rule(rule)
                ]
                if not rules and not last_hour_rules and not stable_rules and not day_gain_rules:
                    await asyncio.sleep(30)
                    continue
                if is_regular_session():
                    if day_gain_rules and self._day_gain_due():
                        await self._record_day_gains(day_gain_rules)
                        self._note_opend_ok()
                    if stable_rules and self._stable_capture_due():
                        client = self._trend_moomoo or self.moomoo
                        if client is not None:
                            for rule in stable_rules:
                                await self._capture_stable_names(rule, client)
                                self._note_opend_ok()
                    if rules and self._breaker_due():
                        for rule in rules:
                            await self._record_circuit_breakers(rule)
                            self._note_opend_ok()
                    await self._run_late_rules(last_hour_rules)
                elif is_afterhours():
                    if rules and self._breaker_due():
                        for rule in rules:
                            matched = await self._match_breaker_afterhours(rule)
                            self._note_opend_ok()
                            if not matched:
                                continue
                            async with self._lock:
                                state = self.store.load_state()
                                await self._emit_trend_matches(rule, matched, state)
                                self.store.save_state(state)
                    await self._run_late_rules(last_hour_rules)
                elif is_premarket():
                    await self._run_late_rules(last_hour_rules)
                else:
                    await asyncio.sleep(30)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Circuit breaker scan failed: %s", exc)
                await self._note_opend_failure(exc)
                await self._note_problem(str(exc))
            await asyncio.sleep(10)

    def _day_gain_due(self) -> bool:
        now = datetime.now(timezone.utc)
        last = self._day_gain_recorded_at
        if last is not None and (now - last).total_seconds() < 60:
            return False
        self._day_gain_recorded_at = now
        return True

    async def _record_day_gains(self, rules: list[Rule]) -> None:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return
        day = now_et().date().isoformat()
        plain: dict[tuple[float, float | None], None] = {}
        touched: dict[tuple[float, float | None], None] = {}
        for rule in rules:
            minimum = rule.condition.min_recent_day_change
            if minimum is None:
                continue
            key = (minimum, rule.condition.max_market_cap)
            if rule.condition.intraday_touch:
                touched[key] = None
            else:
                plain[key] = None
        for minimum, cap in plain:
            found = await client.regular_gainers(minimum, max_market_cap=cap)
            self._note_day_gain_names(day, minimum, [quote.symbol for quote in found])
        for minimum, cap in touched:
            await self._record_intraday_touch(client, day, minimum, cap)

    def _note_day_gain_names(self, day: str, minimum: float, symbols: list[str]) -> None:
        added = self.store.note_day_gains(day, minimum, symbols)
        if added:
            logger.info("Recorded day gain > %.0f%%: %s", minimum, ", ".join(added))

    async def _record_intraday_touch(
        self,
        client: MoomooClient,
        day: str,
        minimum: float,
        cap: float | None,
    ) -> None:
        """Remember a name that traded above the threshold, even if it later fell back."""
        found = await client.regular_gainers(20, max_market_cap=cap)
        snaps = await self._moomoo_snapshots(client, [quote.symbol for quote in found])
        symbols = [
            quote.symbol
            for quote in found
            if quote.change_pct is not None and quote.change_pct > minimum
        ]
        for symbol, snap in snaps.items():
            change = regular_high_change_pct(snap.daily_high, snap.prev_close)
            if change is not None and change > minimum:
                symbols.append(symbol)
        self._note_day_gain_names(day, minimum, symbols)

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
        text = f"OpenD 行情断开，扫描已暂停。{now_et().strftime('%H:%M')} ET"
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
        message = f"{body} {now_et().strftime('%H:%M')} ET"
        await self._send_system(title, message)

    async def _send_system(self, title: str, message: str) -> None:
        routes = getattr(self, "routes", None)
        token = routes.pushover_token("opend") if routes is not None else None
        slack_url = routes.slack_url("opend") if routes is not None else ""
        if self.pushover is not None and (token or routes is None):
            try:
                await self.pushover.send_text(message, title=title, token=token)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not send system alert: %s", exc)
        elif self.pushover is None and title == "OpenD 断开":
            logger.warning("OpenD disconnected")
        if getattr(self, "slack", None) is not None and slack_url:
            try:
                await self.slack.send_text(f"{title}\n{message}", webhook_url=slack_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not send system alert to Slack: %s", exc)

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
            cooldown_key=f"{rule.id}:{quote.symbol}",
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
        if rule.condition.type is ConditionType.AFTERHOURS_GAIN:
            return is_afterhours()
        if rule.condition.type is ConditionType.PREMARKET_GAIN:
            return is_premarket()
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

        logger.info("Premarket matches after market-cap filter: %s", len(matched))
        fired: list[Alert] = []
        slack_error: str | None = None
        for quote, cap in matched:
            extra = {"market_cap": cap, "market_cap_m": cap / 1_000_000}
            alert = await self._fire_alert(
                rule,
                quote,
                state,
                extra=extra,
                cooldown_key=f"{rule.id}:{quote.symbol}",
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
        found = await self.moomoo.scan_afterhours(min(rule.condition.value for rule in rules), caps.pop())
        fired = []
        slack_error = None
        for rule in rules:
            matched: list[tuple[Quote, float | None]] = []
            for quote, cap in found:
                evaluation = evaluate_afterhours_scan(
                    rule, quote, market_cap=cap, in_afterhours=True, cap_filtered=True
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
                logger.info("After-hours matches for %s: %s", rule.id, len(matched))
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
                    cooldown_key=f"{rule.id}:{quote.symbol}",
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
            minimum, max_market_cap=rule.condition.max_market_cap
        )
        pool: dict[str, Quote] = {}
        for quote in found:
            quote.regular_change_pct = quote.change_pct
            quote.change_pct = None
            pool[quote.symbol] = quote
        self._stable_pool[rule.id] = pool
        self._stable_ready_day[rule.id] = now_et().date().isoformat()
        logger.info("%s regular names above %.0f%%: %s", rule.id, minimum, len(pool))

    async def _stable_rank_fallback(self, rule: Rule) -> list[Quote]:
        if self.moomoo is None or rule.condition.stable_low is None:
            return []
        found = await self.moomoo.scan_afterhours(
            rule.condition.stable_low,
            rule.condition.max_market_cap,
            max_pages=8,
        )
        minimum = rule.condition.min_regular_change
        quotes: list[Quote] = []
        for quote, _cap in found:
            if minimum is not None and (
                quote.regular_change_pct is None or quote.regular_change_pct <= minimum
            ):
                continue
            quotes.append(quote)
        logger.info("%s stable fallback from after-hours rank: %s", rule.id, len(quotes))
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
        logger.info("After-hours stable matches for %s: %s", rule.id, len(matched))
        return matched

    async def _match_trend_afterhours(self, rule: Rule) -> list[Quote]:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        max_cap = None if rule.condition.skip_market_cap else (rule.condition.max_market_cap or 100_000_000)
        found = await client.scan_afterhours(rule.condition.value, max_cap)
        candidates: list[Quote] = []
        regular_min = rule.condition.min_regular_change
        volume_min = rule.condition.min_volume
        for quote, _cap in found:
            if quote.change_pct is None or quote.change_pct <= rule.condition.value:
                continue
            if regular_min is not None and (
                quote.regular_change_pct is None or quote.regular_change_pct <= regular_min
            ):
                continue
            if volume_min is not None and (quote.volume is None or quote.volume <= volume_min):
                continue
            candidates.append(quote)
        if rule.condition.min_regular_volume is not None and candidates:
            snapshots = await self._moomoo_snapshots(client, [quote.symbol for quote in candidates])
            for quote in candidates:
                snap = snapshots.get(quote.symbol)
                if snap is not None and snap.volume is not None:
                    quote.regular_volume = snap.volume
        if rule.condition.require_uptrend:
            await self._fill_session_uptrends(candidates, rule, client)
        matched: list[Quote] = []
        for quote in candidates:
            evaluation = evaluate_afterhours_scan(
                rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
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
            logger.info("After-hours matches for %s: %s", rule.id, len(matched))
            self._scan_log[rule.id] = (signature, now)
        return matched

    async def _match_spike_rule(self, rule: Rule) -> list[Quote]:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        afterhours = rule.condition.type is ConditionType.AFTERHOURS_GAIN
        max_cap = rule.condition.max_market_cap or 100_000_000
        if afterhours:
            found = await client.scan_afterhours(rule.condition.value, max_cap)
        else:
            found = await client.scan_premarket(rule.condition.value, max_cap)
        candidates: list[Quote] = []
        volume_min = rule.condition.min_volume
        for quote, _cap in found:
            if quote.change_pct is None or quote.change_pct <= rule.condition.value:
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
            logger.info("%s pullback matches for %s: %s", session_name, rule.id, len(matched))
            self._scan_log[rule.id] = (signature, now)
        return matched

    async def _match_day_spike_rule(self, rule: Rule) -> list[Quote]:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        afterhours = rule.condition.type is ConditionType.AFTERHOURS_GAIN
        max_cap = rule.condition.max_market_cap or 100_000_000
        if afterhours:
            found = await client.scan_afterhours(rule.condition.value, max_cap)
        else:
            found = await client.scan_premarket(rule.condition.value, max_cap)
        candidates: list[Quote] = []
        volume_min = rule.condition.min_volume
        for quote, _cap in found:
            if quote.change_pct is None or quote.change_pct <= rule.condition.value:
                continue
            if volume_min is not None and (quote.volume is None or quote.volume <= volume_min):
                continue
            candidates.append(quote)
        await self._fill_recent_day_spikes(candidates, rule, client)
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
            logger.info("%s day-gain matches for %s: %s", session_name, rule.id, len(matched))
            self._scan_log[rule.id] = (signature, now)
        return matched

    async def _fill_recent_day_spikes(
        self, quotes: list[Quote], rule: Rule, _client: MoomooClient
    ) -> None:
        minimum = rule.condition.min_recent_day_change
        if minimum is None:
            return
        today = now_et().date()
        include_today = is_afterhours()
        days = recent_trading_days(
            today, rule.condition.recent_day_count, include_end=include_today
        )
        for quote in quotes:
            if (
                include_today
                and quote.regular_change_pct is not None
                and quote.regular_change_pct > minimum
            ):
                quote.recent_day_spike = True
                continue
            quote.recent_day_spike = self.store.saw_day_gain(quote.symbol, minimum, days)

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
        quotes, next_begin = await client.smallcap_page(begin, max_cap, min_volume, min_price)
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

    async def _record_circuit_breakers(self, rule: Rule) -> None:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return
        found = await client.regular_gainers(20, max_market_cap=rule.condition.max_market_cap)
        symbols = [quote.symbol for quote in found]
        snaps: dict[str, Quote] = {}
        for start in range(0, len(symbols), 200):
            snaps.update(await self._moomoo_snapshots(client, symbols[start : start + 200]))
        day = now_et().date().isoformat()
        seen_at = datetime.now(timezone.utc)
        noted: list[str] = []
        for symbol, snap in snaps.items():
            if not is_upward_halt(snap):
                continue
            if self.store.spike_seen_at(day, "upward_halt", symbol) is None:
                change = snap.change_pct
                shown = f"{change:+.1f}%" if change is not None else "n/a"
                noted.append(f"{symbol} {shown}")
            self.store.note_spike(day, "upward_halt", symbol, seen_at)
        signature = tuple(sorted(noted))
        now = datetime.now(timezone.utc)
        previous = self._scan_log.get(f"{rule.id}:halt")
        if noted and (previous is None or previous[0] != signature):
            logger.info("Noted upward halts: %s", ", ".join(noted))
            self._scan_log[f"{rule.id}:halt"] = (signature, now)

    async def _match_breaker_afterhours(self, rule: Rule) -> list[Quote]:
        client = self._trend_moomoo or self.moomoo
        if client is None:
            return []
        found = await client.scan_afterhours(rule.condition.value, rule.condition.max_market_cap)
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
                cap_filtered=rule.condition.max_market_cap is not None,
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
            logger.info("After-hours circuit breaker matches for %s: %s", rule.id, len(matched))
            self._scan_log[rule.id] = (signature, now)
        return matched

    async def _maybe_send_leaderboard(self, rule: Rule, state: dict) -> None:
        slot = afterhours_board_slot()
        if slot is None or rule.condition.top_n is None or self.moomoo is None:
            return
        slot_key = f"{rule.id}:{slot.strftime('%Y-%m-%dT%H:%M')}"
        if self.store.last_fired_at(state, slot_key) is not None:
            return
        now = datetime.now(timezone.utc)
        if self._board_retry_at is not None and now < self._board_retry_at:
            return
        max_cap = rule.condition.max_market_cap or 100_000_000
        min_volume = None if slot.time() == AFTERHOURS_START else rule.condition.min_volume
        try:
            leaders = await self.moomoo.top_afterhours(max_cap, rule.condition.top_n, min_volume)
        except Exception as exc:  # noqa: BLE001
            logger.warning("After-hours leaderboard failed: %s", exc)
            self._board_retry_at = now + timedelta(seconds=30)
            await self._note_opend_failure(exc)
            await self._note_problem(str(exc))
            return
        self._board_retry_at = None
        self._note_opend_ok()
        if len(leaders) < rule.condition.top_n:
            logger.info(
                "After-hours leaderboard %s has %s priced names, waiting",
                slot.strftime("%H:%M"),
                len(leaders),
            )
            self._board_retry_at = now + timedelta(seconds=30)
            return
        rows = [(quote.symbol, leaderboard_line(quote)) for quote in leaders]
        title = f"{slot.strftime('%H:%M')} {rule.label or 'Top 5'}"
        delivered = await self._deliver_digest(rule.id, title, rows, rule.notify)
        if not delivered:
            self._board_retry_at = datetime.now(timezone.utc) + timedelta(seconds=30)
            return
        message = "\n".join(line for _symbol, line in rows)
        alert = self.store.new_alert(
            rule_id=rule.id,
            rule_name=rule.name,
            label=rule.label,
            notify=rule.notify,
            symbol=leaders[0].symbol,
            message=message,
            price=leaders[0].price,
            change_pct=leaders[0].change_pct,
        )
        self.store.append_alert(alert)
        self.store.mark_fired(state, slot_key, alert.fired_at)
        logger.info("ALERT %s | %s", rule.id, message.replace("\n", " | "))

    async def _deliver_digest(
        self, rule_id: str, title: str, rows: list[tuple[str, str]], notify: str | None = None
    ) -> bool:
        delivered = False
        slack_url = self.routes.slack_url(notify)
        if slack_url:
            try:
                await self.slack.send_digest(title, rows, webhook_url=slack_url)
                delivered = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Slack leaderboard failed for %s: %s", rule_id, exc)
                self.last_error = f"Slack send failed: {exc}"
        token = self.routes.pushover_token(notify)
        if self.pushover is not None and token and self.settings.pushover_user:
            try:
                await self.pushover.send_digest(title, rows, token=token)
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
                cooldown_key=f"{rule.id}:{quote.symbol}",
            )
            if alert is not None:
                fired.append(alert)
        return fired

    async def _fill_session_uptrends(
        self, quotes: list[Quote], rule: Rule, client: MoomooClient
    ) -> None:
        minimum = rule.condition.min_regular_volume
        session_day = now_et().date().isoformat()
        now = datetime.now(timezone.utc)
        for quote in quotes:
            if minimum is not None and (
                quote.regular_volume is None or quote.regular_volume <= minimum
            ):
                continue
            cached = self._uptrend_cache.get((session_day, quote.symbol))
            if cached is not None:
                quote.session_uptrend = cached
                continue
            if self._kline_blocked_until is not None and now < self._kline_blocked_until:
                continue
            result = await client.session_uptrend(quote.symbol, session_day)
            await self._drain_client_problem(client)
            if result is None:
                self._kline_blocked_until = datetime.now(timezone.utc) + timedelta(seconds=31)
                break
            self._uptrend_cache[(session_day, quote.symbol)] = result
            quote.session_uptrend = result

    async def _run_moomoo_scanner(
        self,
        rule: Rule,
        state: dict,
    ) -> tuple[list[Alert], str | None]:
        if self.moomoo is None:
            return [], None
        afterhours = rule.condition.type is ConditionType.AFTERHOURS_GAIN
        max_cap = rule.condition.max_market_cap or 100_000_000
        if afterhours:
            found = await self.moomoo.scan_afterhours(rule.condition.value, max_cap)
        else:
            found = await self.moomoo.scan_premarket(rule.condition.value, max_cap)
        matched: list[tuple[Quote, float | None]] = []
        for quote, cap in found:
            if afterhours:
                evaluation = evaluate_afterhours_scan(
                    rule, quote, market_cap=cap, in_afterhours=True, cap_filtered=True
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
            logger.info("%s matches: %s", session_name, len(matched))
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
                cooldown_key=f"{rule.id}:{quote.symbol}",
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
            afterhours = rule.condition.type is ConditionType.AFTERHOURS_GAIN
            max_cap = None if rule.condition.skip_market_cap else (rule.condition.max_market_cap or 100_000_000)
            if afterhours:
                found = await self.moomoo.scan_afterhours(rule.condition.value, max_cap)
            else:
                found = await self.moomoo.scan_premarket(rule.condition.value, max_cap)
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
                "Moomoo preview %s: %s names >= %.0f%%, %s matched (not alerting)",
                rule.id,
                len(found),
                rule.condition.value,
                len(matched),
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

    async def _fire_alert(
        self,
        rule: Rule,
        quote: Quote,
        state: dict,
        extra: dict | None,
        cooldown_key: str | None = None,
    ) -> Alert | None:
        key = cooldown_key or rule.id
        if self.store.in_cooldown(state, key, rule.cooldown_minutes):
            logger.info("Rule %s matched %s but is in cooldown", rule.id, quote.symbol)
            return None
        if rule.condition.type is ConditionType.PREMARKET_GAIN and self.moomoo is not None:
            try:
                quote.after_hours_change_pct = await self.moomoo.after_hours_change(quote.symbol)
                await self._drain_client_problem(self.moomoo)
            except Exception as exc:  # noqa: BLE001
                logger.warning("After-hours change unavailable for %s: %s", quote.symbol, exc)
        alert = self.store.new_alert(
            rule_id=rule.id,
            rule_name=rule.name,
            label=rule.label,
            notify=rule.notify,
            symbol=quote.symbol,
            message=render_message(rule, quote, extra),
            price=quote.price,
            change_pct=quote.change_pct,
        )
        alert.url = quote_url(quote.symbol)
        await self._deliver(alert)
        self.store.append_alert(alert)
        self.store.mark_fired(state, key, alert.fired_at)
        logger.info("ALERT %s | %s", rule.id, alert.message)
        return alert

    def _scanner_digest(self, rule: Rule, alerts: list[Alert]) -> str:
        lines = [f":chart_with_upwards_trend: *{rule.name}*"]
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
        await self._deliver(alert)
        self.store.append_alert(alert)
        logger.info("Sent test alert for %s at %s", symbol, quote.price)
        return alert

    async def _deliver(self, alert: Alert) -> None:
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
                await self.pushover.send_alert(alert, token=token)
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
