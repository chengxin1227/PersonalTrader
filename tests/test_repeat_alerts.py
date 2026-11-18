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


def test_halt_or_5d_history_keeps_stock_line_and_short_trigger():
    prior = [
        Alert(
            id="1",
            rule_id="premarket-halt-or-5d-boost",
            rule_name="熔断或5日",
            label="今日盘中一度涨超50%后续超10%",
            symbol="AIOK",
            message="AI OKTO    +12.0%    $1.10\n今日盘中一度涨超50%后续超10%",
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
            message="AI OKTO    +6.5%    $1.20\n涨停后续超5%",
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
        message="AI OKTO    +7.0%    $1.30\n过去5日曾收盘涨超100%后续超5%",
        price=1.3,
        change_pct=7.0,
        fired_at=datetime(2026, 11, 3, 14, 0, tzinfo=NZ),
    )
    delivery = with_repeat_history(alert, prior)
    assert delivery.message == (
        "AI OKTO    +7.0%    $1.30\n过去5日曾收盘涨超100%后续超5%\n\n"
        f"{HISTORY_DIVIDER}\n\n"
        "AI OKTO(#2)    13:00    +6.5%    $1.20\n涨停后续超5%\n\n"
        "AI OKTO(#1)    12:00    +12.0%    $1.10\n今日盘中一度涨超50%后续超10%"
    )


def test_mega_matrix_second_alert_history_matches_classic_layout():
    """History: Mega Matrix(#1) time % $ / short label — not the long description."""
    prior = [
        Alert(
            id="1",
            rule_id="afterhours-halt-or-day50",
            rule_name="长描述",
            label="盘中熔断或涨超50%后续涨超5%的股票",
            symbol="MPU",
            message=(
                "Mega Matrix    +75.8%    $1.90\n"
                "盘中最高涨超50%或触发涨停后记录；盘后涨幅超5%，成交量超过1万，"
                "总市值超过100万美元且不超过10亿美元；第一次超5%不提醒，"
                "满10分钟后任意时段再超0%才提醒"
            ),
            price=1.9,
            change_pct=75.8,
            fired_at=datetime(2026, 11, 18, 10, 1, tzinfo=NZ),
        )
    ]
    alert = Alert(
        id="2",
        rule_id="afterhours-halt-or-5d-boost",
        rule_name="熔断或5日",
        label="今日盘中一度涨超50%后续超10%",
        symbol="MPU",
        message="Mega Matrix    +10.8%    $2.35\n今日盘中一度涨超50%后续超10%",
        price=2.35,
        change_pct=10.8,
        fired_at=datetime(2026, 11, 18, 12, 53, tzinfo=NZ),
    )
    delivery = with_repeat_history(alert, prior)
    assert delivery.repeat_count == 2
    assert delivery.message == (
        "Mega Matrix    +10.8%    $2.35\n今日盘中一度涨超50%后续超10%\n\n"
        f"{HISTORY_DIVIDER}\n\n"
        "Mega Matrix(#1)    10:01    +75.8%    $1.90\n"
        "盘中熔断或涨超50%后续涨超5%的股票"
    )
    assert "满10分钟后" not in delivery.message
