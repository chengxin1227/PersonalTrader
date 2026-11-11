import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from monitor.engine import MonitorEngine, _is_symbol_close_rule
from monitor.models import Condition, ConditionType, Quote, Rule
from monitor.rules import load_rules
from monitor.store import Store
from monitor.yahoo import uses_yahoo_quote, yahoo_ticker

ROOT = Path(__file__).resolve().parents[1]


def _nasdaq_rule() -> Rule:
    return Rule(
        id="regular-nasdaq-2",
        name="纳斯达克涨跌超2%",
        description="盘中收盘时纳斯达克综指（IXIC）涨超2%或跌超2%",
        group="盘中",
        symbol="^IXIC",
        notify="Important-reminder",
        cooldown_minutes=1200,
        condition=Condition(
            type=ConditionType.REGULAR_CLOSE,
            value=2,
            abs_change=True,
            close_minutes=5,
        ),
        slack_message="{stock_name}{field_gap}{move_text}{price_text}",
    )


def test_rules_yaml_loads_nasdaq_close_rule():
    config = load_rules(ROOT / "config" / "rules.yaml")
    rule = next(rule for rule in config.rules if rule.id == "regular-nasdaq-2")
    assert rule.symbol == "^IXIC"
    assert uses_yahoo_quote(rule.symbol)
    assert yahoo_ticker(rule.symbol) == "^IXIC"
    assert rule.condition.type is ConditionType.REGULAR_CLOSE
    assert rule.condition.abs_change is True
    assert rule.condition.value == 2
    assert rule.condition.close_minutes == 5
    assert rule.notify == "Important-reminder"
    assert "regular-nasdaq-2" in config.important.high_priority_rule_ids
    assert _is_symbol_close_rule(rule)


def test_match_symbol_close_uses_yahoo_for_ixic(tmp_path):
    engine = MonitorEngine.__new__(MonitorEngine)
    engine.store = Store(tmp_path)
    engine.moomoo = object()
    engine._trend_moomoo = None
    engine._scan_log = {}
    rule = _nasdaq_rule()

    async def run() -> None:
        with patch(
            "monitor.yahoo.quote",
            new=AsyncMock(
                return_value=Quote(
                    symbol="IXIC",
                    company="NASDAQ Composite",
                    price=25000.0,
                    change_pct=2.0,
                    updated_at=datetime.now(timezone.utc),
                )
            ),
        ):
            assert await engine._match_symbol_close(rule) == []

        with patch(
            "monitor.yahoo.quote",
            new=AsyncMock(
                return_value=Quote(
                    symbol="IXIC",
                    company="NASDAQ Composite",
                    price=26000.0,
                    change_pct=2.4,
                    updated_at=datetime.now(timezone.utc),
                )
            ),
        ):
            up = await engine._match_symbol_close(rule)
            assert [quote.symbol for quote in up] == ["IXIC"]

        with patch(
            "monitor.yahoo.quote",
            new=AsyncMock(
                return_value=Quote(
                    symbol="IXIC",
                    company="NASDAQ Composite",
                    price=24000.0,
                    change_pct=-2.2,
                    updated_at=datetime.now(timezone.utc),
                )
            ),
        ):
            down = await engine._match_symbol_close(rule)
            assert [quote.symbol for quote in down] == ["IXIC"]

    asyncio.run(run())


def test_yahoo_ixic_live_quote():
    from monitor.yahoo import quote

    result = asyncio.run(quote("^IXIC"))
    assert result is not None
    assert result.symbol == "IXIC"
    assert result.change_pct is not None
    assert result.price is not None and result.price > 0
