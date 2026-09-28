from __future__ import annotations

import logging
from typing import Optional

import httpx

from monitor.models import Quote

logger = logging.getLogger("personaltrader.marketcap")
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


def yesterday_market_cap(
    *,
    prev_close: float | None,
    price: float | None,
    shares_outstanding: float | None,
    current_market_cap: float | None,
) -> float | None:
    if prev_close and shares_outstanding:
        return float(shares_outstanding) * float(prev_close)
    if current_market_cap and price and prev_close and price > 0:
        return float(current_market_cap) * float(prev_close) / float(price)
    return None


def yesterday_market_cap_for_quote(
    quote: Quote,
    shares_outstanding: float | None,
    current_market_cap: float | None,
) -> float | None:
    return yesterday_market_cap(
        prev_close=quote.prev_close,
        price=quote.price,
        shares_outstanding=shares_outstanding,
        current_market_cap=current_market_cap,
    )


class MarketCapClient:
    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=20.0)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fundamentals(self, symbols: list[str]) -> dict[str, dict[str, float]]:
        found: dict[str, dict[str, float]] = {}
        for index in range(0, len(symbols), 40):
            batch = symbols[index : index + 40]
            if not batch:
                continue
            try:
                response = await self._client.get(
                    YAHOO_QUOTE_URL,
                    params={"symbols": ",".join(batch)},
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                )
                response.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Market cap lookup failed for %s: %s", ",".join(batch), exc)
                continue
            payload = response.json()
            results = ((payload.get("quoteResponse") or {}).get("result")) or []
            for item in results:
                symbol = str(item.get("symbol") or "").upper()
                if not symbol:
                    continue
                entry: dict[str, float] = {}
                if item.get("sharesOutstanding"):
                    entry["shares"] = float(item["sharesOutstanding"])
                if item.get("marketCap"):
                    entry["market_cap"] = float(item["marketCap"])
                if entry:
                    found[symbol] = entry
        return found
