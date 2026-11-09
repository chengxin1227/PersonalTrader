from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from monitor.engine import (
    _important_reminders,
    _is_high_priority,
    _wants_important_watchlist,
)
from monitor.models import Rule, Condition, ConditionType
from monitor.rules import load_rules
from monitor.session import due_check_reminders, session_late_window

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]


def test_rules_yaml_loads_important_block():
    config = load_rules(ROOT / "config" / "rules.yaml")
    assert config.important.ths_group == "我的自选"
    assert config.important.watchlist.always == []
    assert config.important.watchlist.event == []
    assert config.important.watchlist.afterhours_open == []
    assert config.important.watchlist.board == [
        "afterhours-top5",
        "regular-top5",
        "premarket-top5",
        "afterhours-halt-or-day80",
    ]
    assert set(config.important.watchlist.session_late) == {
        "afterhours-sudden-10",
        "regular-sudden-10",
        "premarket-sudden-10",
    }
    assert config.important.watchlist.session_late_hours == 2
    assert config.important.watchlist.reminder_sync == ["afterhours-top5"]
    assert config.important.watched_symbols.symbols == ["OMH", "AEHL"]
    keys = [item.key for item in config.important.reminders]
    assert keys == ["afterhours-2h30", "afterhours-3h"]
    assert all(item.rule_ids == ["afterhours-top5"] for item in config.important.reminders)


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
    config = load_rules(ROOT / "config" / "rules.yaml").important
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
