from __future__ import annotations

import re

# Tonghuashun iOS launches from this scheme. The stock itself is passed on the
# clipboard by their webpage, which a notification cannot set.
_APP_URL = "amihexin://"
_MOOMOO_URL = "https://www.moomoo.com/stock/{symbol}-US"
_SYMBOL = re.compile(r"[A-Z0-9.]+")


def quote_url(symbol: str) -> str:
    """Open the Tonghuashun app. The ticker is already in the notification title."""
    del symbol
    return _APP_URL


def page_url(symbol: str) -> str:
    """Moomoo quote page used in Slack links."""
    cleaned = symbol.strip().upper()
    if not _SYMBOL.fullmatch(cleaned):
        cleaned = "AAPL"
    return _MOOMOO_URL.format(symbol=cleaned)
