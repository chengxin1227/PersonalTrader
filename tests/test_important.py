from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from monitor.engine import _important_reminders, _wants_important_watchlist
from monitor.models import ImportantConfig, Rule, Condition, ConditionType
from monitor.rules import load_rules
from monitor.session import afterhours_open_window, due_check_reminders

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]


def test_rules_yaml_loads_important_block():
    config = load_rules(ROOT / "config" / "rules.yaml")
    assert config.important.ths_group == "我的自选"
    assert "afterhours-sudden-10" in config.important.high_priority_rule_ids
    assert "afterhours-sudden-10" in config.important.watchlist.always
    assert "afterhours-5d100-ah0" in config.important.watchlist.afterhours_open
    keys = [item.key for item in config.important.reminders]
    assert keys == ["afterhours-2h30", "afterhours-3h"]
    assert all("afterhours-ah50" not in item.rule_ids for item in config.important.reminders)


def test_important_reminders_merge_into_due_window():
    config = load_rules(ROOT / "config" / "rules.yaml")
    extra = _important_reminders(config.important)
    due = due_check_reminders(datetime(2026, 10, 15, 19, 1, tzinfo=ET), extra=extra)
    assert [item.key for item in due] == ["afterhours-3h"]


def test_wants_important_watchlist_gates():
    config = load_rules(ROOT / "config" / "rules.yaml").important
    sudden = Rule(
        id="afterhours-sudden-10",
        name="s",
        description="d",
        group="盘后",
        symbol="*",
        condition=Condition(
            type=ConditionType.SUDDEN_GAIN, value=10, late_session="afterhours"
        ),
    )
    five = Rule(
        id="afterhours-5d50-ah5",
        name="f",
        description="d",
        group="盘后",
        symbol="*",
        condition=Condition(type=ConditionType.AFTERHOURS_GAIN, value=5),
    )
    top5 = Rule(
        id="afterhours-top5",
        name="t",
        description="d",
        group="盘后",
        symbol="*",
        condition=Condition(type=ConditionType.AFTERHOURS_GAIN, value=0, top_n=5),
    )
    assert _wants_important_watchlist(config, sudden, board=False)
    assert not _wants_important_watchlist(config, sudden, board=True)
    assert not _wants_important_watchlist(config, top5, board=False)
    inside = datetime(2026, 10, 15, 16, 5, tzinfo=ET)
    outside = datetime(2026, 10, 15, 16, 20, tzinfo=ET)
    assert afterhours_open_window(inside, minutes=15)
    assert _wants_important_watchlist(config, five, board=False, moment=inside)
    assert not _wants_important_watchlist(config, five, board=False, moment=outside)
