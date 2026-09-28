from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from monitor.config import Settings
from monitor.models import MarketClock, Quote

logger = logging.getLogger("personaltrader.alpaca")


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def quote_from_snapshot(symbol: str, payload: dict[str, Any]) -> Quote:
    trade = payload.get("latestTrade") or {}
    quote = payload.get("latestQuote") or {}
    daily = payload.get("dailyBar") or {}
    prev = payload.get("prevDailyBar") or {}

    price = trade.get("p") or daily.get("c")
    prev_close = prev.get("c")
    change_pct = None
    if price is not None and prev_close:
        change_pct = ((float(price) - float(prev_close)) / float(prev_close)) * 100

    trade_at = _parse_time(trade.get("t"))
    quote_at = _parse_time(quote.get("t"))
    updated = trade_at or quote_at or _parse_time(daily.get("t"))

    volume = daily.get("v")
    return Quote(
        symbol=symbol.upper(),
        price=float(price) if price is not None else None,
        bid=float(quote["bp"]) if quote.get("bp") is not None else None,
        ask=float(quote["ap"]) if quote.get("ap") is not None else None,
        daily_open=float(daily["o"]) if daily.get("o") is not None else None,
        daily_high=float(daily["h"]) if daily.get("h") is not None else None,
        daily_low=float(daily["l"]) if daily.get("l") is not None else None,
        prev_close=float(prev_close) if prev_close is not None else None,
        change_pct=change_pct,
        volume=int(volume) if volume is not None else None,
        updated_at=updated,
        trade_at=trade_at,
        quote_at=quote_at,
    )


class AlpacaClient:
    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": self._settings.alpaca_api_secret,
        }

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def snapshots(self, symbols: list[str], feed: str) -> dict[str, Quote]:
        if not symbols:
            return {}
        url = f"{self._settings.alpaca_data_url.rstrip('/')}/v2/stocks/snapshots"
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                response = await self._client.get(
                    url,
                    headers=self._headers(),
                    params={"symbols": ",".join(symbols), "feed": feed},
                )
                response.raise_for_status()
                last_error = None
                break
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if feed != "iex" and exc.response.status_code in {403, 422}:
                    return await self.snapshots(symbols, "iex")
                if exc.response.status_code == 429:
                    wait = min(8 * (attempt + 1), 30)
                    logger.warning("Alpaca rate limit; waiting %ss", wait)
                    await asyncio.sleep(wait)
                    continue
                raise
        if last_error is not None:
            raise last_error
        payload = response.json()
        quotes: dict[str, Quote] = {}
        for symbol, snapshot in payload.items():
            if isinstance(snapshot, dict):
                quotes[symbol.upper()] = quote_from_snapshot(symbol, snapshot)
        return quotes

    async def snapshots_many(
        self,
        symbols: list[str],
        feed: str,
        batch_size: int = 80,
        pause_seconds: float = 0.4,
    ) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        batches = [symbols[index : index + batch_size] for index in range(0, len(symbols), batch_size)]
        logger.info("Fetching %s snapshot batches (%s symbols)", len(batches), len(symbols))
        for index, batch in enumerate(batches, start=1):
            try:
                quotes.update(await self.snapshots(batch, feed))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Snapshot batch %s/%s failed: %s", index, len(batches), exc)
            if index < len(batches):
                await asyncio.sleep(pause_seconds)
        return quotes

    async def list_us_equities(self) -> list[dict[str, str]]:
        url = f"{self._settings.alpaca_trade_url.rstrip('/')}/v2/assets"
        response = await self._client.get(
            url,
            headers=self._headers(),
            params={"status": "active", "asset_class": "us_equity"},
        )
        response.raise_for_status()
        assets = []
        for item in response.json():
            if not item.get("tradable"):
                continue
            symbol = str(item.get("symbol") or "").upper()
            exchange = str(item.get("exchange") or "").upper()
            if not symbol:
                continue
            assets.append({"symbol": symbol, "exchange": exchange})
        return assets

    async def clock(self) -> MarketClock:
        url = f"{self._settings.alpaca_trade_url.rstrip('/')}/v2/clock"
        response = await self._client.get(url, headers=self._headers())
        response.raise_for_status()
        payload = response.json()
        return MarketClock(
            is_open=bool(payload.get("is_open")),
            next_open=_parse_time(payload.get("next_open")),
            next_close=_parse_time(payload.get("next_close")),
            timestamp=_parse_time(payload.get("timestamp")),
        )
