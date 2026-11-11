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
from monitor.session import due_check_reminders, session_late_window

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]


def test_rules_yaml_loads_important_block():
    config = load_rules(ROOT / "config" / "rules.yaml")
    assert config.important.ths_group == "我的自选"
    assert set(config.important.high_priority_rule_ids) == {
        "afterhours-day25-10",
        "premarket-day25-10",
        "regular-day25-10",
        "afterhours-halt-or-day50",
        "premarket-halt-or-day50",
        "regular-halt-or-day50",
        "afterhours-5d-boost",
        "premarket-5d-boost",
        "regular-5d-boost",
        "afterhours-sudden-10",
        "regular-sudden-10",
        "premarket-sudden-10",
        "regular-nasdaq-2",
    }
    assert set(config.important.watchlist.always) == {
        "afterhours-day25-10",
        "premarket-day25-10",
        "regular-day25-10",
        "afterhours-halt-or-day50",
        "premarket-halt-or-day50",
        "regular-halt-or-day50",
        "afterhours-5d-boost",
        "premarket-5d-boost",
        "regular-5d-boost",
    }
    assert config.important.watchlist.event == []
    assert config.important.watchlist.afterhours_open == []
    # Top5 boards stay in the rule group only; 盘后分组 / 我的自选 are for single names.
    assert config.important.watchlist.board == []
    assert set(config.important.watchlist.session_late) == {
        "afterhours-sudden-10",
        "regular-sudden-10",
        "premarket-sudden-10",
    }
    assert config.important.watchlist.session_late_hours == 2
    assert config.important.watchlist.reminder_sync == []
    assert config.important.watched_symbols.symbols == []
    keys = [item.key for item in config.important.reminders]
    assert keys == ["afterhours-2h30", "afterhours-3h"]
    assert all(
        item.rule_ids == ["afterhours-top5", "afterhours-halt50-or-5d50-board"]
        for item in config.important.reminders
    )
    assert all(item.sync_watchlist is False for item in config.important.reminders)
    board = next(
        rule for rule in config.rules if rule.id == "afterhours-halt50-or-5d50-board"
    )
    assert board.notify == "Halt-5d-board"
    assert board.condition.top_n == 50
    assert board.condition.value == 0
    assert board.condition.min_regular_touch == 50
    assert board.condition.min_recent_day_change == 50
    assert board.condition.recent_day_count == 5
    assert board.condition.include_circuit_breaker is True
    day25 = [rule for rule in config.rules if rule.id.endswith("-day25-10")]
    assert len(day25) == 3
    assert {rule.name for rule in day25} == {"盘中曾涨超25%后续涨超10%的股票"}
    assert {rule.notify for rule in day25} == {"Day-25-boost"}
    assert all(rule.cooldown_minutes == 240 for rule in day25)
    assert all(rule.confirm_delay_minutes == 10 for rule in day25)
    assert all(rule.confirm_value == 0 for rule in day25)
    halt80 = [rule for rule in config.rules if rule.id.endswith("-halt-or-day50")]
    assert len(halt80) == 3
    assert {rule.name for rule in halt80} == {"盘中熔断或涨超50%后续涨超5%的股票"}
    assert {rule.notify for rule in halt80} == {"Halt-boost"}
    assert all(rule.condition.value == 5 for rule in halt80)
    assert all(rule.condition.min_recent_day_change == 50 for rule in halt80)
    assert all(rule.cooldown_minutes == 240 for rule in halt80)
    assert all(rule.confirm_delay_minutes == 10 for rule in halt80)
    assert all(rule.confirm_value == 0 for rule in halt80)
    boost5d = [rule for rule in config.rules if rule.id.endswith("-5d-boost")]
    assert len(boost5d) == 3
    assert {rule.name for rule in boost5d} == {
        "5日曾涨超50%后续超5%或曾涨超100%后续超0%的股票"
    }
    assert {rule.notify for rule in boost5d} == {"5-day-boost"}
    assert all(rule.cooldown_minutes == 240 for rule in boost5d)
    assert all(rule.confirm_delay_minutes == 10 for rule in boost5d)
    assert all(rule.confirm_value == 0 for rule in boost5d)
    assert all(rule.condition.recent_day_count == 5 for rule in boost5d)
    assert all(len(rule.condition.day_gain_tiers) == 2 for rule in boost5d)


def test_important_reminders_merge_into_due_window():
    config = load_rules(ROOT / "config" / "rules.yaml")
    extra = _important_reminders(config.important)
    due = due_check_reminders(datetime(2026, 10, 15, 19, 1, tzinfo=ET), extra=extra)
    assert [item.key for item in due] == ["afterhours-3h"]


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
            board=["afterhours-top5", "regular-top5", "premarket-top5"],
            session_late_hours=2,
            session_late=[
                "afterhours-sudden-10",
                "regular-sudden-10",
                "premarket-sudden-10",
            ],
        ),
    )
    sudden_ah = _sudden("afterhours", "afterhours-sudden-10")
    sudden_reg = _sudden("regular", "regular-sudden-10")
    sudden_pre = _sudden("premarket", "premarket-sudden-10")
    five = Rule(
        id="afterhours-5d-boost",
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
    ah50 = Rule(
        id="afterhours-ah50",
        name="a",
        description="d",
        group="盘后",
        symbol="*",
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN, value=50, min_fade_from_high=20
        ),
    )
    assert session_late_window("afterhours", datetime(2026, 10, 15, 18, 0, tzinfo=ET), hours=2)
    assert not session_late_window("afterhours", datetime(2026, 10, 15, 17, 0, tzinfo=ET), hours=2)
    assert session_late_window("regular", datetime(2026, 10, 15, 14, 0, tzinfo=ET), hours=2)
    assert not session_late_window("regular", datetime(2026, 10, 15, 13, 59, tzinfo=ET), hours=2)
    assert session_late_window("premarket", datetime(2026, 10, 15, 7, 30, tzinfo=ET), hours=2)
    assert not session_late_window("premarket", datetime(2026, 10, 15, 7, 29, tzinfo=ET), hours=2)

    assert not _wants_important_watchlist(
        config, sudden_ah, board=False, moment=datetime(2026, 10, 15, 17, 0, tzinfo=ET)
    )
    assert _wants_important_watchlist(
        config, sudden_ah, board=False, moment=datetime(2026, 10, 15, 18, 0, tzinfo=ET)
    )
    assert _wants_important_watchlist(
        config, sudden_reg, board=False, moment=datetime(2026, 10, 15, 14, 0, tzinfo=ET)
    )
    assert not _wants_important_watchlist(
        config, sudden_reg, board=False, moment=datetime(2026, 10, 15, 13, 0, tzinfo=ET)
    )
    assert _wants_important_watchlist(
        config, sudden_pre, board=False, moment=datetime(2026, 10, 15, 8, 0, tzinfo=ET)
    )
    assert not _wants_important_watchlist(
        config, sudden_pre, board=False, moment=datetime(2026, 10, 15, 6, 0, tzinfo=ET)
    )
    assert not _wants_important_watchlist(config, ah50, board=False)
    assert not _wants_important_watchlist(config, five, board=False)
    assert not _wants_important_watchlist(config, top5, board=False)
    assert _wants_important_watchlist(config, top5, board=True)
    assert not _wants_important_watchlist(config, ah50, board=True)
    assert _is_high_priority(config, sudden_ah)


def test_live_rules_top5_skips_important_and_session_groups():
    config = load_rules(ROOT / "config" / "rules.yaml").important
    top5 = Rule(
        id="afterhours-top5",
        name="盘后涨幅最高前5的股票",
        description="d",
        group="盘后",
        symbol="*",
        condition=Condition(type=ConditionType.AFTERHOURS_GAIN, value=0, top_n=5),
    )
    assert not _wants_important_watchlist(config, top5, board=True)
    assert not _wants_important_watchlist(config, top5, board=False)
