from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from monitor.models import MarketClock, Quote
from monitor.session import (
    is_afterhours,
    is_premarket,
    is_regular_session,
    is_trading_day,
    afterhours_board_slot,
    session_board_slot,
    afterhours_late_window,
    session_late_window,
    check_reminder_text,
    due_check_reminders,
    afterhours_just_opened,
    afterhours_stable_due,
    afterhours_stable_missed,
    format_nz_clock,
    last_hour_phase,
    recent_trading_days,
    regular_close_approach,
    premarket_mark,
    seconds_until_extended_open,
    seconds_until_session_end,
    trade_is_premarket,
)

ET = ZoneInfo("America/New_York")
NZ = ZoneInfo("Pacific/Auckland")


def test_format_nz_clock_converts_from_et():
    # 16:30 ET in daylight saving ≈ 09:30 NZ next calendar day.
    slot = datetime(2026, 10, 15, 16, 30, tzinfo=ET)
    assert format_nz_clock(slot) == slot.astimezone(NZ).strftime("%H:%M")


def test_premarket_window():
    assert is_premarket(datetime(2026, 9, 23, 4, 0, tzinfo=ET))
    assert is_premarket(datetime(2026, 9, 23, 6, 50, tzinfo=ET))
    assert not is_premarket(datetime(2026, 9, 23, 9, 30, tzinfo=ET))
    assert not is_premarket(datetime(2026, 9, 23, 15, 0, tzinfo=ET))
    assert not is_premarket(datetime(2026, 9, 26, 6, 0, tzinfo=ET))  # Saturday


def test_regular_session_window():
    assert is_regular_session(datetime(2026, 9, 23, 9, 30, tzinfo=ET))
    assert is_regular_session(datetime(2026, 9, 23, 15, 59, tzinfo=ET))
    assert not is_regular_session(datetime(2026, 9, 23, 9, 29, tzinfo=ET))
    assert not is_regular_session(datetime(2026, 9, 23, 16, 0, tzinfo=ET))
    assert not is_regular_session(datetime(2026, 9, 26, 12, 0, tzinfo=ET))


def test_afterhours_stable_window_is_the_first_five_minutes():
    assert regular_close_approach(datetime(2026, 10, 8, 15, 50, tzinfo=ET))
    assert not regular_close_approach(datetime(2026, 10, 8, 15, 49, tzinfo=ET))
    assert afterhours_just_opened(datetime(2026, 10, 8, 16, 0, 44, tzinfo=ET))
    assert not afterhours_just_opened(datetime(2026, 10, 8, 16, 0, 45, tzinfo=ET))
    assert not afterhours_stable_due(datetime(2026, 10, 8, 16, 4, 59, tzinfo=ET), 5)
    assert afterhours_stable_due(datetime(2026, 10, 8, 16, 5, 0, tzinfo=ET), 5)
    assert afterhours_stable_due(datetime(2026, 10, 8, 16, 5, 19, tzinfo=ET), 5)
    assert not afterhours_stable_due(datetime(2026, 10, 8, 16, 5, 20, tzinfo=ET), 5)
    assert not afterhours_stable_due(datetime(2026, 10, 10, 16, 5, tzinfo=ET), 5)
    assert not afterhours_stable_missed(datetime(2026, 10, 8, 16, 5, 19, tzinfo=ET), 5)
    assert afterhours_stable_missed(datetime(2026, 10, 8, 16, 5, 20, tzinfo=ET), 5)


def test_check_reminders_fire_once_inside_a_three_minute_window():
    from monitor.session import CheckReminder, afterhours_open_window
    from datetime import time

    assert [item.key for item in due_check_reminders(datetime(2026, 10, 15, 15, 44, tzinfo=ET))] == []
    close = due_check_reminders(datetime(2026, 10, 15, 15, 45, tzinfo=ET))
    assert [item.key for item in close] == ["regular-close-15"]
    assert close[0].rule_ids == ("afterhours-halt-or-day50", "regular-sudden-10")
    assert due_check_reminders(datetime(2026, 10, 15, 15, 47, 59, tzinfo=ET))
    assert due_check_reminders(datetime(2026, 10, 15, 15, 48, tzinfo=ET)) == []

    # Built-in list no longer includes the deleted day30 / 18:30 entries.
    assert due_check_reminders(datetime(2026, 10, 15, 16, 5, tzinfo=ET)) == []
    assert due_check_reminders(datetime(2026, 10, 15, 18, 30, tzinfo=ET)) == []

    extra = [
        CheckReminder(
            "afterhours-2h30",
            time(18, 30),
            ("afterhours-top5", "afterhours-halt-or-day50"),
            "盘后2.5小时",
        ),
        CheckReminder(
            "afterhours-3h",
            time(19, 0),
            ("afterhours-top5", "afterhours-halt-or-day50"),
            "盘后3小时",
        ),
    ]
    mid = due_check_reminders(datetime(2026, 10, 15, 18, 30, tzinfo=ET), extra=extra)
    assert [item.key for item in mid] == ["afterhours-2h30"]
    assert mid[0].rule_ids == ("afterhours-top5", "afterhours-halt-or-day50")
    assert [
        item.key for item in due_check_reminders(datetime(2026, 10, 15, 19, 0, tzinfo=ET), extra=extra)
    ] == ["afterhours-3h"]

    assert [item.rule_ids for item in due_check_reminders(datetime(2026, 10, 15, 9, 15, tzinfo=ET))] == [
        ("premarket-sudden-10",)
    ]
    assert due_check_reminders(datetime(2026, 10, 17, 15, 45, tzinfo=ET)) == []  # Saturday

    assert afterhours_open_window(datetime(2026, 10, 15, 16, 0, tzinfo=ET), minutes=15)
    assert afterhours_open_window(datetime(2026, 10, 15, 16, 14, 59, tzinfo=ET), minutes=15)
    assert not afterhours_open_window(datetime(2026, 10, 15, 16, 15, tzinfo=ET), minutes=15)
    assert not afterhours_open_window(datetime(2026, 10, 15, 15, 59, tzinfo=ET), minutes=15)


def test_check_reminder_text_names_the_lists_to_review():
    assert (
        check_reminder_text(
            "盘后2.5小时",
            ["盘后涨幅最高前5的股票", "盘后涨超50%的股票", "盘中熔断或涨超50%的股票"],
        )
        == "盘后2.5小时，请检查盘后涨幅最高前5的股票和盘后涨超50%的股票和盘中熔断或涨超50%的股票"
    )


def test_recent_trading_days_skip_weekends_and_can_leave_out_today():
    assert recent_trading_days(date(2026, 10, 9), 5, include_end=False) == [
        "2026-10-08",
        "2026-10-07",
        "2026-10-06",
        "2026-10-05",
        "2026-10-02",
    ]
    assert recent_trading_days(date(2026, 10, 9), 5, include_end=True) == [
        "2026-10-09",
        "2026-10-08",
        "2026-10-07",
        "2026-10-06",
        "2026-10-05",
    ]


def test_last_hour_records_before_the_close_and_alerts_from_15():
    assert last_hour_phase(datetime(2026, 10, 7, 14, 44, tzinfo=ET)) is None
    assert last_hour_phase(datetime(2026, 10, 7, 14, 45, tzinfo=ET)) == "baseline"
    assert last_hour_phase(datetime(2026, 10, 7, 14, 59, tzinfo=ET)) == "baseline"
    assert last_hour_phase(datetime(2026, 10, 7, 15, 0, tzinfo=ET)) == "alert"
    assert last_hour_phase(datetime(2026, 10, 7, 15, 59, tzinfo=ET)) == "alert"
    assert last_hour_phase(datetime(2026, 10, 7, 16, 0, tzinfo=ET)) is None
    assert last_hour_phase(datetime(2026, 10, 10, 15, 10, tzinfo=ET)) is None
    assert last_hour_phase(datetime(2026, 10, 7, 13, 44, tzinfo=ET), hours=2) is None
    assert last_hour_phase(datetime(2026, 10, 7, 13, 45, tzinfo=ET), hours=2) == "baseline"
    assert last_hour_phase(datetime(2026, 10, 7, 13, 59, tzinfo=ET), hours=2) == "baseline"
    assert last_hour_phase(datetime(2026, 10, 7, 14, 0, tzinfo=ET), hours=2) == "alert"
    assert last_hour_phase(datetime(2026, 10, 7, 15, 59, tzinfo=ET), hours=2) == "alert"
    assert last_hour_phase(datetime(2026, 10, 7, 16, 0, tzinfo=ET), hours=2) is None
    assert last_hour_phase(datetime(2026, 10, 7, 7, 14, tzinfo=ET), hours=2, session="premarket") is None
    assert last_hour_phase(datetime(2026, 10, 7, 7, 15, tzinfo=ET), hours=2, session="premarket") == "baseline"
    assert last_hour_phase(datetime(2026, 10, 7, 7, 30, tzinfo=ET), hours=2, session="premarket") == "alert"
    assert last_hour_phase(datetime(2026, 10, 7, 9, 29, tzinfo=ET), hours=2, session="premarket") == "alert"
    assert last_hour_phase(datetime(2026, 10, 7, 9, 30, tzinfo=ET), hours=2, session="premarket") is None
    assert last_hour_phase(datetime(2026, 10, 7, 17, 44, tzinfo=ET), hours=2, session="afterhours") is None
    assert last_hour_phase(datetime(2026, 10, 7, 17, 45, tzinfo=ET), hours=2, session="afterhours") == "baseline"
    assert last_hour_phase(datetime(2026, 10, 7, 18, 0, tzinfo=ET), hours=2, session="afterhours") == "alert"
    assert last_hour_phase(datetime(2026, 10, 7, 19, 59, tzinfo=ET), hours=2, session="afterhours") == "alert"
    assert last_hour_phase(datetime(2026, 10, 7, 20, 0, tzinfo=ET), hours=2, session="afterhours") is None


def test_afterhours_late_window_is_last_two_hours():
    assert not afterhours_late_window(datetime(2026, 10, 15, 17, 59, tzinfo=ET), hours=2)
    assert afterhours_late_window(datetime(2026, 10, 15, 18, 0, tzinfo=ET), hours=2)
    assert afterhours_late_window(datetime(2026, 10, 15, 19, 59, tzinfo=ET), hours=2)
    assert not afterhours_late_window(datetime(2026, 10, 15, 20, 0, tzinfo=ET), hours=2)
    assert not afterhours_late_window(datetime(2026, 10, 17, 18, 0, tzinfo=ET), hours=2)


def test_session_late_window_covers_premarket_and_regular():
    assert session_late_window("premarket", datetime(2026, 10, 15, 7, 30, tzinfo=ET), hours=2)
    assert not session_late_window("premarket", datetime(2026, 10, 15, 7, 29, tzinfo=ET), hours=2)
    assert session_late_window("regular", datetime(2026, 10, 15, 14, 0, tzinfo=ET), hours=2)
    assert not session_late_window("regular", datetime(2026, 10, 15, 13, 59, tzinfo=ET), hours=2)
    assert session_late_window("盘中", datetime(2026, 10, 15, 15, 0, tzinfo=ET), hours=2)


def test_afterhours_board_slot_is_each_half_hour():
    assert afterhours_board_slot(datetime(2026, 10, 6, 15, 59, tzinfo=ET)) is None
    # First board at 16:05 ET (≈10:05 NZ); nothing before that.
    assert afterhours_board_slot(datetime(2026, 10, 6, 16, 0, tzinfo=ET)) is None
    assert afterhours_board_slot(datetime(2026, 10, 6, 16, 4, tzinfo=ET)) is None
    assert afterhours_board_slot(datetime(2026, 10, 6, 16, 5, tzinfo=ET)) == datetime(
        2026, 10, 6, 16, 5, tzinfo=ET
    )
    assert afterhours_board_slot(datetime(2026, 10, 6, 16, 29, tzinfo=ET)) == datetime(
        2026, 10, 6, 16, 5, tzinfo=ET
    )
    assert afterhours_board_slot(datetime(2026, 10, 6, 16, 30, tzinfo=ET)) == datetime(
        2026, 10, 6, 16, 30, tzinfo=ET
    )
    assert afterhours_board_slot(datetime(2026, 10, 6, 19, 59, tzinfo=ET)) == datetime(
        2026, 10, 6, 19, 30, tzinfo=ET
    )
    assert afterhours_board_slot(datetime(2026, 10, 6, 20, 0, tzinfo=ET)) is None
    assert afterhours_board_slot(datetime(2026, 10, 10, 17, 0, tzinfo=ET)) is None


def test_session_board_slot_covers_premarket_and_regular():
    assert session_board_slot("premarket", datetime(2026, 10, 6, 3, 59, tzinfo=ET)) is None
    assert session_board_slot("premarket", datetime(2026, 10, 6, 4, 0, tzinfo=ET)) == datetime(
        2026, 10, 6, 4, 0, tzinfo=ET
    )
    assert session_board_slot("premarket", datetime(2026, 10, 6, 8, 45, tzinfo=ET)) == datetime(
        2026, 10, 6, 8, 30, tzinfo=ET
    )
    assert session_board_slot("premarket", datetime(2026, 10, 6, 9, 30, tzinfo=ET)) is None
    assert session_board_slot("regular", datetime(2026, 10, 6, 9, 29, tzinfo=ET)) is None
    assert session_board_slot("regular", datetime(2026, 10, 6, 9, 30, tzinfo=ET)) == datetime(
        2026, 10, 6, 9, 30, tzinfo=ET
    )
    assert session_board_slot("regular", datetime(2026, 10, 6, 11, 5, tzinfo=ET)) == datetime(
        2026, 10, 6, 11, 0, tzinfo=ET
    )
    assert session_board_slot("regular", datetime(2026, 10, 6, 16, 0, tzinfo=ET)) is None
    assert session_board_slot("afterhours", datetime(2026, 10, 6, 16, 5, tzinfo=ET)) == (
        afterhours_board_slot(datetime(2026, 10, 6, 16, 5, tzinfo=ET))
    )


def test_afterhours_window():
    assert is_afterhours(datetime(2026, 9, 23, 16, 0, tzinfo=ET))
    assert is_afterhours(datetime(2026, 9, 23, 19, 59, tzinfo=ET))
    assert not is_afterhours(datetime(2026, 9, 23, 15, 59, tzinfo=ET))
    assert not is_afterhours(datetime(2026, 9, 23, 20, 0, tzinfo=ET))
    assert not is_afterhours(datetime(2026, 9, 26, 17, 0, tzinfo=ET))


def test_seconds_until_session_end_matches_each_window():
    assert seconds_until_session_end(datetime(2026, 9, 23, 8, 30, tzinfo=ET)) == 3600
    assert seconds_until_session_end(datetime(2026, 9, 23, 15, 0, tzinfo=ET)) == 3600
    assert seconds_until_session_end(datetime(2026, 9, 23, 19, 0, tzinfo=ET)) == 3600
    assert seconds_until_session_end(datetime(2026, 9, 23, 3, 0, tzinfo=ET)) is None
    assert seconds_until_session_end(datetime(2026, 9, 23, 12, 0, tzinfo=ET)) == 4 * 3600


def test_seconds_until_next_extended_session():
    sunday_evening = datetime(2026, 9, 27, 17, 52, tzinfo=ET)
    assert seconds_until_extended_open(sunday_evening) == pytest.approx(10 * 3600 + 8 * 60)
    assert seconds_until_extended_open(datetime(2026, 9, 28, 4, 0, tzinfo=ET)) == 0
    regular = datetime(2026, 9, 28, 10, 0, tzinfo=ET)
    assert seconds_until_extended_open(regular) == pytest.approx(6 * 3600)


def test_trade_must_be_today_premarket():
    now = datetime(2026, 9, 23, 6, 50, tzinfo=ET)
    assert trade_is_premarket(datetime(2026, 9, 23, 5, 10, tzinfo=ET), now)
    assert not trade_is_premarket(datetime(2026, 9, 22, 20, 10, tzinfo=ET), now)
    assert not trade_is_premarket(datetime(2026, 9, 23, 16, 10, tzinfo=ET), now)


def test_premarket_mark_falls_back_to_today_quote():
    now = datetime(2026, 9, 23, 6, 50, tzinfo=ET)
    quote = Quote(
        symbol="ABCD",
        price=1.0,
        bid=2.1,
        ask=2.3,
        prev_close=2.0,
        trade_at=datetime(2026, 9, 22, 20, 0, tzinfo=ET),
        quote_at=datetime(2026, 9, 23, 5, 15, tzinfo=ET),
    )
    price, change, stamped = premarket_mark(quote, now)
    assert price == 2.2
    assert change == pytest.approx(10.0)
    assert stamped == quote.quote_at


def test_trading_day_uses_next_open():
    morning = datetime(2026, 9, 23, 6, 50, tzinfo=ET)
    clock = MarketClock(
        is_open=False,
        next_open=datetime(2026, 9, 23, 9, 30, tzinfo=ET),
    )
    assert is_trading_day(clock, morning)
    weekend = MarketClock(
        is_open=False,
        next_open=datetime(2026, 9, 28, 9, 30, tzinfo=ET),
    )
    assert not is_trading_day(weekend, datetime(2026, 9, 26, 10, 0, tzinfo=ET))


def test_session_board_slot_five_minute_interval():
    assert session_board_slot(
        "regular", datetime(2026, 10, 6, 9, 30, tzinfo=ET), interval_minutes=5
    ) == datetime(2026, 10, 6, 9, 30, tzinfo=ET)
    assert session_board_slot(
        "regular", datetime(2026, 10, 6, 9, 34, tzinfo=ET), interval_minutes=5
    ) == datetime(2026, 10, 6, 9, 30, tzinfo=ET)
    assert session_board_slot(
        "regular", datetime(2026, 10, 6, 9, 35, tzinfo=ET), interval_minutes=5
    ) == datetime(2026, 10, 6, 9, 35, tzinfo=ET)
    assert session_board_slot(
        "regular", datetime(2026, 10, 6, 11, 7, tzinfo=ET), interval_minutes=5
    ) == datetime(2026, 10, 6, 11, 5, tzinfo=ET)
    # Default half-hour path unchanged.
    assert session_board_slot("regular", datetime(2026, 10, 6, 11, 7, tzinfo=ET)) == datetime(
        2026, 10, 6, 11, 0, tzinfo=ET
    )
