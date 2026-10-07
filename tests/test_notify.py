import asyncio
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx

from monitor.config import Settings
from monitor.models import Alert
from monitor.notify import NotifyRoutes, env_suffix
from monitor.pushover import PushoverClient
from monitor.slack import SlackClient


def test_notify_name_becomes_an_env_suffix():
    assert env_suffix("premarket-pullback") == "PREMARKET_PULLBACK"
    assert env_suffix("day-30") == "DAY_30"
    assert env_suffix("top5") == "TOP5"


def test_rule_route_uses_its_own_webhook_and_token():
    routes = NotifyRoutes(
        {
            "SLACK_WEBHOOK_HALT": "https://hooks.slack.com/services/halt",
            "PUSHOVER_TOKEN_HALT": "halt-token",
        },
        default_slack="https://hooks.slack.com/services/default",
        default_token="default-token",
    )
    assert routes.slack_url("halt") == "https://hooks.slack.com/services/halt"
    assert routes.pushover_token("halt") == "halt-token"
    assert routes.slack_url("trend") == "https://hooks.slack.com/services/default"
    assert routes.pushover_token("trend") == "default-token"
    assert routes.slack_url(None) == "https://hooks.slack.com/services/default"
    assert routes.has_slack("halt")
    assert not routes.has_pushover("trend")


def test_slack_posts_to_the_rule_webhook():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    async def run() -> None:
        client = SlackClient(
            Settings(slack_webhook_url="https://hooks.slack.com/services/default"),
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        alert = Alert(
            id="1",
            rule_id="halt",
            rule_name="Halt",
            label="Halt",
            notify="halt",
            symbol="GYGY",
            message="Game Your Game   +10.0%   $1.50",
            price=1.5,
            change_pct=10.0,
            fired_at=datetime.now(timezone.utc),
        )
        await client.send_alert(alert, webhook_url="https://hooks.slack.com/services/halt")
        await client.close()

    asyncio.run(run())
    assert seen["url"] == "https://hooks.slack.com/services/halt"


def test_pushover_posts_with_the_rule_token():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = parse_qs(request.content.decode())
        seen.update({key: values[0] for key, values in parsed.items()})
        return httpx.Response(200, json={"status": 1})

    async def run() -> None:
        client = PushoverClient(
            Settings(pushover_token="default-token", pushover_user="user-key"),
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        alert = Alert(
            id="1",
            rule_id="halt",
            rule_name="Halt",
            symbol="GYGY",
            message="Game Your Game   +10.0%   $1.50",
            price=1.5,
            change_pct=10.0,
            fired_at=datetime.now(timezone.utc),
        )
        await client.send_alert(alert, token="halt-token")
        await client.close()

    asyncio.run(run())
    assert seen["token"] == "halt-token"
    assert seen["user"] == "user-key"
