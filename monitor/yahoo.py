from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from monitor.models import Quote

logger = logging.getLogger(__name__)

# Yahoo index tickers keep a leading ^; we store the bare code on Quote.symbol.
_INDEX_ALIASES = {
    "IXIC": "^IXIC",
    "^IXIC": "^IXIC",
    "NDX": "^NDX",
    "^NDX": "^NDX",
    "DJI": "^DJI",
    "^DJI": "^DJI",
    "GSPC": "^GSPC",
    "^GSPC": "^GSPC",
}


def yahoo_ticker(symbol: str) -> str:
    key = symbol.strip().upper()
    return _INDEX_ALIASES.get(key, key)


def uses_yahoo_quote(symbol: str) -> bool:
    key = symbol.strip().upper()
    return key in _INDEX_ALIASES or key.startswith("^")


def _quote_sync(symbol: str) -> Quote | None:
    import yfinance as yf

    ticker = yahoo_ticker(symbol)
    bare = ticker.lstrip("^") or ticker
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Yahoo quote failed for %s: %s", ticker, exc)
        return None
    price = info.get("regularMarketPrice")
    prev = info.get("regularMarketPreviousClose")
    change = info.get("regularMarketChangePercent")
    if change is None and price is not None and prev:
        try:
            change = (float(price) - float(prev)) / float(prev) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            change = None
    if price is None and change is None:
        logger.warning("Yahoo quote empty for %s", ticker)
        return None
    try:
        price_f = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_f = None
    try:
        prev_f = float(prev) if prev is not None else None
    except (TypeError, ValueError):
        prev_f = None
    try:
        change_f = float(change) if change is not None else None
    except (TypeError, ValueError):
        change_f = None
    name = str(info.get("shortName") or info.get("longName") or "").strip() or None
    return Quote(
        symbol=bare,
        company=name,
        price=price_f,
        prev_close=prev_f,
        change_pct=change_f,
        regular_price=price_f,
        regular_change_pct=change_f,
        updated_at=datetime.now(timezone.utc),
    )


async def quote(symbol: str) -> Quote | None:
    return await asyncio.to_thread(_quote_sync, symbol)
