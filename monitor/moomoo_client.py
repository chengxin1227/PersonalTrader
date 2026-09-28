from __future__ import annotations

import asyncio
import logging
import math
import threading
from datetime import datetime, timezone
from typing import Any

from monitor.config import Settings
from monitor.models import MarketClock, Quote
from monitor.session import ET

logger = logging.getLogger("personaltrader.moomoo")

US_OPEN_STATES = {"MORNING", "AFTERNOON"}
_PAGE_SIZE = 200
_MAX_PAGES = 5


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


def quote_from_snapshot(row: dict[str, Any]) -> Quote:
    symbol = bare_symbol(str(row.get("code") or ""))
    price = _float(row.get("last_price"))
    prev_close = _float(row.get("prev_close_price"))
    change_pct = None
    if price is not None and prev_close:
        change_pct = ((price - prev_close) / prev_close) * 100
    volume = _float(row.get("volume"))
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
        volume=int(volume) if volume is not None else None,
    )


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
            for _page in range(_MAX_PAGES):
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
        self, min_change_pct: float, max_market_cap: float
    ) -> list[tuple[Quote, float | None]]:
        logger.info(
            "Moomoo after-hours rank, gain >= %.1f%%, total cap <= %.0f",
            min_change_pct,
            max_market_cap,
        )
        found = await asyncio.to_thread(self._scan_sync, min_change_pct, "afterhours", max_market_cap)
        logger.info("Moomoo after-hours names above threshold: %s", len(found))
        return found
