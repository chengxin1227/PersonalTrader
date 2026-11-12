
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from monitor.engine import MonitorEngine, _alert_cooldown_key
from monitor.models import Condition, ConditionType, Quote, Rule
from monitor.store import Store


def _day25(session_type: ConditionType, rule_id: str) -> Rule:
    group = {
        ConditionType.PREMARKET_GAIN: "盘前",
        ConditionType.REGULAR_GAIN: "盘中",
        ConditionType.AFTERHOURS_GAIN: "盘后",
    }[session_type]
    return Rule(
        id=rule_id,
        name="盘中曾涨超25%后续涨超10%的股票",
        group=group,
        symbol="*",
        cooldown_minutes=240,
        confirm_delay_minutes=0,
        once_per_session=True,
        condition=Condition(
            type=session_type,
            value=10,
            min_recent_day_change=25,
            recent_day_count=1,
            intraday_touch=True,
        ),
        notify="Day-25-boost",
        slack_message="{symbol}{field_gap}{change_pct:+.1f}%",
    )


def test_once_per_session_cooldown_key_is_rule_scoped():
    regular = _day25(ConditionType.REGULAR_GAIN, "regular-day25-10")
    premarket = _day25(ConditionType.PREMARKET_GAIN, "premarket-day25-10")
    day = __import__("monitor.engine", fromlist=["now_et"]).now_et().date().isoformat()
    assert _alert_cooldown_key(regular, "ESLA") == f"regular-day25-10:盘中:{day}:ESLA"
    assert _alert_cooldown_key(premarket, "ESLA") == f"premarket-day25-10:盘前:{day}:ESLA"
    assert _alert_cooldown_key(regular, "ESLA") != _alert_cooldown_key(premarket, "ESLA")


def test_same_rule_alerts_only_once_per_session(tmp_path):
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

    rule = _day25(ConditionType.REGULAR_GAIN, "regular-day25-10")
    halt = Rule(
        id="regular-halt-or-day50",
        name="盘中熔断或涨超50%后续涨超5%的股票",
        group="盘中",
        symbol="*",
        cooldown_minutes=240,
        confirm_delay_minutes=0,
        once_per_session=True,
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
        symbol="ESLA",
        company="Estrella",
        price=1.0,
        change_pct=12.0,
        volume=50_000,
        updated_at=datetime.now(timezone.utc),
    )

    async def run() -> None:
        t0 = datetime(2026, 11, 12, 3, 30, tzinfo=timezone.utc)
        state = store.load_state()
        with patch("monitor.store._now", return_value=t0), patch(
            "monitor.engine.now_et",
            return_value=t0,
        ):
            first = await engine._fire_alert(
                rule, quote, state, extra=None,
                cooldown_key=_alert_cooldown_key(rule, quote.symbol),
            )
            assert first is not None
            assert delivered == ["ESLA"]
            later = t0 + timedelta(hours=5)
            with patch("monitor.store._now", return_value=later), patch(
                "monitor.engine.now_et", return_value=later
            ):
                again = await engine._fire_alert(
                    rule, quote, state, extra=None,
                    cooldown_key=_alert_cooldown_key(rule, quote.symbol),
                )
            assert again is None
            assert delivered == ["ESLA"]
            # Different rule in the same session can still alert once
            # (after the 5s coalesce window).
            with patch("monitor.store._now", return_value=later + timedelta(seconds=6)), patch(
                "monitor.engine.now_et", return_value=later + timedelta(seconds=6)
            ):
                halt_alert = await engine._fire_alert(
                    halt, quote, state, extra=None,
                    cooldown_key=_alert_cooldown_key(halt, quote.symbol),
                )
            assert halt_alert is not None
            assert delivered == ["ESLA", "ESLA"]

    asyncio.run(run())


def test_live_rules_once_per_session_flags():
    from monitor.config import ROOT
    from monitor.rules import load_rules

    config = load_rules(ROOT / "config" / "rules.yaml")
    ids = {
        "afterhours-day25-10",
        "afterhours-halt-or-day50",
        "afterhours-5d-boost",
        "regular-day25-10",
        "regular-halt-or-day50",
        "regular-5d-boost",
        "premarket-day25-10",
        "premarket-halt-or-day50",
        "premarket-5d-boost",
    }
    for rule in config.rules:
        if rule.id in ids:
            assert rule.once_per_session is True, rule.id
