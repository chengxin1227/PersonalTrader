from datetime import datetime
from zoneinfo import ZoneInfo

from monitor.models import Alert
from monitor.repeat_alerts import HISTORY_DIVIDER, with_repeat_history

NZ = ZoneInfo("Pacific/Auckland")


def _alert(message: str, when: datetime) -> Alert:
    return Alert(
        id=message,
        rule_id="rule",
        rule_name="name",
        symbol="INLF",
        message=message,
        price=1.0,
        change_pct=10.0,
        fired_at=when,
    )


def test_first_alert_keeps_message_and_count_one():
    alert = _alert("now", datetime(2026, 11, 3, 12, 0, tzinfo=NZ))
    delivery = with_repeat_history(alert, [])
    assert delivery is alert
    assert delivery.repeat_count == 1
    assert delivery.message == "now"


def test_repeat_merges_newest_first_across_rules():
    prior = [
        _alert(
            "INLIF Ltd    +15.0%    $0.30\n5日盘前超5%",
            datetime(2026, 11, 3, 12, 0, tzinfo=NZ),
        ),
        _alert(
            "INLIF Ltd    +20.0%    $0.33\n盘前1分钟涨超10%",
            datetime(2026, 11, 3, 13, 0, tzinfo=NZ),
        ),
    ]
    alert = _alert(
        "INLIF Ltd    +27.6%    $0.37\n盘前涨超30%",
        datetime(2026, 11, 3, 14, 0, tzinfo=NZ),
    )
    delivery = with_repeat_history(alert, prior)
    assert alert.message == "INLIF Ltd    +27.6%    $0.37\n盘前涨超30%"
    assert alert.repeat_count == 3
    assert delivery.repeat_count == 3
    assert delivery.message == (
        "INLIF Ltd    +27.6%    $0.37\n盘前涨超30%\n\n"
        f"{HISTORY_DIVIDER}\n\n"
        "INLIF Ltd(#2)    13:00    +20.0%    $0.33\n盘前1分钟涨超10%\n\n"
        "INLIF Ltd(#1)    12:00    +15.0%    $0.30\n5日盘前超5%"
    )


def test_halt_or_5d_history_keeps_only_trigger_label():
    prior = [
        Alert(
            id="1",
            rule_id="premarket-halt-or-5d-boost",
            rule_name="熔断或5日",
            label="今日盘中一度涨超50%后续超10%",
            symbol="AIOK",
            message="今日盘中一度涨超50%后续超10%\nAI OKTO    +12.0%    $1.10",
            price=1.1,
            change_pct=12.0,
            fired_at=datetime(2026, 11, 3, 12, 0, tzinfo=NZ),
        ),
        Alert(
            id="2",
            rule_id="regular-halt-or-5d-boost",
            rule_name="熔断或5日",
            label="涨停后续超5%",
            symbol="AIOK",
            message="涨停后续超5%\nAI OKTO    +6.5%    $1.20",
            price=1.2,
            change_pct=6.5,
            fired_at=datetime(2026, 11, 3, 13, 0, tzinfo=NZ),
        ),
    ]
    alert = Alert(
        id="3",
        rule_id="afterhours-halt-or-5d-boost",
        rule_name="熔断或5日",
        label="过去5日曾收盘涨超100%后续超5%",
        symbol="AIOK",
        message="过去5日曾收盘涨超100%后续超5%\nAI OKTO    +7.0%    $1.30",
        price=1.3,
        change_pct=7.0,
        fired_at=datetime(2026, 11, 3, 14, 0, tzinfo=NZ),
    )
    delivery = with_repeat_history(alert, prior)
    assert delivery.message.startswith(
        "过去5日曾收盘涨超100%后续超5%\nAI OKTO    +7.0%    $1.30\n\n"
        f"{HISTORY_DIVIDER}\n\n"
    )
    assert "涨停后续超5%(#2)" in delivery.message
    assert "今日盘中一度涨超50%后续超10%(#1)" in delivery.message
    assert "+6.5%" not in delivery.message
    assert "+12.0%" not in delivery.message
    assert delivery.message.count("AI OKTO") == 1
