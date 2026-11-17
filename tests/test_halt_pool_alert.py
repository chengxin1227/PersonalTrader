import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from monitor.engine import MonitorEngine
from monitor.models import Condition, ConditionType, Quote, Rule
from monitor.store import Store


def _halt_rule() -> Rule:
    return Rule(
        id="afterhours-halt-or-5d-boost",
        name="熔断或5日涨超50%后续超10%或曾涨超100%后续超5%的股票",
        description="第一次达标不提醒",
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
            day_gain_tiers=[
                {"min_recent_day_change": 50, "value": 10},
                {"min_recent_day_change": 100, "value": 5},
            ],
            recent_day_count=5,
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
            session_circuit_breaker=True,
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
        arm_key = "confirm:afterhours-halt-or-5d-boost:AIOK"
        assert arm_key in state["confirm_armed"]
        assert "confirm:Halt-boost:AIOK" not in state["confirm_armed"]

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
            id="regular-halt-or-5d-boost",
            name="熔断或5日涨超50%后续超10%或曾涨超100%后续超5%的股票",
            group="盘中",
            symbol="*",
            cooldown_minutes=240,
            condition=Condition(
                type=ConditionType.REGULAR_GAIN,
                value=5,
                min_regular_touch=50,
                include_circuit_breaker=True,
                require_circuit_breaker=True,
                day_gain_tiers=[
                    {"min_recent_day_change": 50, "value": 5},
                    {"min_recent_day_change": 100, "value": 0},
                ],
                recent_day_count=5,
                intraday_touch=True,
            ),
            notify="Halt-boost",
        ),
        Rule(
            id="premarket-halt-or-5d-boost",
            name="熔断或5日涨超50%后续超10%或曾涨超100%后续超5%的股票",
            group="盘前",
            symbol="*",
            cooldown_minutes=240,
            condition=Condition(
                type=ConditionType.PREMARKET_GAIN,
                value=5,
                min_regular_touch=50,
                include_circuit_breaker=True,
                require_circuit_breaker=True,
                day_gain_tiers=[
                    {"min_recent_day_change": 50, "value": 5},
                    {"min_recent_day_change": 100, "value": 0},
                ],
                recent_day_count=5,
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
    ]
    with patch("monitor.engine.is_regular_session", return_value=True), patch(
        "monitor.engine.is_afterhours", return_value=False
    ), patch("monitor.engine.is_premarket", return_value=False):
        assert [rule.id for rule in _rules_for_active_session(rules)] == [
            "regular-halt-or-5d-boost",
            "regular-day25-10",
        ]
    with patch("monitor.engine.is_regular_session", return_value=False), patch(
        "monitor.engine.is_afterhours", return_value=True
    ), patch("monitor.engine.is_premarket", return_value=False):
        assert [rule.id for rule in _rules_for_active_session(rules)] == [
            "afterhours-halt-or-5d-boost",
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
        id="regular-halt-or-5d-boost",
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


def test_confirm_arm_is_rule_scoped_not_shared_notify(tmp_path):
    """盘前 armed must not make 盘中 fire immediately via shared Halt-boost notify key."""
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
    engine._deliver = AsyncMock()
    engine._fill_prior_session = AsyncMock()
    engine._add_to_tonghuashun = lambda *_args, **_kwargs: None

    premarket = Rule(
        id="premarket-halt-or-5d-boost",
        name="盘中熔断或涨超50%后续涨超5%的股票",
        group="盘前",
        symbol="*",
        cooldown_minutes=240,
        confirm_delay_minutes=10,
        confirm_value=0,
        once_per_session=True,
        notify="Halt-boost",
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
    )
    regular = Rule(
        id="regular-halt-or-5d-boost",
        name="盘中熔断或涨超50%后续涨超5%的股票",
        group="盘中",
        symbol="*",
        cooldown_minutes=240,
        confirm_delay_minutes=10,
        confirm_value=0,
        once_per_session=True,
        notify="Halt-boost",
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
    )
    quote = Quote(
        symbol="ABC",
        company="Abc",
        price=2.0,
        change_pct=8.0,
        volume=20_000,
        updated_at=datetime.now(timezone.utc),
    )
    state = store.load_state()
    # Legacy shared notify arm already past delay — must not trigger regular.
    state["confirm_armed"]["confirm:Halt-boost:ABC"] = (
        datetime.now(timezone.utc) - timedelta(minutes=30)
    ).isoformat()
    store.save_state(state)

    async def run() -> None:
        live = store.load_state()
        first = await engine._fire_alert(
            regular,
            quote,
            live,
            extra=None,
            cooldown_key="Halt-boost:盘中:ABC",
        )
        assert first is None
        assert "confirm:regular-halt-or-5d-boost:ABC" in live["confirm_armed"]
        assert "confirm:Halt-boost:ABC" not in live["confirm_armed"]

        # Premarket keeps its own arm clock.
        prem = await engine._fire_alert(
            premarket,
            quote,
            live,
            extra=None,
            cooldown_key="Halt-boost:盘前:ABC",
        )
        assert prem is None
        assert "confirm:premarket-halt-or-5d-boost:ABC" in live["confirm_armed"]

    asyncio.run(run())


def test_halt_or_5d_match_includes_today_intraday_touch(tmp_path):
    """Today's RTH peak >50%/100% uses the same session thresholds as 5-day floors."""
    from unittest.mock import AsyncMock, patch

    from monitor.models import DayGainTier

    store = Store(tmp_path)
    # Only today's touches — no prior-day floors.
    store.note_day_gains("2026-11-18", 50, ["T50"])
    store.note_day_gains("2026-11-18", 100, ["T100"])

    engine = MonitorEngine.__new__(MonitorEngine)
    engine.store = store
    engine.moomoo = object()
    engine._trend_moomoo = None
    engine.verbose = False
    engine._scan_log = {}

    rule = Rule(
        id="regular-halt-or-5d-boost",
        name="merged",
        symbol="*",
        condition=Condition(
            type=ConditionType.REGULAR_GAIN,
            value=5,
            max_market_cap=1_000_000_000,
            min_volume=10_000,
            min_regular_touch=50,
            include_circuit_breaker=True,
            require_circuit_breaker=True,
            day_gain_tiers=[
                DayGainTier(min_recent_day_change=50, value=10),
                DayGainTier(min_recent_day_change=100, value=5),
            ],
            recent_day_count=5,
            intraday_touch=True,
        ),
    )
    marks = {
        "T50": Quote(
            symbol="T50",
            price=2.0,
            change_pct=10.1,
            volume=20_000,
            updated_at=datetime.now(timezone.utc),
        ),
        "T100": Quote(
            symbol="T100",
            price=3.0,
            change_pct=5.1,
            volume=20_000,
            updated_at=datetime.now(timezone.utc),
        ),
        "LOW": Quote(
            symbol="LOW",
            price=1.0,
            change_pct=6.0,
            volume=20_000,
            updated_at=datetime.now(timezone.utc),
        ),
    }
    # LOW also touched 50% today but session only 6% — should miss (>10 needed).
    store.note_day_gains("2026-11-18", 50, ["LOW"])

    async def run() -> None:
        engine._moomoo_snapshots = AsyncMock(return_value=marks)  # type: ignore[method-assign]
        with patch("monitor.engine.now_et") as now:
            now.return_value = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
            with patch(
                "monitor.engine.recent_trading_days",
                return_value=[
                    "2026-11-17",
                    "2026-11-16",
                    "2026-11-15",
                    "2026-11-14",
                    "2026-11-13",
                ],
            ):
                matched = await engine._match_halt_or_day(rule)
        assert {quote.symbol for quote in matched} == {"T50", "T100"}

    asyncio.run(run())


def test_halt50_board_takes_boost_top5(tmp_path):
    """Board ranks halt-or-5d-boost matches by AH gain and keeps top_n."""
    from unittest.mock import AsyncMock, patch

    from monitor.models import DayGainTier

    store = Store(tmp_path)
    store.note_day_gains("2026-11-17", 50, ["A", "B", "C", "D", "E", "F"])

    engine = MonitorEngine.__new__(MonitorEngine)
    engine.store = store
    engine.moomoo = object()
    engine._trend_moomoo = None
    engine.verbose = False
    engine._scan_log = {}

    board = Rule(
        id="afterhours-halt50-or-5d50-board",
        name="board",
        symbol="*",
        notify="Halt-5d-board",
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=5,
            max_market_cap=1_000_000_000,
            min_volume=10_000,
            top_n=5,
            min_regular_touch=50,
            include_circuit_breaker=True,
            require_circuit_breaker=True,
            day_gain_tiers=[
                DayGainTier(min_recent_day_change=50, value=10),
                DayGainTier(min_recent_day_change=100, value=5),
            ],
            recent_day_count=5,
            intraday_touch=True,
        ),
    )
    marks = {
        sym: Quote(
            symbol=sym,
            price=1.0,
            change_pct=pct,
            volume=20_000,
            updated_at=datetime.now(timezone.utc),
        )
        for sym, pct in [
            ("A", 20.0),
            ("B", 18.0),
            ("C", 16.0),
            ("D", 14.0),
            ("E", 12.0),
            ("F", 11.0),
        ]
    }

    async def run() -> None:
        engine._saved_afterhours_quotes = AsyncMock(return_value=marks)  # type: ignore[method-assign]
        with patch("monitor.engine.now_et") as now:
            now.return_value = datetime(2026, 11, 18, 17, 0, tzinfo=timezone.utc)
            with patch(
                "monitor.engine.recent_trading_days",
                return_value=["2026-11-17", "2026-11-16", "2026-11-15", "2026-11-14", "2026-11-13"],
            ):
                leaders = await engine._touched_afterhours_leaders(board, 10_000)
        assert [q.symbol for q in leaders] == ["A", "B", "C", "D", "E"]

    asyncio.run(run())


def test_halt_or_5d_trigger_label_legs():
    from monitor.models import DayGainTier
    from monitor.rules import halt_or_5d_trigger_label

    rule = Rule(
        id="regular-halt-or-5d-boost",
        name="熔断或5日",
        symbol="*",
        group="盘中",
        condition=Condition(
            type=ConditionType.REGULAR_GAIN,
            value=5,
            require_circuit_breaker=True,
            day_gain_tiers=[
                DayGainTier(min_recent_day_change=50, value=10),
                DayGainTier(min_recent_day_change=100, value=5),
            ],
            recent_day_count=5,
        ),
    )
    today50 = Quote(
        symbol="T50",
        change_pct=12.0,
        recent_day_gain_floors=[50],
    )
    assert (
        halt_or_5d_trigger_label(rule, today50, today_floors={50})
        == "今日盘中一度涨超50%后续超10%"
    )
    assert (
        halt_or_5d_trigger_label(rule, today50, today_floors=set())
        == "过去5日曾收盘涨超50%后续超10%"
    )
    today100 = Quote(
        symbol="T100",
        change_pct=6.0,
        recent_day_gain_floors=[100],
    )
    assert (
        halt_or_5d_trigger_label(rule, today100, today_floors={100})
        == "今日盘中一度涨超100%后续超5%"
    )
    halt = Quote(symbol="HALT", change_pct=6.0, session_circuit_breaker=True)
    assert halt_or_5d_trigger_label(rule, halt) == "涨停后续超5%"


def test_fire_alert_uses_trigger_as_pushover_label(tmp_path):
    store = Store(tmp_path)
    store.note_day_gains("2026-11-18", 50, ["AIOK"])

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
    delivered: list = []

    async def _deliver(alert, **_kwargs) -> None:
        delivered.append(alert)

    engine._deliver = _deliver  # type: ignore[method-assign]
    engine._fill_prior_session = AsyncMock()
    engine._add_to_tonghuashun = lambda *_args, **_kwargs: None

    rule = _halt_rule().model_copy(update={"confirm_delay_minutes": 0})
    quote = Quote(
        symbol="AIOK",
        company="AI OKTO",
        price=12.0,
        change_pct=12.5,
        volume=200_000,
        recent_day_gain_floors=[50],
        updated_at=datetime.now(timezone.utc),
    )
    state = store.load_state()

    async def run() -> None:
        with patch("monitor.engine.now_et") as now:
            now.return_value = datetime(2026, 11, 18, 17, 0, tzinfo=timezone.utc)
            alert = await engine._fire_alert(
                rule,
                quote,
                state,
                extra=None,
                cooldown_key="Halt-boost:AIOK",
            )
        assert alert is not None
        assert alert.label == "今日盘中一度涨超50%后续超10%"
        assert alert.message.startswith("今日盘中一度涨超50%后续超10%\n")
        assert delivered[0].label == alert.label
        assert delivered[0].message.startswith("今日盘中一度涨超50%后续超10%\n")

    asyncio.run(run())
