from __future__ import annotations

import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import certifi
import websockets

from monitor.alpaca import _parse_time
from monitor.config import Settings

logger = logging.getLogger("personaltrader.stream")

STREAM_PATHS = {
    "iex": "/v2/iex",
    "sip": "/v2/sip",
    "delayed_sip": "/v2/delayed_sip",
    "boats": "/v1beta1/boats",
    "overnight": "/v1beta1/overnight",
}


@dataclass(slots=True)
class StreamEvent:
    kind: str
    symbol: str
    price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    at: Optional[datetime] = None


def stream_url(feed: str, base: str = "wss://stream.data.alpaca.markets") -> str:
    path = STREAM_PATHS.get(feed, f"/v2/{feed}")
    return f"{base.rstrip('/')}{path}"


def parse_stream_messages(payload: Any) -> tuple[list[StreamEvent], list[dict[str, Any]]]:
    messages = payload if isinstance(payload, list) else [payload]
    events: list[StreamEvent] = []
    control: list[dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        kind = item.get("T")
        if kind == "t":
            symbol = str(item.get("S") or "").upper()
            price = item.get("p")
            if not symbol or price is None:
                continue
            events.append(
                StreamEvent(
                    kind="trade",
                    symbol=symbol,
                    price=float(price),
                    at=_parse_time(item.get("t")),
                )
            )
        elif kind == "q":
            symbol = str(item.get("S") or "").upper()
            if not symbol:
                continue
            bid = item.get("bp")
            ask = item.get("ap")
            events.append(
                StreamEvent(
                    kind="quote",
                    symbol=symbol,
                    bid=float(bid) if bid is not None else None,
                    ask=float(ask) if ask is not None else None,
                    at=_parse_time(item.get("t")),
                )
            )
        else:
            control.append(item)
    return events, control


class AlpacaStream:
    def __init__(self, settings: Settings, feed: str) -> None:
        self._settings = settings
        self._feed = feed
        self.subscribed_all_trades = False

    async def listen(
        self,
        *,
        trades: list[str],
        quotes: list[str],
        on_events: Callable[[list[StreamEvent]], Awaitable[None]],
        should_continue: Callable[[], bool],
    ) -> None:
        url = stream_url(self._feed)
        logger.info("Connecting %s", url)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        async with websockets.connect(
            url, ping_interval=20, ping_timeout=20, ssl=ssl_context
        ) as ws:
            await self._authenticate(ws)
            await self._subscribe(ws, trades, quotes)
            async for raw in ws:
                if not should_continue():
                    break
                payload = json.loads(raw)
                events, control = parse_stream_messages(payload)
                for item in control:
                    if item.get("T") == "error":
                        raise RuntimeError(item.get("msg") or "Alpaca stream error")
                    if item.get("T") == "subscription":
                        trade_subs = item.get("trades") or []
                        self.subscribed_all_trades = "*" in trade_subs
                        logger.info("Subscribed trades=%s quotes=%s", trade_subs, item.get("quotes"))
                if events:
                    await on_events(events)

    async def _authenticate(self, ws: Any) -> None:
        hello = json.loads(await ws.recv())
        _, control = parse_stream_messages(hello)
        logger.info("Stream hello: %s", control)
        await ws.send(
            json.dumps(
                {
                    "action": "auth",
                    "key": self._settings.alpaca_api_key,
                    "secret": self._settings.alpaca_api_secret,
                }
            )
        )
        reply = json.loads(await ws.recv())
        _, control = parse_stream_messages(reply)
        messages = [item.get("msg") for item in control]
        if "authenticated" not in messages:
            raise RuntimeError(f"Alpaca stream auth failed: {control}")
        logger.info("Alpaca stream authenticated")

    async def _subscribe(self, ws: Any, trades: list[str], quotes: list[str]) -> None:
        body: dict[str, Any] = {"action": "subscribe"}
        if trades:
            body["trades"] = trades
        if quotes:
            body["quotes"] = quotes
        await ws.send(json.dumps(body))
        reply = json.loads(await ws.recv())
        _events, control = parse_stream_messages(reply)
        for item in control:
            if item.get("T") == "error":
                raise RuntimeError(item.get("msg") or "subscribe failed")
            if item.get("T") == "subscription":
                trade_subs = item.get("trades") or []
                self.subscribed_all_trades = "*" in trade_subs
                logger.info("Subscribed trades=%s quotes=%s", trade_subs, item.get("quotes"))
