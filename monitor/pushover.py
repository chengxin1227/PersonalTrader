from __future__ import annotations

import html
import re
from typing import Optional

import httpx

from monitor.config import Settings
from monitor.models import Alert
from monitor.quote_link import quote_url
from monitor.rules import FIELD_GAP, split_alert_name

_CHANGE = re.compile(r"[+-]\d+(?:\.\d+)?%")
_PRICE = re.compile(r"\$\d[\d,.]*")

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
_MUTED = "#8E8E93"


def html_digest(rows: list[tuple[str, str]]) -> str:
    """Each row is (url, line). Names stay full size and each one is a link."""
    return "\n".join(_style_line(line, url) for url, line in rows)


def html_message(text: str, url: str | None = None) -> str:
    """First row links the stock name. Later rows are the smaller trigger reason."""
    lines = [raw for raw in text.splitlines() if raw.strip()]
    if not lines:
        return ""
    headline = _style_line(lines[0], url)
    reason = "".join(_style_line(raw, None) for raw in lines[1:])
    if not reason:
        return headline
    # The font tag has to wrap the newlines. A newline before the tag drops the size,
    # and <br> alone disappears from the lock-screen preview.
    return f'{headline}<font size="1" color="{_MUTED}">\n\n{reason}</font>'


def _style_line(raw: str, url: str | None = None) -> str:
    spans = [(match.start(), match.end(), "price") for match in _PRICE.finditer(raw)]
    spans.extend((match.start(), match.end(), "change") for match in _CHANGE.finditer(raw))
    spans.sort()
    if not spans:
        name = html.escape(raw.strip())
        return _anchor(name, url) if url else name
    parts: list[str] = []
    cursor = 0
    previous = ""
    if url and spans[0][0] > 0:
        name, label = split_alert_name(raw[: spans[0][0]])
        if name:
            parts.append(_anchor(html.escape(name), url))
        cursor = spans[0][0]
        while cursor < len(raw) and raw[cursor] == " ":
            cursor += 1
        if label:
            parts.append(f"{FIELD_GAP}{html.escape(label)} ")
        elif name:
            parts.append(FIELD_GAP)
    for start, end, kind in spans:
        if start < cursor:
            continue
        gap = raw[cursor:start]
        if gap:
            parts.append(FIELD_GAP if gap.strip() == "" else (_muted(gap) if previous == "change" else html.escape(gap)))
        token = raw[start:end]
        parts.append(html.escape(token))
        previous = kind
        cursor = end
    tail = raw[cursor:]
    if tail:
        parts.append(_muted(tail) if previous == "change" else html.escape(tail))
    return "".join(parts)


def _muted(text: str) -> str:
    return f'<font color="{_MUTED}">{html.escape(text)}</font>'


def _anchor(label: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{label}</a>'


class PushoverClient:
    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_alert(self, alert: Alert, token: str | None = None) -> None:
        title = alert.symbol
        if alert.change_pct is not None:
            title = f"{alert.symbol}  {alert.change_pct:+.1f}%"
        if alert.label:
            title = f"{alert.label}  {title}"
        await self.send_text(
            html_message(alert.message, alert.url), title=title, html=True, token=token
        )

    async def send_digest(
        self, title: str, rows: list[tuple[str, str]], token: str | None = None
    ) -> None:
        linked = [(quote_url(symbol), line) for symbol, line in rows]
        await self.send_text(html_digest(linked), title=title, html=True, token=token)

    async def send_text(
        self,
        text: str,
        title: str = "PersonalTrader",
        url: str | None = None,
        url_title: str = "打开同花顺",
        html: bool = False,
        token: str | None = None,
    ) -> None:
        app_token = token or self._settings.pushover_token
        if not app_token or not self._settings.pushover_user:
            raise RuntimeError("PUSHOVER_TOKEN and PUSHOVER_USER are not set")
        payload = {
            "token": app_token,
            "user": self._settings.pushover_user,
            "title": title[:250],
            "message": text[:1024],
        }
        if html:
            payload["html"] = "1"
        if url:
            payload["url"] = url[:512]
            payload["url_title"] = url_title[:100]
        response = await self._client.post(
            PUSHOVER_URL,
            data=payload,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("status") != 1:
            errors = body.get("errors") or "Pushover rejected the message"
            raise RuntimeError(str(errors))
