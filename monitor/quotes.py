from __future__ import annotations

from datetime import datetime

from monitor.models import Quote


def apply_trade(quote: Quote | None, symbol: str, price: float, at: datetime | None) -> Quote:
    current = quote or Quote(symbol=symbol.upper())
    previous = current.price
    current.symbol = symbol.upper()
    current.price = price
    current.trade_at = at
    current.updated_at = at or current.updated_at
    if current.prev_close:
        current.change_pct = ((price - current.prev_close) / current.prev_close) * 100
    current._previous_price = previous  # type: ignore[attr-defined]
    return current


def apply_quote(
    quote: Quote | None,
    symbol: str,
    bid: float | None,
    ask: float | None,
    at: datetime | None,
) -> Quote:
    current = quote or Quote(symbol=symbol.upper())
    current.symbol = symbol.upper()
    current.bid = bid
    current.ask = ask
    current.quote_at = at
    current.updated_at = at or current.updated_at
    if current.price is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2
        current.price = mid
        if current.prev_close:
            current.change_pct = ((mid - current.prev_close) / current.prev_close) * 100
    return current


def previous_price(quote: Quote) -> float | None:
    value = getattr(quote, "_previous_price", None)
    return float(value) if value is not None else None
