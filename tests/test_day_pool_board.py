"""5-day 50% pool board: session-gain ranking into one Tonghuashun group."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from monitor.engine import (
    MonitorEngine,
    _is_day_pool_board,
    _is_day_spike_rule,
    _is_day_spike_singles_rule,
    _is_peak_top_board,
    _is_touched_leaderboard,
)
from monitor.models import Condition, ConditionType, Quote, Rule
from monitor.store import Store


def _board(session: ConditionType, rule_id: str) -> Rule:
    return Rule(
        id=rule_id,
        name="5日曾涨超50%的股票",
        description="test",
        board_notify=False,
        symbol="*",
        cooldown_minutes=240,
        condition=Condition(
            type=session,
            value=0,
            min_recent_day_change=50,
            recent_day_count=5,
            intraday_touch=False,
            min_market_cap=1_000_000,
            max_market_cap=1_000_000_000,
            min_volume=10_000,
            top_n=50,
        ),
    )


def test_day_pool_board_classifier():
    board = _board(ConditionType.AFTERHOURS_GAIN, "afterhours-5d50-board")
    assert _is_day_pool_board(board)
    assert _is_day_spike_rule(board)
    assert not _is_day_spike_singles_rule(board)
    assert not _is_touched_leaderboard(board)
    assert not _is_peak_top_board(board)

    top5 = Rule(
        id="afterhours-top5",
        name="盘后涨幅最高前5的股票",
        description="test",
        symbol="*",
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=0,
            top_n=5,
            min_volume=100_000,
        ),
    )
    assert _is_peak_top_board(top5)
    assert not _is_day_pool_board(top5)


def test_day_pool_session_leaders_sort_by_session_gain(tmp_path):
    store = Store(tmp_path)
    store.note_day_gains("2026-11-10", 50, ["AAA", "BBB", "CCC", "DDD"])
    store.note_day_gains("2026-11-11", 50, ["EEE"])

    engine = MonitorEngine.__new__(MonitorEngine)
    engine.store = store
    engine.moomoo = object()
    engine._trend_moomoo = None

    marks = {
        "AAA": Quote(
            symbol="AAA",
            price=2.0,
            change_pct=12.0,
            volume=50_000,
            updated_at=datetime.now(timezone.utc),
        ),
        "BBB": Quote(
            symbol="BBB",
            price=1.5,
            change_pct=-3.0,
            volume=20_000,
            updated_at=datetime.now(timezone.utc),
        ),
        "CCC": Quote(
            symbol="CCC",
            price=3.0,
            change_pct=8.0,
            volume=5_000,  # below volume floor
            updated_at=datetime.now(timezone.utc),
        ),
        "DDD": Quote(
            symbol="DDD",
            price=0.0,
            change_pct=20.0,
            volume=30_000,
            updated_at=datetime.now(timezone.utc),
        ),
    }

    async def run() -> None:
        engine._saved_afterhours_quotes = AsyncMock(return_value=marks)  # type: ignore[method-assign]
        rule = _board(ConditionType.AFTERHOURS_GAIN, "afterhours-5d50-board")
        # Freeze "today" via include_end path: patch recent days by using known store days.
        from unittest.mock import patch

        with patch("monitor.engine.now_et") as now:
            now.return_value = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)
            with patch(
                "monitor.engine.recent_trading_days",
                return_value=["2026-11-11", "2026-11-10", "2026-11-09", "2026-11-08", "2026-11-07"],
            ):
                leaders = await engine._day_pool_session_leaders(rule, 10_000)
        # BBB is red (−3%) and CCC fails volume; only AAA clears >0% + volume.
        assert [quote.symbol for quote in leaders] == ["AAA"]

    asyncio.run(run())
