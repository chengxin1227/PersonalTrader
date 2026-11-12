import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from monitor.engine import MonitorEngine
from monitor.models import Condition, ConditionType, Quote, Rule
from monitor.store import Store


def _halt_rule() -> Rule:
    return Rule(
        id="afterhours-halt-or-day50",
        name="盘中熔断或涨超50%后续涨超5%的股票",
        description="第一次超5%不提醒",
        group="盘后",
        symbol="*",
        cooldown_minutes=240,
        confirm_delay_minutes=10,
        confirm_value=0,
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=5,
            max_market_cap=500_000_000,
            min_volume=100_000,
            min_regular_touch=50,
            include_circuit_breaker=True,
            require_circuit_breaker=True,
            min_recent_day_change=50,
            recent_day_count=1,
            intraday_touch=True,
        ),
        slack_message="{symbol}{field_gap}{change_pct:+.1f}%",
        notify="Halt-boost",
    )


def test_pool_entry_alert_does_not_block_afterhours_single(tmp_path):
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
        delivered.append(alert.symbol)

    engine._deliver = _deliver  # type: ignore[method-assign]
    engine._fill_prior_session = AsyncMock()
    engine._add_to_tonghuashun = lambda *_args, **_kwargs: None

    rule = _halt_rule()
    quote = Quote(
        symbol="AIOK",
        company="AI OKTO",
        price=12.0,
        change_pct=85.0,
        volume=200_000,
        updated_at=datetime.now(timezone.utc),
    )

    async def run() -> None:
        t0 = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)
        with patch("monitor.store._now", return_value=t0):
            await engine._emit_halt_or_day_pool_entries(rule, [quote])
        assert delivered == ["AIOK"]
        state = store.load_state()
        assert "Halt-boost:pool:AIOK" in state["last_fired"]
        assert "Halt-boost:AIOK" not in state["last_fired"]

        ah = Quote(
            symbol="AIOK",
            company="AI OKTO",
            price=13.0,
            change_pct=6.5,
            volume=200_000,
            updated_at=datetime.now(timezone.utc),
        )
        later = t0 + timedelta(minutes=1)
        with patch("monitor.store._now", return_value=later):
            first = await engine._fire_alert(
                rule,
                ah,
                state,
                extra=None,
                cooldown_key="Halt-boost:AIOK",
            )
        assert first is None
        assert delivered == ["AIOK"]
        arm_key = "confirm:Halt-boost:AIOK"
        assert arm_key in state["confirm_armed"]

        with patch("monitor.store._now", return_value=later):
            again = await engine._fire_alert(
                rule,
                ah,
                state,
                extra=None,
                cooldown_key="Halt-boost:AIOK",
            )
        assert again is None
        assert delivered == ["AIOK"]

        armed = datetime.fromisoformat(state["confirm_armed"][arm_key])
        state["confirm_armed"][arm_key] = (armed - timedelta(minutes=10)).isoformat()
        soft = Quote(
            symbol="AIOK",
            company="AI OKTO",
            price=12.2,
            change_pct=1.5,
            volume=200_000,
            updated_at=datetime.now(timezone.utc),
        )
        with patch("monitor.store._now", return_value=later + timedelta(seconds=6)):
            alert = await engine._fire_alert(
                rule,
                soft,
                state,
                extra=None,
                cooldown_key="Halt-boost:AIOK",
            )
        assert alert is not None
        assert delivered == ["AIOK", "AIOK"]
        store.save_state(state)

        # Pool entry cooldown still blocks a second pool push.
        with patch("monitor.store._now", return_value=later + timedelta(minutes=2)):
            await engine._emit_halt_or_day_pool_entries(rule, [quote])
        assert delivered == ["AIOK", "AIOK"]

    asyncio.run(run())


def test_rules_for_active_session_keeps_only_current_session():
    from monitor.engine import _rules_for_active_session

    rules = [
        _halt_rule(),
        Rule(
            id="regular-halt-or-day50",
            name="盘中熔断或涨超50%后续涨超5%的股票",
            group="盘中",
            symbol="*",
            cooldown_minutes=240,
            condition=Condition(
                type=ConditionType.REGULAR_GAIN,
                value=5,
                min_regular_touch=50,
                include_circuit_breaker=True,
                require_circuit_breaker=True,
                min_recent_day_change=50,
                recent_day_count=1,
                intraday_touch=True,
            ),
            notify="Halt-boost",
        ),
        Rule(
            id="premarket-halt-or-day50",
            name="盘中熔断或涨超50%后续涨超5%的股票",
            group="盘前",
            symbol="*",
            cooldown_minutes=240,
            condition=Condition(
                type=ConditionType.PREMARKET_GAIN,
                value=5,
                min_regular_touch=50,
                include_circuit_breaker=True,
                require_circuit_breaker=True,
                min_recent_day_change=50,
                recent_day_count=1,
                intraday_touch=True,
            ),
            notify="Halt-boost",
        ),
        Rule(
            id="afterhours-day25-10",
            name="盘中曾涨超25%后续涨超10%的股票",
            group="盘后",
            symbol="*",
            cooldown_minutes=240,
            condition=Condition(
                type=ConditionType.AFTERHOURS_GAIN,
                value=10,
                min_recent_day_change=25,
                recent_day_count=1,
                intraday_touch=True,
            ),
            notify="Day-25-boost",
        ),
        Rule(
            id="regular-day25-10",
            name="盘中曾涨超25%后续涨超10%的股票",
            group="盘中",
            symbol="*",
            cooldown_minutes=240,
            condition=Condition(
                type=ConditionType.REGULAR_GAIN,
                value=10,
                min_recent_day_change=25,
                recent_day_count=1,
                intraday_touch=True,
            ),
            notify="Day-25-boost",
        ),
        Rule(
            id="premarket-5d-boost",
            name="5日曾涨超50%后续超5%或曾涨超100%后续超0%的股票",
            group="盘前",
            symbol="*",
            cooldown_minutes=240,
            condition=Condition(
                type=ConditionType.PREMARKET_GAIN,
                value=0,
                day_gain_tiers=[
                    {"min_recent_day_change": 50, "value": 5},
                    {"min_recent_day_change": 100, "value": 0},
                ],
                recent_day_count=5,
            ),
            notify="5-day-boost",
        ),
    ]
    with patch("monitor.engine.is_regular_session", return_value=True), patch(
        "monitor.engine.is_afterhours", return_value=False
    ), patch("monitor.engine.is_premarket", return_value=False):
        assert [rule.id for rule in _rules_for_active_session(rules)] == [
            "regular-halt-or-day50",
            "regular-day25-10",
        ]
    with patch("monitor.engine.is_regular_session", return_value=False), patch(
        "monitor.engine.is_afterhours", return_value=True
    ), patch("monitor.engine.is_premarket", return_value=False):
        assert [rule.id for rule in _rules_for_active_session(rules)] == [
            "afterhours-halt-or-day50",
            "afterhours-day25-10",
        ]


def test_regular_halt_skips_pool_entry_alert(tmp_path):
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
        delivered.append(alert.symbol)

    engine._deliver = _deliver  # type: ignore[method-assign]
    engine._fill_prior_session = AsyncMock()
    engine._add_to_tonghuashun = lambda *_args, **_kwargs: None

    rule = Rule(
        id="regular-halt-or-day50",
        name="盘中熔断或涨超50%后续涨超5%的股票",
        group="盘中",
        symbol="*",
        cooldown_minutes=240,
        confirm_delay_minutes=10,
        confirm_value=0,
        condition=Condition(
            type=ConditionType.REGULAR_GAIN,
            value=5,
            min_regular_touch=50,
            include_circuit_breaker=True,
            require_circuit_breaker=True,
            min_recent_day_change=50,
            recent_day_count=1,
            intraday_touch=True,
        ),
        notify="Halt-boost",
        slack_message="{symbol}{field_gap}{change_pct:+.1f}%",
    )
    quote = Quote(
        symbol="POOL",
        company="Pool Co",
        price=2.0,
        change_pct=60.0,
        volume=200_000,
        updated_at=datetime.now(timezone.utc),
    )

    async def run() -> None:
        await engine._emit_halt_or_day_pool_entries(rule, [quote])
        assert delivered == []
        state = store.load_state()
        assert "Halt-boost:pool:POOL" not in state.get("last_fired", {})

    asyncio.run(run())
