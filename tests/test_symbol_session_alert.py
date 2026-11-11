import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from monitor.engine import MonitorEngine
from monitor.models import Condition, ConditionType, Quote, Rule
from monitor.store import Store


def _rule(rule_id: str, notify: str) -> Rule:
    return Rule(
        id=rule_id,
        name=rule_id,
        description=rule_id,
        group="盘前",
        symbol="*",
        cooldown_minutes=240,
        condition=Condition(type=ConditionType.PREMARKET_GAIN, value=0),
        slack_message="{symbol}{field_gap}{change_pct:+.1f}%",
        notify=notify,
    )


def _engine(tmp_path) -> tuple[MonitorEngine, list[str], Store]:
    store = Store(tmp_path)
    engine = MonitorEngine.__new__(MonitorEngine)
    engine.store = store
    engine._lock = asyncio.Lock()
    engine._symbol_alert_lock = asyncio.Lock()
    engine.moomoo = None
    engine.slack = None
    engine.pushover = None
    engine.ths = type("THS", (), {"configured": False})()
    engine.routes = type(
        "Routes",
        (),
        {
            "slack_url": lambda self, _notify=None: "",
            "pushover_token": lambda self, _notify=None: None,
        },
    )()
    engine.last_error = None
    engine._cached_config = type(
        "Cfg",
        (),
        {
            "important": type(
                "Important",
                (),
                {"high_priority_rule_ids": [], "watchlist": None, "watched_symbols": None},
            )()
        },
    )()
    engine._config_loaded_at = datetime.now(timezone.utc)
    delivered: list[str] = []

    async def _deliver(alert, **_kwargs) -> None:
        delivered.append(f"{alert.rule_id}:{alert.symbol}")

    engine._deliver = _deliver  # type: ignore[method-assign]
    engine._fill_prior_session = AsyncMock()
    engine._add_to_tonghuashun = lambda *_args, **_kwargs: None
    return engine, delivered, store


def test_claim_symbol_coalesce_alert_blocks_other_rules_briefly(tmp_path):
    store = Store(tmp_path)
    state = store.load_state()
    now = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)
    assert (
        store.claim_symbol_coalesce_alert(
            state, symbol="nft", rule_id="premarket-day25-10", now=now
        )
        is None
    )
    assert (
        store.claim_symbol_coalesce_alert(
            state,
            symbol="NFT",
            rule_id="premarket-halt-or-day50",
            now=now + timedelta(seconds=2),
        )
        == "premarket-day25-10"
    )
    # Same rule inside the window may still notify (repeat / stage-2).
    assert (
        store.claim_symbol_coalesce_alert(
            state,
            symbol="NFT",
            rule_id="premarket-day25-10",
            now=now + timedelta(seconds=3),
        )
        is None
    )
    # After the coalesce window from the original hit, another rule may notify.
    assert (
        store.claim_symbol_coalesce_alert(
            state,
            symbol="NFT",
            rule_id="premarket-5d-boost",
            now=now + timedelta(seconds=6),
        )
        is None
    )


def test_same_symbol_simultaneous_multi_rule_sends_one_notify(tmp_path):
    engine, delivered, store = _engine(tmp_path)
    quote = Quote(
        symbol="NFT",
        company="NFT Ltd",
        price=0.45,
        change_pct=10.0,
        volume=50_000,
        updated_at=datetime.now(timezone.utc),
    )
    moment = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)

    async def run() -> None:
        with (
            patch("monitor.engine.now_et", return_value=moment),
            patch("monitor.engine.et_session_name", return_value="盘前"),
        ):
            state = store.load_state()
            first = await engine._fire_alert(
                _rule("premarket-day25-10", "Day-25-boost"),
                quote,
                state,
                extra=None,
                cooldown_key="premarket-day25-10:NFT",
                skip_confirm=True,
            )
            second = await engine._fire_alert(
                _rule("premarket-halt-or-day50", "Halt-boost"),
                quote,
                state,
                extra=None,
                cooldown_key="premarket-halt-or-day50:NFT",
                skip_confirm=True,
            )
            third = await engine._fire_alert(
                _rule("premarket-5d-boost", "5-day-boost"),
                quote,
                state,
                extra=None,
                cooldown_key="premarket-5d-boost:NFT",
                skip_confirm=True,
            )
            store.save_state(state)
            assert first is not None
            assert second is None
            assert third is None
            assert delivered == ["premarket-day25-10:NFT"]
            assert "premarket-halt-or-day50:NFT" in state["last_fired"]
            assert "premarket-5d-boost:NFT" in state["last_fired"]

            # Same rule may still repeat after its own cooldown clears.
            state["last_fired"].pop("premarket-day25-10:NFT", None)
            again = await engine._fire_alert(
                _rule("premarket-day25-10", "Day-25-boost"),
                quote,
                state,
                extra=None,
                cooldown_key="premarket-day25-10:NFT",
                skip_confirm=True,
            )
            assert again is not None
            assert delivered == ["premarket-day25-10:NFT", "premarket-day25-10:NFT"]

    asyncio.run(run())


def test_later_separate_rule_still_notifies(tmp_path):
    engine, delivered, store = _engine(tmp_path)
    quote = Quote(
        symbol="NFT",
        company="NFT Ltd",
        price=0.45,
        change_pct=10.0,
        volume=50_000,
        updated_at=datetime.now(timezone.utc),
    )
    first_at = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)
    later = first_at + timedelta(seconds=6)

    async def run() -> None:
        state = store.load_state()
        with (
            patch("monitor.engine.now_et", return_value=first_at),
            patch("monitor.engine.et_session_name", return_value="盘前"),
            patch("monitor.store._now", return_value=first_at),
        ):
            assert (
                await engine._fire_alert(
                    _rule("premarket-day25-10", "Day-25-boost"),
                    quote,
                    state,
                    extra=None,
                    cooldown_key="premarket-day25-10:NFT",
                    skip_confirm=True,
                )
                is not None
            )
        with (
            patch("monitor.engine.now_et", return_value=later),
            patch("monitor.engine.et_session_name", return_value="盘前"),
            patch("monitor.store._now", return_value=later),
        ):
            assert (
                await engine._fire_alert(
                    _rule("premarket-5d-boost", "5-day-boost"),
                    quote,
                    state,
                    extra=None,
                    cooldown_key="premarket-5d-boost:NFT",
                    skip_confirm=True,
                )
                is not None
            )
        assert delivered == ["premarket-day25-10:NFT", "premarket-5d-boost:NFT"]

    asyncio.run(run())


def test_pool_entry_does_not_claim_symbol_coalesce(tmp_path):
    engine, delivered, store = _engine(tmp_path)
    quote = Quote(
        symbol="AIOK",
        company="AI OKTO",
        price=12.0,
        change_pct=85.0,
        volume=200_000,
        updated_at=datetime.now(timezone.utc),
    )
    moment = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)

    async def run() -> None:
        with (
            patch("monitor.engine.now_et", return_value=moment),
            patch("monitor.engine.et_session_name", return_value="盘中"),
        ):
            state = store.load_state()
            pool = await engine._fire_alert(
                _rule("afterhours-halt-or-day50", "Halt-boost"),
                quote,
                state,
                extra=None,
                cooldown_key="afterhours-halt-or-day50:pool:AIOK",
                skip_confirm=True,
            )
            single = await engine._fire_alert(
                _rule("afterhours-halt-or-day50", "Halt-boost"),
                quote,
                state,
                extra=None,
                cooldown_key="afterhours-halt-or-day50:AIOK",
                skip_confirm=True,
            )
            assert pool is not None
            assert single is not None
            assert delivered == [
                "afterhours-halt-or-day50:AIOK",
                "afterhours-halt-or-day50:AIOK",
            ]

    asyncio.run(run())
