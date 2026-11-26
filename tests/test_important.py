from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from monitor.engine import (
    _important_reminders,
    _is_high_priority,
    _wants_important_watchlist,
)
from monitor.models import (
    ImportantConfig,
    ImportantWatchlistConfig,
    Rule,
    Condition,
    ConditionType,
)
from monitor.rules import load_rules
from monitor.session import due_check_reminders

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]


def test_rules_yaml_loads_important_block():
    config = load_rules(ROOT / "config" / "rules.yaml")
    assert config.important.ths_group == "今日自选"
    assert config.important.high_priority_rule_ids == []
    # Timed check reminders (yaml only) stay high-priority in _deliver_check_reminder.
    assert {item.key for item in config.important.reminders} == {
        "afterhours-halt-or-5d-check",
        "afterhours-2h",
        "afterhours-buy",
        "afterhours-sell",
    }
    # All singles → 今日自选 (半点榜默认不进). Lists below unused (kept empty).
    assert config.important.watchlist.always == []
    assert config.important.watchlist.event == []
    assert config.important.watchlist.afterhours_open == []
    assert config.important.watchlist.board == []
    assert config.important.watchlist.session_late == []
    assert config.important.watchlist.reminder_sync == []
    assert config.important.watched_symbols.symbols == []
    keys = [item.key for item in config.important.reminders]
    assert keys == [
        "afterhours-halt-or-5d-check",
        "afterhours-2h",
        "afterhours-buy",
        "afterhours-sell",
    ]
    assert [item.at for item in config.important.reminders] == [
        "16:15",
        "18:00",
        "19:50",
        "03:00",
    ]
    assert [item.label for item in config.important.reminders] == [
        "熔断或5日盘后",
        "盘后2小时",
        "准备买入",
        "准备卖出",
    ]
    assert config.important.reminders[0].rule_ids == ["afterhours-halt-or-5d-boost"]
    assert all(
        item.rule_ids
        == ["afterhours-top5", "afterhours-halt50-or-5d50-board"]
        for item in config.important.reminders[1:]
    )
    assert all(item.sync_watchlist is False for item in config.important.reminders)

    assert [rule.id for rule in config.rules] == [
        "afterhours-top5",
        "afterhours-halt-or-5d-boost",
        "afterhours-halt50-or-5d50-board",
        "afterhours-5d100-board",
        "afterhours-sudden-10",
        "afterhours-50-rebound",
        "regular-top5",
        "regular-halt50-fade-board",
        "regular-halt50-rebound",
        "regular-5d100-board",
        "regular-near-peak-35",
        "premarket-top5",
        "premarket-halt-or-5d-boost",
        "premarket-5d100-board",
        "premarket-sudden-10",
        "premarket-50-rebound",
    ]
    for rule_id in (
        "afterhours-5d100-board",
        "regular-5d100-board",
        "premarket-5d100-board",
    ):
        board = next(rule for rule in config.rules if rule.id == rule_id)
        assert board.name == "过去5日超100%的股票"
        assert board.board_notify is False
        assert board.condition.min_recent_day_change == 100
        assert board.condition.recent_day_count == 5
        assert board.condition.top_n == 50
        assert board.condition.board_interval_minutes == 5
    for rule_id, name in (
        ("regular-top5", "今日盘中涨幅最高前5的股票"),
        ("premarket-top5", "今日盘前涨幅最高前5的股票"),
    ):
        board = next(rule for rule in config.rules if rule.id == rule_id)
        assert board.name == name
        assert board.condition.top_n == 5
        assert board.condition.min_volume == 100_000
        assert board.condition.min_market_cap == 1_000_000
        assert board.condition.max_market_cap == 1_000_000_000
    fade = next(rule for rule in config.rules if rule.id == "regular-halt50-fade-board")
    assert fade.name == "盘中熔断或50%回落的股票"
    assert fade.board_notify is False
    assert fade.condition.min_fade_from_high == 0
    rebound = next(rule for rule in config.rules if rule.id == "regular-halt50-rebound")
    assert rebound.name == "盘中熔断或50%回升的股票"
    assert rebound.condition.min_rebound_from_trough == 10
    assert rebound.condition.late_session_hours == 3.75
    assert rebound.once_per_session is True
    ah_rebound = next(rule for rule in config.rules if rule.id == "afterhours-50-rebound")
    assert ah_rebound.name == "盘后涨超50%回升的股票"
    assert ah_rebound.condition.value == 50
    assert ah_rebound.condition.min_rebound_from_trough == 10
    assert ah_rebound.condition.late_session == "afterhours"
    assert ah_rebound.condition.late_session_hours == 2.5
    assert ah_rebound.condition.include_circuit_breaker is False
    assert ah_rebound.once_per_session is True
    pm_rebound = next(rule for rule in config.rules if rule.id == "premarket-50-rebound")
    assert pm_rebound.name == "盘前涨超50%回升的股票"
    assert pm_rebound.condition.value == 50
    assert pm_rebound.condition.min_rebound_from_trough == 10
    assert pm_rebound.condition.late_session == "premarket"
    assert pm_rebound.condition.late_session_hours == 3.25
    assert pm_rebound.condition.include_circuit_breaker is False
    assert pm_rebound.once_per_session is True
    near = next(rule for rule in config.rules if rule.id == "regular-near-peak-35")
    assert near.name == "盘中涨超35%接近最高的股票"
    assert near.condition.value == 35
    assert near.condition.max_fade_from_high == 10
    assert near.condition.min_volume == 10_000
    assert near.condition.late_session_hours == 3.75
    assert near.once_per_session is True
    top5 = next(rule for rule in config.rules if rule.id == "afterhours-top5")
    assert top5.name == "今日盘后涨幅最高前5的股票"
    assert top5.condition.top_n == 5
    assert top5.condition.min_volume == 100_000
    assert top5.condition.min_market_cap == 1_000_000
    assert top5.condition.max_market_cap == 1_000_000_000
    boost = next(rule for rule in config.rules if rule.id == "afterhours-halt-or-5d-boost")
    board = next(
        rule for rule in config.rules if rule.id == "afterhours-halt50-or-5d50-board"
    )
    assert boost.once_per_session is True
    assert boost.condition.top_n is None
    assert board.condition.top_n == 5
    assert board.condition.value == boost.condition.value
    assert board.condition.min_regular_touch == boost.condition.min_regular_touch
    assert board.condition.require_circuit_breaker is True
    assert [
        (t.min_recent_day_change, t.value) for t in board.condition.day_gain_tiers
    ] == [(t.min_recent_day_change, t.value) for t in boost.condition.day_gain_tiers]
    pm_boost = next(
        rule for rule in config.rules if rule.id == "premarket-halt-or-5d-boost"
    )
    assert pm_boost.name == "熔断或5日盘前的股票"
    assert pm_boost.once_per_session is True
    assert pm_boost.condition.type is ConditionType.PREMARKET_GAIN
    assert pm_boost.condition.value == boost.condition.value
    assert pm_boost.condition.min_regular_touch == boost.condition.min_regular_touch
    assert pm_boost.condition.require_circuit_breaker is True
    assert [
        (t.min_recent_day_change, t.value) for t in pm_boost.condition.day_gain_tiers
    ] == [(t.min_recent_day_change, t.value) for t in boost.condition.day_gain_tiers]


def test_important_reminders_merge_into_due_window():
    config = load_rules(ROOT / "config" / "rules.yaml")
    extra = _important_reminders(config.important)
    due_first = due_check_reminders(datetime(2026, 10, 15, 16, 15, tzinfo=ET), extra=extra)
    assert [item.key for item in due_first] == ["afterhours-halt-or-5d-check"]
    assert due_first[0].label == "熔断或5日盘后"
    assert due_first[0].rule_ids == ("afterhours-halt-or-5d-boost",)
    due = due_check_reminders(datetime(2026, 10, 15, 19, 50, tzinfo=ET), extra=extra)
    assert [item.key for item in due] == ["afterhours-buy"]
    assert due[0].label == "准备买入"
    due_early = due_check_reminders(datetime(2026, 10, 15, 18, 0, tzinfo=ET), extra=extra)
    assert [item.key for item in due_early] == ["afterhours-2h"]
    due_sell = due_check_reminders(datetime(2026, 10, 16, 3, 0, tzinfo=ET), extra=extra)
    assert [item.key for item in due_sell] == ["afterhours-sell"]
    assert due_sell[0].label == "准备卖出"
    # Hardcoded 15:45 / 09:15 reminders removed.
    assert due_check_reminders(datetime(2026, 10, 15, 15, 45, tzinfo=ET), extra=extra) == []
    assert due_check_reminders(datetime(2026, 10, 15, 9, 15, tzinfo=ET), extra=extra) == []


def _sudden(session: str, rule_id: str) -> Rule:
    group = {"premarket": "盘前", "regular": "盘中", "afterhours": "盘后"}[session]
    return Rule(
        id=rule_id,
        name="s",
        description="d",
        group=group,
        symbol="*",
        condition=Condition(
            type=ConditionType.SUDDEN_GAIN, value=10, late_session=session
        ),
    )


def test_wants_important_watchlist_gates():
    config = ImportantConfig(
        high_priority_rule_ids=["afterhours-sudden-10"],
        watchlist=ImportantWatchlistConfig(
            board=["afterhours-top5"],
        ),
    )
    sudden_ah = _sudden("afterhours", "afterhours-sudden-10")
    five = Rule(
        id="afterhours-halt-or-5d-boost",
        name="f",
        description="d",
        group="盘后",
        symbol="*",
        condition=Condition(type=ConditionType.AFTERHOURS_GAIN, value=0),
    )
    top5 = Rule(
        id="afterhours-top5",
        name="t",
        description="d",
        group="盘后",
        symbol="*",
        condition=Condition(type=ConditionType.AFTERHOURS_GAIN, value=0, top_n=5),
    )
    other_board = Rule(
        id="afterhours-other-board",
        name="o",
        description="d",
        group="盘后",
        symbol="*",
        condition=Condition(type=ConditionType.AFTERHOURS_GAIN, value=0, top_n=5),
    )
    # All singles → 今日自选; boards only if listed.
    assert _wants_important_watchlist(config, sudden_ah, board=False)
    assert _wants_important_watchlist(config, five, board=False)
    assert not _wants_important_watchlist(config, top5, board=False)
    assert _wants_important_watchlist(config, top5, board=True)
    assert not _wants_important_watchlist(config, other_board, board=True)
    assert _is_high_priority(config, sudden_ah)


def test_live_rules_top5_skips_important_and_session_groups():
    config = load_rules(ROOT / "config" / "rules.yaml").important
    top5 = Rule(
        id="afterhours-top5",
        name="今日盘后涨幅最高前5的股票",
        description="d",
        group="盘后",
        symbol="*",
        condition=Condition(type=ConditionType.AFTERHOURS_GAIN, value=0, top_n=5),
    )
    assert not _wants_important_watchlist(config, top5, board=True)
    assert not _wants_important_watchlist(config, top5, board=False)
