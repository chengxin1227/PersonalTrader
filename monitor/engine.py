from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from monitor.alpaca import AlpacaClient
from monitor.config import Settings
from monitor.marketcap import MarketCapClient
from monitor.moomoo_client import MoomooClient
from monitor.models import Alert, ConditionType, MonitorStatus, Quote, Rule, RulesConfig
from monitor.quotes import apply_quote, apply_trade, previous_price
from monitor.rules import (
    evaluate_afterhours_scan,
    evaluate_premarket_scan,
    evaluate_rule,
    load_rules,
    render_message,
    save_rules,
)
from monitor.session import (
    extended_session_open,
    is_afterhours,
    is_premarket,
    is_trading_day,
    now_et,
    premarket_mark,
    seconds_until_extended_open,
)
from monitor.slack import SlackClient
from monitor.store import Store
from monitor.stream import AlpacaStream, StreamEvent
from monitor.universe import UniverseCache

logger = logging.getLogger("personaltrader.monitor")


class MonitorEngine:
    def __init__(
        self,
        settings: Settings,
        alpaca: AlpacaClient,
        slack: SlackClient,
        store: Store,
        verbose: bool = False,
        moomoo: MoomooClient | None = None,
    ) -> None:
        self.settings = settings
        self.alpaca = alpaca
        self.moomoo = moomoo
        self.slack = slack
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
            self.market = None

        if self.market and not self.market.is_open and not config.poll_when_closed:
            self.last_poll_at = datetime.now(timezone.utc)
            self.last_error = None
            return []

        quotes: dict[str, Quote] = {}
        if symbols:
            if self.moomoo is not None:
                quotes = await self.moomoo.snapshots(symbols)
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
            for rule in config.rules:
                if not rule.enabled or not rule.is_scanner or not self._rule_session_open(rule):
                    continue
                scanned, scan_error = await self._run_scanner(rule, config, state)
                fired.extend(scanned)
                slack_error = slack_error or scan_error
            self._last_scan_at = datetime.now(timezone.utc)

        for symbol, quote in quotes.items():
            self.store.set_last_price(state, symbol, quote.price)
        self.store.save_state(state)
        self.last_poll_at = datetime.now(timezone.utc)
        self.last_error = slack_error
        return fired

    async def run_forever(self, transport: str = "rest") -> None:
        if transport == "rest":
            await self._run_rest_loop()
            return
        await self._run_stream_loop()

    async def _run_rest_loop(self) -> None:
        self._running = True
        logger.info("Monitor loop started (REST poll)")
        idle_logged = False
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
                    await asyncio.sleep(30)
                    continue
                logger.exception("Poll failed: %s", exc)
            await asyncio.sleep(interval)

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
            max_cap = rule.condition.max_market_cap or 100_000_000
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
        alert = self.store.new_alert(
            rule_id=rule.id,
            rule_name=rule.name,
            symbol=quote.symbol,
            message=render_message(rule, quote, extra),
            price=quote.price,
            change_pct=quote.change_pct,
        )
        try:
            await self.slack.send_alert(alert)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slack send failed for %s: %s", rule.id, exc)
            self.last_error = f"Slack send failed: {exc}"
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
            quotes = await self.moomoo.snapshots([symbol])
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
        await self.slack.send_alert(alert)
        self.store.append_alert(alert)
        logger.info("Sent test alert for %s at %s", symbol, quote.price)
        return alert

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
