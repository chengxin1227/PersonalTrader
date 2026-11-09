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
