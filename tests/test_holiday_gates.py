"""Exchange holidays suppress reminders and boards."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
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


def _moomoo_holiday_clock() -> MarketClock:
    """Moomoo-style clock: closed, no next_open."""
    return MarketClock(is_open=False, next_open=None)


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


def test_rest_poll_loop_idles_on_holiday(tmp_path):
    """Thanksgiving weekday: no THS sync, poll, or reminders."""
    engine = MonitorEngine.__new__(MonitorEngine)
    engine.market = _moomoo_holiday_clock()
    engine.moomoo = object()
    engine._running = True
    engine.store = Store(tmp_path)
    engine.load_config = MagicMock(
        return_value=type("Cfg", (), {"poll_interval_seconds": 15})()
    )
    engine._maybe_sync_tonghuashun_groups = AsyncMock()
    engine._maybe_send_check_reminders = AsyncMock()
    engine._run_watched_symbols = AsyncMock()
    engine.poll_once = AsyncMock(return_value=[])

    thanksgiving = datetime(2026, 11, 26, 10, 0, tzinfo=ET)
    sleep_calls = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        engine._running = False

    async def run() -> None:
        with (
            patch("monitor.engine.now_et", return_value=thanksgiving),
            patch("monitor.engine.is_trading_day", return_value=False),
            patch("monitor.engine.seconds_until_extended_open", return_value=64_800.0),
            patch("monitor.engine.asyncio.sleep", side_effect=fake_sleep),
        ):
            await engine._rest_poll_loop(idle_logged=False)

    asyncio.run(run())
    assert sleep_calls == 1
    engine._maybe_sync_tonghuashun_groups.assert_not_called()
    engine._maybe_send_check_reminders.assert_not_called()
    engine._run_watched_symbols.assert_not_called()
    engine.poll_once.assert_not_called()
