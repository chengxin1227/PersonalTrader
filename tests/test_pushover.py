import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from monitor.config import Settings
from monitor.models import Alert
from monitor.engine import MonitorEngine, opend_is_down
from monitor.pushover import PushoverClient, html_message
from monitor.slack import SlackClient


def _settings() -> Settings:
    return Settings(pushover_token="app-token", pushover_user="user-key")


def test_pushover_posts_symbol_and_message():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = parse_qs(request.content.decode())
        seen.update({key: values[0] for key, values in parsed.items()})
        return httpx.Response(200, json={"status": 1})

    async def run() -> None:
        client = PushoverClient(
            _settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        alert = Alert(
            id="1",
            rule_id="premarket",
            rule_name="盘前",
            symbol="GYGY",
            message="Game Your Game   +10.0%   $1.50",
            price=1.5,
            change_pct=10.0,
            fired_at=datetime.now(timezone.utc),
        )
        alert.url = "amihexin://"
        await client.send_alert(alert)
        await client.close()

    asyncio.run(run())
    assert seen["token"] == "app-token"
    assert "url" not in seen
    assert "url_title" not in seen
    assert seen["user"] == "user-key"
    assert seen["html"] == "1"
    assert seen["title"] == "GYGY  +10.0%"
    assert '<a href="amihexin://">Game Your Game</a>' in seen["message"]
    assert "<br>" not in seen["message"]
    assert "#FF3B30" not in seen["message"]
    assert "#34C759" not in seen["message"]
    assert "<b>" not in seen["message"]
    assert "+10.0%" in seen["message"]
    assert "$1.50" in seen["message"]
    assert "盘前" not in seen["message"]
    assert seen["message"].index("+10.0%") < seen["message"].index("$1.50")


def test_pushover_title_starts_with_the_rule_label():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = parse_qs(request.content.decode())
        seen.update({key: values[0] for key, values in parsed.items()})
        return httpx.Response(200, json={"status": 1})

    async def run() -> None:
        client = PushoverClient(
            _settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        alert = Alert(
            id="1",
            rule_id="afterhours-smallcap-10pct",
            rule_name="盘后",
            label="After-hours pullback",
            symbol="GYGY",
            message="Game Your Game   +10.0%   $1.50",
            price=1.5,
            change_pct=10.0,
            fired_at=datetime.now(timezone.utc),
        )
        await client.send_alert(alert)
        await client.close()

    asyncio.run(run())
    assert seen["title"] == "After-hours pullback  GYGY  +10.0%"


def test_slack_message_starts_with_the_rule_label():
    alert = Alert(
        id="1",
        rule_id="afterhours-smallcap-10pct",
        rule_name="盘后目前涨幅超15%",
        label="After-hours pullback",
        symbol="GYGY",
        message="Game Your Game   +10.0%   $1.50\n盘后目前涨幅超15%",
        price=1.5,
        change_pct=10.0,
        fired_at=datetime.now(timezone.utc),
    )
    async def run() -> list:
        client = SlackClient(Settings(slack_webhook_url="https://hooks.slack.com/services/T/B/x"))
        blocks = client._blocks(alert)
        await client.close()
        return blocks

    blocks = asyncio.run(run())
    assert blocks[0]["text"]["text"] == "*After-hours pullback*"
    assert "Game Your Game" in blocks[1]["text"]["text"]


def test_digest_links_each_name_like_other_alerts():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = parse_qs(request.content.decode())
        seen.update({key: values[0] for key, values in parsed.items()})
        return httpx.Response(200, json={"status": 1})

    async def run() -> None:
        client = PushoverClient(
            _settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        await client.send_digest(
            "16:00 Top 5",
            [("GWHT", "GWHT    +75.4%    $0.30"), ("VCIG", "VCIG    +46.7%    $2.07")],
        )
        await client.close()

    asyncio.run(run())
    assert seen["title"] == "16:00 Top 5"
    assert "url" not in seen
    assert seen["message"].count('<a href="amihexin://">') == 2
    assert '<a href="amihexin://">GWHT</a>' in seen["message"]
    assert "stockpage.10jqka.com.cn" not in seen["message"]
    assert '<font size="1"' not in seen["message"]


def test_message_percent_is_labeled_and_name_stays_the_link():
    html = html_message("Game Your Game   盘后涨幅 -3.2%   $0.41", "amihexin://")
    assert '<a href="amihexin://">Game Your Game</a>' in html
    assert "盘后涨幅</a>" not in html
    assert "盘后涨幅 -3.2%" in html
    assert "<font" not in html.split("-3.2%")[0].split("盘后涨幅")[-1]
    assert "#FF3B30" not in html
    assert "#34C759" not in html
    assert "$0.41" in html


def test_trigger_reason_is_separated_and_smaller():
    html = html_message("KALA BIO    +11.8%    $0.843\n盘中涨幅超10%", "amihexin://")
    assert html.startswith('<a href="amihexin://">KALA BIO</a>')
    assert '<font size="1" color="#8E8E93">\n\n盘中涨幅超10%</font>' in html
    assert "<br>" not in html
    assert "盘中涨幅超10%</a>" not in html


def test_pushover_rejects_failed_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 0, "errors": ["invalid token"]})

    async def run() -> None:
        client = PushoverClient(
            _settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        with pytest.raises(RuntimeError, match="invalid token"):
            await client.send_text("hello")
        await client.close()

    asyncio.run(run())


def test_opend_disconnect_notifies_once_after_the_connection_stays_down():
    assert opend_is_down(RuntimeError("Moomoo after-hours rank failed: Network interruption."))
    assert not opend_is_down(RuntimeError("Slack webhook failed"))

    class _Pushover:
        def __init__(self) -> None:
            self.titles: list[str] = []

        async def send_text(self, text: str, title: str = "PersonalTrader", **_kwargs: object) -> None:
            self.titles.append(title)

    engine = MonitorEngine.__new__(MonitorEngine)
    engine.pushover = _Pushover()
    engine._opend_down_notified = False
    engine._opend_ok_at = datetime.now(timezone.utc)
    failure = RuntimeError("Moomoo after-hours rank failed: Network interruption.")

    async def run() -> None:
        await engine._note_opend_failure(failure)
        assert engine.pushover.titles == []
        engine._opend_ok_at = datetime.now(timezone.utc) - timedelta(seconds=20)
        await engine._note_opend_failure(failure)
        await engine._note_opend_failure(failure)
        assert engine.pushover.titles == ["OpenD 断开"]
        engine._note_opend_ok()
        await engine._note_opend_failure(failure)
        assert engine.pushover.titles == ["OpenD 断开"]

    asyncio.run(run())
