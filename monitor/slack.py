from __future__ import annotations

import re
from typing import Optional

import httpx

from monitor.config import Settings
from monitor.models import Alert
from monitor.quote_link import page_url
from monitor.rules import FIELD_GAP, split_alert_name

_CHANGE = re.compile(r"[+-]\d+(?:\.\d+)?%")


def _link_name(raw: str, url: str | None) -> str:
    if not url:
        return raw
    match = _CHANGE.search(raw)
    if match is None or match.start() == 0:
        return f"<{url}|{raw.strip()}>"
    name, label = split_alert_name(raw[: match.start()])
    rest = re.sub(r"[ \u00a0]+", FIELD_GAP, raw[match.start() :], count=1)
    if label:
        return f"<{url}|{name}>{FIELD_GAP}{label} {rest}"
    return f"<{url}|{name}>{FIELD_GAP}{rest}"


class SlackClient:
    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_alert(self, alert: Alert, webhook_url: str | None = None) -> None:
        url = webhook_url or self._settings.slack_webhook_url
        if not url:
            raise RuntimeError("SLACK_WEBHOOK_URL is not set")
        color = "#34C759" if (alert.change_pct or 0) < 0 else "#FF3B30"
        fallback = f"{alert.label}\n{alert.message}" if alert.label else alert.message
        response = await self._client.post(
            url,
            json={
                "attachments": [
                    {
                        "color": color,
                        "fallback": fallback,
                        "blocks": self._blocks(alert),
                    }
                ]
            },
        )
        response.raise_for_status()

    async def send_digest(
        self, title: str, rows: list[tuple[str, str]], webhook_url: str | None = None
    ) -> None:
        url = webhook_url or self._settings.slack_webhook_url
        if not url:
            raise RuntimeError("SLACK_WEBHOOK_URL is not set")
        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": title}},
        ]
        for symbol, line in rows:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": _link_name(line, page_url(symbol))},
                }
            )
        response = await self._client.post(
            url,
            json={
                "attachments": [
                    {
                        "color": "#FF3B30",
                        "fallback": title,
                        "blocks": blocks,
                    }
                ]
            },
        )
        response.raise_for_status()

    async def send_text(self, text: str, webhook_url: str | None = None) -> None:
        url = webhook_url or self._settings.slack_webhook_url
        if not url:
            raise RuntimeError("SLACK_WEBHOOK_URL is not set")
        response = await self._client.post(
            url,
            json={"text": text},
        )
        response.raise_for_status()

    def _blocks(self, alert: Alert) -> list[dict]:
        lines = [raw for raw in alert.message.splitlines() if raw.strip()]
        headline = _link_name(lines[0], page_url(alert.symbol)) if lines else alert.message
        blocks: list[dict] = []
        if alert.label:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{alert.label}*"}})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": headline}})
        reason = "\n".join(lines[1:])
        if reason:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": reason}]})
        return blocks
