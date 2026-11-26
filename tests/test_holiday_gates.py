"""Exchange holidays suppress reminders and boards."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from monitor.engine import MonitorEngine
from monitor.models import Condition, ConditionType, MarketClock, Rule
from monitor.store import Store

ET = ZoneInfo("America/New_York")


def _holiday_clock() -> MarketClock:
    return MarketClock(
        is_open=False,
        next_open=datetime(2026, 11, 27, 9, 30, tzinfo=ET),
    )


def test_holiday_skips_check_reminders(tmp_path):
    engine = MonitorEngine.__new__(MonitorEngine)
    engine.market = _holiday_clock()
    engine._lock = asyncio.Lock()
    engine._check_reminder_inflight = set()
    engine.store = Store(tmp_path)
    engine._deliver_check_reminder = AsyncMock()  # type: ignore[method-assign]

    config = type(
        "Cfg",
        (),
        {
            "important": type(
                "Imp",
                (),
                {
                    "reminders": [
                        type(
                            "R",
                            (),
                            {
                                "key": "afterhours-halt-or-5d-check",
                                "at": "16:15",
                                "label": "熔断或5日盘后",
                                "rule_ids": ["afterhours-halt-or-5d-boost"],
                                "sync_watchlist": False,
                            },
                        )()
                    ]
                },
            )()
        },
    )()

    async def run() -> None:
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "monitor.engine.due_check_reminders"
        ) as due:
            due.return_value = [object()]  # would fire if not gated
            await engine._maybe_send_check_reminders(config)  # type: ignore[arg-type]
            due.assert_not_called()
        engine._deliver_check_reminder.assert_not_called()

    asyncio.run(run())


def test_holiday_skips_leaderboard(tmp_path):
    engine = MonitorEngine.__new__(MonitorEngine)
    engine.market = _holiday_clock()
    engine.moomoo = object()
    engine.store = Store(tmp_path)
    engine._board_retry_at = None
    called = False

    async def boom(*_a, **_k):
        nonlocal called
        called = True
        raise AssertionError("leaderboard should not run on holiday")

    engine._touched_regular_fade_leaders = boom  # type: ignore[method-assign]

    rule = Rule(
        id="regular-halt50-fade-board",
        name="盘中熔断或50%回落的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.REGULAR_GAIN,
            value=0,
            top_n=50,
            board_interval_minutes=5,
            min_regular_touch=50,
            include_circuit_breaker=True,
            min_fade_from_high=0,
        ),
    )

    async def run() -> None:
        await engine._maybe_send_leaderboard(rule, {})

    asyncio.run(run())
    assert called is False
