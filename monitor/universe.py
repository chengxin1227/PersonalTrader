from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from monitor.alpaca import AlpacaClient

LISTED_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS", "NYSEARCA"}


def is_common_symbol(symbol: str, exchange: str) -> bool:
    cleaned = symbol.strip().upper()
    if not cleaned or cleaned in {"*", "ALL"}:
        return False
    if any(char in cleaned for char in "/^+= ."):
        return False
    return exchange.upper() in LISTED_EXCHANGES


class UniverseCache:
    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "universe.json"

    def load_if_fresh(self) -> list[str] | None:
        if not self._path.exists():
            return None
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if payload.get("date") != date.today().isoformat() or payload.get("version") != 2:
            return None
        symbols = payload.get("symbols") or []
        return [str(symbol).upper() for symbol in symbols if symbol]

    def save(self, symbols: list[str]) -> None:
        self._path.write_text(
            json.dumps(
                {"date": date.today().isoformat(), "version": 2, "symbols": symbols},
                indent=2,
            ),
            encoding="utf-8",
        )

    async def symbols(self, alpaca: AlpacaClient) -> list[str]:
        cached = self.load_if_fresh()
        if cached:
            return cached
        assets = await alpaca.list_us_equities()
        symbols = [
            item["symbol"]
            for item in assets
            if is_common_symbol(item["symbol"], item["exchange"])
        ]
        self.save(symbols)
        return symbols
