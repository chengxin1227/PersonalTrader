import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

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
        await engine._emit_halt_or_day_pool_entries(rule, [quote])
        assert delivered == ["AIOK"]
        state = store.load_state()
        assert f"{rule.id}:pool:AIOK" in state["last_fired"]
        assert f"{rule.id}:AIOK" not in state["last_fired"]

        ah = Quote(
            symbol="AIOK",
            company="AI OKTO",
            price=13.0,
            change_pct=6.5,
            volume=200_000,
            updated_at=datetime.now(timezone.utc),
        )
        first = await engine._fire_alert(
            rule,
            ah,
            state,
            extra=None,
            cooldown_key=f"{rule.id}:{ah.symbol}",
        )
        assert first is None
        assert delivered == ["AIOK"]
        arm_key = "confirm:Halt-boost:AIOK"
        assert arm_key in state["confirm_armed"]

        again = await engine._fire_alert(
            rule,
            ah,
            state,
            extra=None,
            cooldown_key=f"{rule.id}:{ah.symbol}",
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
        alert = await engine._fire_alert(
            rule,
            soft,
            state,
            extra=None,
            cooldown_key=f"{rule.id}:{soft.symbol}",
        )
        assert alert is not None
        assert delivered == ["AIOK", "AIOK"]
        store.save_state(state)

        # Pool entry cooldown still blocks a second pool push.
        await engine._emit_halt_or_day_pool_entries(rule, [quote])
        assert delivered == ["AIOK", "AIOK"]

    asyncio.run(run())
