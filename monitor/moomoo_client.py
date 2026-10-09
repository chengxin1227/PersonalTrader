from __future__ import annotations

import asyncio
import logging
import math
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

from monitor.config import Settings
from monitor.models import MarketClock, Quote
from monitor.session import ET, now_et

logger = logging.getLogger("personaltrader.moomoo")

US_OPEN_STATES = {"MORNING", "AFTERNOON"}
CIRCUIT_BREAKER_STATUSES = {
    "RECOVERABLE_CIRCUIT_BREAKER",
    "UNRECOVERABLE_CIRCUIT_BREAKER",
}
_PAGE_SIZE = 200
_MAX_PAGES = 5


def is_circuit_breaker(status: str | None) -> bool:
    return status in CIRCUIT_BREAKER_STATUSES


def is_upward_halt(quote: Quote) -> bool:
    """A gain halt pauses at the session high. A drop halt pauses at the low."""
    if not is_circuit_breaker(quote.sec_status):
        return False
    price = quote.price
    high = quote.daily_high
    if price is None or high is None or high <= 0 or price <= 0:
        return False
    if price < high * 0.999:
        return False
    low = quote.daily_low
    if low is not None and low > 0 and high > low * 1.001 and price <= low * 1.001:
        return False
    return True


def bare_symbol(code: str) -> str:
    text = str(code).strip().upper()
    if "." not in text:
        return text
    return text.split(".", 1)[1]


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _parse_et(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.split(".")[0]
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=ET)


def is_test_name(name: str) -> bool:
    return "test symbol" in name.lower()


def line_slope(values: list[float]) -> float:
    count = len(values)
    if count < 2:
        return 0.0
    x_mean = (count - 1) / 2
    y_mean = sum(values) / count
    numerator = 0.0
    denominator = 0.0
    for index, value in enumerate(values):
        dx = index - x_mean
        numerator += dx * (value - y_mean)
        denominator += dx * dx
    if denominator == 0:
        return 0.0
    return numerator / denominator


def completed_daily_changes(
    bars: list[tuple[str, float]],
    *,
    today: str,
    include_today: bool,
) -> list[float]:
    """Daily percent changes, oldest first. Premarket drops the unfinished session."""
    changes: list[float] = []
    for day, change in sorted(bars):
        if include_today:
            if day > today:
                continue
        elif day >= today:
            continue
        changes.append(change)
    return changes


def recent_daily_window(end: date, count: int) -> tuple[str, str, int]:
    """Start, end, and page size for the most recent daily bars.

    OpenD treats a missing range as the past year and returns the oldest page
    first. A short window with room for every session in it keeps the latest days.
    """
    span = max(count * 4, 21)
    start = end - timedelta(days=span)
    return start.isoformat(), end.isoformat(), span + 1


def day_gain_exceeded(changes: list[float], minimum: float, lookback: int) -> bool:
    if lookback < 1:
        return False
    return any(change > minimum for change in changes[-lookback:])


def regular_session_uptrend(bars: list[tuple[float, float]], *, minimum_bars: int = 20) -> bool:
    """True when the regular-session path rises: fitted slope > 0 and close > open."""
    if len(bars) < minimum_bars:
        return False
    open_px = bars[0][0]
    close_px = bars[-1][1]
    if open_px <= 0:
        return False
    closes = [close for _open_px, close in bars]
    return line_slope(closes) > 0 and close_px > open_px


def _is_opend_limit(message: str) -> bool:
    lowered = message.lower()
    if "quota" in lowered or "high frequency" in lowered or "too frequent" in lowered:
        return True
    return "maximum" in lowered and "times per" in lowered


def after_hours_range_pct(
    price: float | None,
    change_pct: float | None,
    high: float | None,
    low: float | None,
) -> tuple[float, float] | None:
    """After-hours low and high versus the regular close, in percent.

    `change_pct` is the after-hours change already expressed in percent, the same
    unit as `after_change_rate` on a snapshot.
    """
    if price is None or change_pct is None or price <= 0:
        return None
    if high is None or low is None or high <= 0 or low <= 0:
        return None
    denominator = 1 + change_pct / 100
    if denominator <= 0:
        return None
    base = price / denominator
    if base <= 0:
        return None
    return (
        round((low - base) / base * 100, 4),
        round((high - base) / base * 100, 4),
    )


def regular_high_change_pct(high: float | None, prev_close: float | None) -> float | None:
    """Regular-session high versus the previous close, in percent."""
    if high is None or prev_close is None or high <= 0 or prev_close <= 0:
        return None
    return round((high - prev_close) / prev_close * 100, 4)


def session_mark(row: dict[str, Any], session: str) -> tuple[float, int] | None:
    """Premarket or after-hours price and share volume from a snapshot row."""
    if session == "premarket":
        price = _float(row.get("pre_price"))
        volume = _float(row.get("pre_volume"))
    elif session == "afterhours":
        price = _float(row.get("after_price"))
        volume = _float(row.get("after_volume"))
    else:
        return None
    if price is None or price <= 0:
        return None
    return price, int(volume or 0)


def quote_from_after_hours(row: dict[str, Any]) -> Quote | None:
    symbol = bare_symbol(str(row.get("code") or ""))
    if not symbol:
        return None
    name = str(row.get("name") or "").strip()
    if is_test_name(name):
        return None
    price = _float(row.get("after_price"))
    change = _float(row.get("after_change_rate"))
    band = after_hours_range_pct(
        price,
        change,
        _float(row.get("after_high_price")),
        _float(row.get("after_low_price")),
    )
    if price is None or price <= 0 or change is None or band is None:
        return None
    volume = _float(row.get("after_volume"))
    low_pct, high_pct = band
    return Quote(
        symbol=symbol,
        company=name or None,
        price=price,
        change_pct=change,
        volume=int(volume) if volume is not None else None,
        session_low_pct=low_pct,
        session_high_pct=high_pct,
    )


def quote_from_snapshot(row: dict[str, Any]) -> Quote:
    symbol = bare_symbol(str(row.get("code") or ""))
    price = _float(row.get("last_price"))
    prev_close = _float(row.get("prev_close_price"))
    change_pct = None
    if price is not None and prev_close:
        change_pct = ((price - prev_close) / prev_close) * 100
    volume = _float(row.get("volume"))
    status = str(row.get("sec_status") or "").strip()
    if status in {"", "N/A", "NONE"}:
        status = ""
    return Quote(
        symbol=symbol,
        price=price,
        bid=_float(row.get("bid_price")),
        ask=_float(row.get("ask_price")),
        daily_open=_float(row.get("open_price")),
        daily_high=_float(row.get("high_price")),
        daily_low=_float(row.get("low_price")),
        prev_close=prev_close,
        change_pct=change_pct,
        volume=int(volume) if volume is not None else None,
        company=str(row.get("name") or "").strip() or None,
        sec_status=status or None,
        updated_at=_parse_et(row.get("update_time")),
    )


def quote_from_rank(
    row: dict[str, Any],
    *,
    price_key: str = "pre_market_price",
    change_key: str = "pre_market_change_ratio",
    volume_key: str = "pre_market_volume",
) -> Quote | None:
    name = str(row.get("name") or "")
    if is_test_name(name):
        return None
    symbol = bare_symbol(str(row.get("security") or ""))
    if not symbol:
        return None
    volume = _float(row.get(volume_key))
    return Quote(
        symbol=symbol,
        price=_float(row.get(price_key)),
        prev_close=_float(row.get("close_price")),
        change_pct=_float(row.get(change_key)),
        regular_change_pct=_float(row.get("change_ratio")),
        volume=int(volume) if volume is not None else None,
        company=name.strip() or None,
    )


def quote_from_filter(item: Any) -> Quote | None:
    name = str(getattr(item, "stock_name", "") or "")
    if is_test_name(name):
        return None
    symbol = bare_symbol(str(getattr(item, "stock_code", "") or ""))
    if not symbol:
        return None
    fields = getattr(item, "__dict__", {})
    price = _float(fields.get("cur_price"))
    volume = _float(fields.get(("volume", 1)))
    return Quote(
        symbol=symbol,
        company=name.strip() or None,
        price=price,
        volume=int(volume) if volume is not None else None,
    )


def afterhours_leaders(
    quotes: list[Quote],
    limit: int,
    min_volume: int | None = None,
) -> list[Quote]:
    """Skip zero prices, and thin names after the after-hours open."""
    ready: list[Quote] = []
    for quote in quotes:
        if quote.price is None or quote.price <= 0:
            continue
        if min_volume is not None and (quote.volume is None or quote.volume <= min_volume):
            continue
        ready.append(quote)
        if len(ready) >= limit:
            break
    return ready


def total_market_cap(
    *,
    total_market_val: float | None,
    shares: float | None,
    last_price: float | None,
) -> float | None:
    if total_market_val and total_market_val > 0:
        return total_market_val
    if shares and last_price:
        return shares * last_price
    return None


def yesterday_cap(
    *,
    shares: float | None,
    prev_close: float | None,
    total_market_val: float | None,
    last_price: float | None,
) -> float | None:
    if shares and prev_close:
        return shares * prev_close
    if total_market_val and last_price and prev_close and last_price > 0:
        return total_market_val * prev_close / last_price
    return None


class MoomooClient:
    def __init__(self, settings: Settings) -> None:
        self._host = settings.moomoo_host
        self._port = settings.moomoo_port
        self._ctx: Any = None
        self._lock = threading.RLock()
        self._pending_problem: str | None = None

    def _remember_problem(self, message: str) -> None:
        with self._lock:
            self._pending_problem = str(message)

    def take_problem(self) -> str | None:
        with self._lock:
            message = self._pending_problem
            self._pending_problem = None
            return message

    def _context(self) -> Any:
        if self._ctx is None:
            from moomoo import OpenQuoteContext

            self._ctx = OpenQuoteContext(host=self._host, port=self._port)
        return self._ctx

    def _close_sync(self) -> None:
        with self._lock:
            if self._ctx is not None:
                self._ctx.close()
                self._ctx = None

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _snapshot_rows(self, context: Any, codes: list[str]) -> list[dict[str, Any]]:
        from moomoo import RET_OK

        if not codes:
            return []
        ret, data = context.get_market_snapshot(codes)
        if ret == RET_OK:
            return data.to_dict(orient="records")
        message = str(data)
        if _is_opend_limit(message):
            self._remember_problem(message)
            logger.warning("Moomoo snapshot paused: %s", message)
            return []
        if len(codes) == 1:
            logger.warning("Skip %s: %s", codes[0], message)
            return []
        blocked = [
            code
            for code in codes
            if f" {bare_symbol(code)}." in f" {message}" or message.endswith(bare_symbol(code))
        ]
        if blocked and len(blocked) < len(codes):
            logger.warning("Moomoo skipped %s", ", ".join(bare_symbol(code) for code in blocked))
            kept = [code for code in codes if code not in blocked]
            return self._snapshot_rows(context, kept)
        middle = len(codes) // 2
        return self._snapshot_rows(context, codes[:middle]) + self._snapshot_rows(
            context, codes[middle:]
        )

    def _snapshots_sync(self, symbols: list[str]) -> dict[str, Quote]:
        codes = [f"US.{symbol.strip().upper()}" for symbol in symbols if symbol.strip()]
        if not codes:
            return {}
        with self._lock:
            rows = self._snapshot_rows(self._context(), codes)
        quotes: dict[str, Quote] = {}
        for row in rows:
            quote = quote_from_snapshot(row)
            if quote.symbol:
                quotes[quote.symbol] = quote
        return quotes

    async def snapshots(self, symbols: list[str]) -> dict[str, Quote]:
        return await asyncio.to_thread(self._snapshots_sync, symbols)

    def _after_hours_change_sync(self, symbol: str) -> float | None:
        code = f"US.{symbol.strip().upper()}"
        with self._lock:
            rows = self._snapshot_rows(self._context(), [code])
        if not rows:
            return None
        row = rows[0]
        price = _float(row.get("after_price"))
        change = _float(row.get("after_change_rate"))
        if price is None or price <= 0 or change is None:
            return None
        return change

    async def after_hours_change(self, symbol: str) -> float | None:
        return await asyncio.to_thread(self._after_hours_change_sync, symbol)

    def _clock_sync(self) -> MarketClock:
        from moomoo import RET_OK

        with self._lock:
            ret, data = self._context().get_global_state()
        if ret != RET_OK:
            raise RuntimeError(f"Moomoo clock failed: {data}")
        raw_ts = data.get("local_timestamp") or data.get("timestamp")
        timestamp = None
        if raw_ts is not None:
            timestamp = datetime.fromtimestamp(float(raw_ts), tz=timezone.utc)
        return MarketClock(
            is_open=str(data.get("market_us") or "") in US_OPEN_STATES,
            timestamp=timestamp,
        )

    async def clock(self) -> MarketClock:
        return await asyncio.to_thread(self._clock_sync)

    def _scan_sync(
        self,
        min_change_pct: float,
        session: str = "premarket",
        max_market_cap: float | None = None,
        limit: int | None = None,
        max_pages: int = _MAX_PAGES,
    ) -> list[tuple[Quote, float | None]]:
        from moomoo import RET_OK, SimpleRankFilter, SimpleRankIndicatorType

        filter_list = None
        if max_market_cap is not None:
            filter_list = [
                SimpleRankFilter(SimpleRankIndicatorType.MARKET_CAP, interval_max=max_market_cap)
            ]
        if session == "afterhours":
            rank_method = "get_us_after_hours_rank"
            change_key = "after_hours_change_ratio"
            price_key = "after_hours_price"
            volume_key = "after_hours_volume"
            failure = "Moomoo after-hours rank failed"
        else:
            rank_method = "get_us_pre_market_rank"
            change_key = "pre_market_change_ratio"
            price_key = "pre_market_price"
            volume_key = "pre_market_volume"
            failure = "Moomoo pre-market rank failed"

        quotes: list[Quote] = []
        offset = 0
        with self._lock:
            context = self._context()
            for _page in range(max_pages):
                ret, data = getattr(context, rank_method)(
                    count=_PAGE_SIZE, offset=offset, filter_list=filter_list
                )
                if ret != RET_OK:
                    raise RuntimeError(f"{failure}: {data}")
                all_count, frame = data
                if frame is None or frame.empty:
                    break
                stop = False
                for row in frame.to_dict(orient="records"):
                    change = _float(row.get(change_key))
                    if change is None:
                        continue
                    if change < min_change_pct:
                        stop = True
                        break
                    quote = quote_from_rank(
                        row,
                        price_key=price_key,
                        change_key=change_key,
                        volume_key=volume_key,
                    )
                    if quote is not None:
                        quotes.append(quote)
                        if limit is not None and len(quotes) >= limit:
                            stop = True
                            break
                offset += len(frame)
                if stop or offset >= int(all_count):
                    break
        if not quotes:
            return []
        return [(quote, None) for quote in quotes]

    async def scan_premarket(
        self, min_change_pct: float, max_market_cap: float
    ) -> list[tuple[Quote, float | None]]:
        logger.info(
            "Moomoo pre-market rank, gain >= %.1f%%, total cap <= %.0f",
            min_change_pct,
            max_market_cap,
        )
        found = await asyncio.to_thread(self._scan_sync, min_change_pct, "premarket", max_market_cap)
        logger.info("Moomoo pre-market names above threshold: %s", len(found))
        return found

    async def scan_afterhours(
        self,
        min_change_pct: float,
        max_market_cap: float | None = None,
        max_pages: int = _MAX_PAGES,
    ) -> list[tuple[Quote, float | None]]:
        if max_market_cap is None:
            logger.info("Moomoo after-hours rank, gain >= %.1f%%", min_change_pct)
        else:
            logger.info(
                "Moomoo after-hours rank, gain >= %.1f%%, total cap <= %.0f",
                min_change_pct,
                max_market_cap,
            )
        found = await asyncio.to_thread(
            self._scan_sync,
            min_change_pct,
            "afterhours",
            max_market_cap,
            None,
            max_pages,
        )
        logger.info("Moomoo after-hours names above threshold: %s", len(found))
        return found

    def _after_hours_quotes_sync(self, symbols: list[str]) -> dict[str, Quote] | None:
        codes = [f"US.{symbol.strip().upper()}" for symbol in symbols if symbol.strip()]
        if not codes:
            return {}
        quotes: dict[str, Quote] = {}
        with self._lock:
            context = self._context()
            for start in range(0, len(codes), _PAGE_SIZE):
                chunk = codes[start : start + _PAGE_SIZE]
                pending = self._pending_problem
                rows = self._snapshot_rows(context, chunk)
                if (
                    not rows
                    and self._pending_problem
                    and self._pending_problem != pending
                    and _is_opend_limit(self._pending_problem)
                ):
                    return None
                for row in rows:
                    quote = quote_from_after_hours(row)
                    if quote is not None and quote.symbol:
                        quotes[quote.symbol] = quote
        return quotes

    async def after_hours_quotes(self, symbols: list[str]) -> dict[str, Quote] | None:
        return await asyncio.to_thread(self._after_hours_quotes_sync, symbols)

    def _session_marks_sync(self, symbols: list[str], session: str) -> dict[str, tuple[float, int]] | None:
        codes = [f"US.{symbol.strip().upper()}" for symbol in symbols if symbol.strip()]
        if not codes:
            return {}
        marks: dict[str, tuple[float, int]] = {}
        with self._lock:
            context = self._context()
            for start in range(0, len(codes), _PAGE_SIZE):
                chunk = codes[start : start + _PAGE_SIZE]
                pending = self._pending_problem
                rows = self._snapshot_rows(context, chunk)
                if (
                    not rows
                    and self._pending_problem
                    and self._pending_problem != pending
                    and _is_opend_limit(self._pending_problem)
                ):
                    return None
                for row in rows:
                    mark = session_mark(row, session)
                    symbol = bare_symbol(str(row.get("code") or ""))
                    if mark is not None and symbol:
                        marks[symbol] = mark
        return marks

    async def session_marks(
        self, symbols: list[str], session: str
    ) -> dict[str, tuple[float, int]] | None:
        return await asyncio.to_thread(self._session_marks_sync, symbols, session)

    async def top_afterhours(
        self,
        max_market_cap: float,
        limit: int,
        min_volume: int | None = None,
    ) -> list[Quote]:
        if min_volume is None:
            logger.info(
                "Moomoo after-hours rank, top %s, total cap <= %.0f",
                limit,
                max_market_cap,
            )
            page_limit = _PAGE_SIZE
        else:
            logger.info(
                "Moomoo after-hours rank, top %s, total cap <= %.0f, volume > %s",
                limit,
                max_market_cap,
                min_volume,
            )
            page_limit = _PAGE_SIZE * _MAX_PAGES
        found = await asyncio.to_thread(
            self._scan_sync, -1_000_000, "afterhours", max_market_cap, page_limit
        )
        return afterhours_leaders([quote for quote, _cap in found], limit, min_volume)

    def _session_uptrend_sync(self, symbol: str, session_day: str) -> bool | None:
        from moomoo import RET_OK, AuType, KLType, Session

        code = f"US.{symbol.strip().upper()}"
        with self._lock:
            ret, data, _page = self._context().request_history_kline(
                code,
                start=f"{session_day} 09:30:00",
                end=f"{session_day} 16:00:00",
                ktype=KLType.K_5M,
                autype=AuType.NONE,
                max_count=120,
                session=Session.RTH,
            )
        if ret != RET_OK:
            message = str(data)
            self._remember_problem(message)
            if "frequency" in message or "quota" in message.lower():
                logger.warning("Kline quota hit while checking %s", symbol)
                return None
            logger.warning("Kline failed for %s: %s", symbol, message)
            return False
        if data is None or data.empty:
            return False
        start = datetime.strptime(f"{session_day} 09:30:00", "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(f"{session_day} 16:00:00", "%Y-%m-%d %H:%M:%S")
        bars: list[tuple[float, float]] = []
        for row in data.to_dict(orient="records"):
            raw_time = str(row.get("time_key") or "").split(".")[0]
            try:
                stamp = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if stamp < start or stamp >= end:
                continue
            open_px = _float(row.get("open"))
            close_px = _float(row.get("close"))
            if open_px is None or close_px is None:
                continue
            bars.append((open_px, close_px))
        return regular_session_uptrend(bars)

    async def session_uptrend(self, symbol: str, session_day: str) -> bool | None:
        return await asyncio.to_thread(self._session_uptrend_sync, symbol, session_day)

    def _recent_daily_changes_sync(self, symbol: str, count: int) -> list[tuple[str, float]] | None:
        from moomoo import RET_OK, AuType, KLType

        code = f"US.{symbol.strip().upper()}"
        start, end, limit = recent_daily_window(now_et().date(), count)
        with self._lock:
            ret, data, _page = self._context().request_history_kline(
                code,
                start=start,
                end=end,
                ktype=KLType.K_DAY,
                autype=AuType.NONE,
                max_count=limit,
            )
        if ret != RET_OK:
            message = str(data)
            self._remember_problem(message)
            lowered = message.lower()
            if "frequency" in lowered or "quota" in lowered:
                logger.warning("Kline quota hit while checking %s", symbol)
                return None
            logger.warning("Daily kline failed for %s: %s", symbol, message)
            return None
        if data is None or data.empty:
            return []
        bars: list[tuple[str, float]] = []
        for row in data.to_dict(orient="records"):
            day = str(row.get("time_key") or "")[:10]
            change = _float(row.get("change_rate"))
            if change is None:
                close_px = _float(row.get("close"))
                last_close = _float(row.get("last_close"))
                if close_px is not None and last_close is not None and last_close > 0:
                    change = (close_px - last_close) / last_close * 100
            if day and change is not None:
                bars.append((day, change))
        return bars

    async def recent_daily_changes(self, symbol: str, count: int) -> list[tuple[str, float]] | None:
        return await asyncio.to_thread(self._recent_daily_changes_sync, symbol, count)

    def _regular_gainers_sync(
        self,
        min_change_pct: float,
        max_pages: int = 15,
        max_market_cap: float | None = None,
    ) -> list[Quote]:
        from moomoo import RET_OK, AccumulateFilter, Market, SortDir, StockField

        acc = AccumulateFilter()
        acc.stock_field = StockField.CHANGE_RATE
        acc.filter_min = min_change_pct
        acc.is_no_filter = False
        acc.sort = SortDir.DESCEND
        acc.days = 1
        filters = [acc]
        if max_market_cap is not None:
            from moomoo import SimpleFilter

            cap = SimpleFilter()
            cap.stock_field = StockField.MARKET_VAL
            cap.filter_max = max_market_cap
            cap.is_no_filter = False
            filters.append(cap)
        quotes: list[Quote] = []
        begin = 0
        with self._lock:
            context = self._context()
            for _page in range(max_pages):
                ret, data = context.get_stock_filter(Market.US, filters, begin=begin, num=_PAGE_SIZE)
                if ret != RET_OK:
                    raise RuntimeError(f"Moomoo regular-session screen failed: {data}")
                last_page, all_count, rows = data
                if not rows:
                    break
                stop = False
                for item in rows:
                    change = _float(item.__dict__.get(("change_rate", 1)))
                    if change is None:
                        continue
                    if change <= min_change_pct:
                        stop = True
                        break
                    name = str(getattr(item, "stock_name", "") or "")
                    if is_test_name(name):
                        continue
                    symbol = bare_symbol(str(getattr(item, "stock_code", "") or ""))
                    if not symbol:
                        continue
                    quotes.append(
                        Quote(symbol=symbol, company=name.strip() or None, change_pct=change)
                    )
                begin += len(rows)
                if stop or last_page or begin >= int(all_count):
                    break
        return quotes

    async def regular_gainers(
        self,
        min_change_pct: float,
        max_pages: int = 15,
        max_market_cap: float | None = None,
    ) -> list[Quote]:
        if max_market_cap is None:
            logger.info("Moomoo regular-session screen, gain > %.1f%%", min_change_pct)
        else:
            logger.info(
                "Moomoo regular-session screen, gain > %.1f%%, total cap <= %.0f",
                min_change_pct,
                max_market_cap,
            )
        found = await asyncio.to_thread(
            self._regular_gainers_sync, min_change_pct, max_pages, max_market_cap
        )
        logger.info("Moomoo regular-session names above threshold: %s", len(found))
        return found

    def _smallcap_page_sync(
        self,
        begin: int,
        max_market_cap: float,
        min_volume: float | None,
        min_price: float | None = None,
    ) -> tuple[list[Quote], int]:
        from moomoo import RET_OK, Market, SimpleFilter, StockField

        cap = SimpleFilter()
        cap.stock_field = StockField.MARKET_VAL
        cap.filter_max = max_market_cap
        cap.is_no_filter = False
        price = SimpleFilter()
        price.stock_field = StockField.CUR_PRICE
        price.filter_min = 0 if min_price is None else min_price
        price.is_no_filter = False
        filters: list[Any] = [cap, price]
        if min_volume is not None:
            from moomoo import AccumulateFilter

            volume = AccumulateFilter()
            volume.stock_field = StockField.VOLUME
            volume.days = 1
            volume.filter_min = min_volume
            volume.is_no_filter = False
            filters.append(volume)
        with self._lock:
            ret, data = self._context().get_stock_filter(
                Market.US, filters, begin=begin, num=_PAGE_SIZE
            )
        if ret != RET_OK:
            raise RuntimeError(f"Moomoo last-hour screen failed: {data}")
        last_page, all_count, rows = data
        quotes: list[Quote] = []
        for item in rows or []:
            quote = quote_from_filter(item)
            if quote is not None:
                quotes.append(quote)
        next_begin = begin + len(rows or [])
        if not rows or last_page or next_begin >= int(all_count):
            next_begin = 0
        return quotes, next_begin

    async def smallcap_page(
        self,
        begin: int,
        max_market_cap: float,
        min_volume: float | None = None,
        min_price: float | None = None,
    ) -> tuple[list[Quote], int]:
        return await asyncio.to_thread(
            self._smallcap_page_sync, begin, max_market_cap, min_volume, min_price
        )
