from __future__ import annotations

from typing import Optional

import httpx

from monitor.config import Settings
from monitor.models import Alert


class SlackClient:
    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_alert(self, alert: Alert) -> None:
        await self.send_text(self._format(alert))

    async def send_text(self, text: str) -> None:
        if not self._settings.slack_configured:
            raise RuntimeError("SLACK_WEBHOOK_URL is not set")
        response = await self._client.post(
            self._settings.slack_webhook_url,
            json={"text": text},
        )
        response.raise_for_status()

    def _format(self, alert: Alert) -> str:
        change = ""
        if alert.change_pct is not None:
            change = f" ({alert.change_pct:+.2f}% today)"
        price = f"${alert.price:.2f}" if alert.price is not None else "n/a"
        return (
            f":rotating_light: *{alert.rule_name}*\n"
            f"{alert.symbol} {price}{change}\n"
            f"{alert.message}"
        )
