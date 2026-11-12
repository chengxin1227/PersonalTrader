import asyncio
from unittest.mock import AsyncMock

from monitor.config import ROOT
from monitor.engine import MonitorEngine
from monitor.models import Condition, ConditionType, Quote, Rule
from monitor.rules import hour_change_pct, load_rules, price_age_seconds_ago, trim_price_samples


def test_price_age_picks_newest_sample_old_enough():
    samples = [(100.0, 1.0), (140.0, 1.1), (160.0, 1.2), (200.0, 1.5)]
    prior = price_age_seconds_ago(samples, now=200.0, window_seconds=60)
    assert prior == (140.0, 1.1)
    assert hour_change_pct(prior[1], 1.5) == 36.3636


def test_price_age_needs_a_full_window():
    samples = [(170.0, 1.0), (200.0, 1.2)]
    assert price_age_seconds_ago(samples, now=200.0, window_seconds=60) is None


def test_trim_keeps_recent_samples_only():
    samples = [(100.0, 1.0), (150.0, 1.1), (190.0, 1.2)]
    assert trim_price_samples(samples, now=200.0, keep_seconds=60) == [
        (150.0, 1.1),
        (190.0, 1.2),
    ]


def test_live_rules_include_sudden_gain_for_each_session():
    config = load_rules(ROOT / "config" / "rules.yaml")
    sudden = [rule for rule in config.rules if rule.condition.type.value == "sudden_gain"]
    sessions = {rule.condition.late_session for rule in sudden}
    assert sessions == {"premarket", "regular", "afterhours"}
    assert {rule.notify for rule in sudden} == {"Sudden-10"}
    for rule in sudden:
        assert rule.condition.value == 10
        assert rule.condition.pool_min_change == 0
        assert rule.condition.sudden_window_seconds == 60
        assert rule.condition.late_session_hours == 2
        assert rule.condition.min_volume is None
        assert rule.condition.min_market_cap == 1_000_000
        assert rule.confirm_delay_minutes == 10
        assert rule.confirm_value == 10
        assert rule.cooldown_minutes == 240


def _sudden_rule(session: str) -> Rule:
    group = {"premarket": "盘前", "regular": "盘中", "afterhours": "盘后"}[session]
    return Rule(
        id=f"{session}-sudden-10",
        name=f"{session} sudden",
        description="test",
        group=group,
        symbol="*",
        cooldown_minutes=240,
        confirm_delay_minutes=10,
        confirm_value=10,
        notify="Sudden-10",
        condition=Condition(
            type=ConditionType.SUDDEN_GAIN,
            value=10,
            pool_min_change=0,
            sudden_window_seconds=60,
            late_session=session,
            late_session_hours=2,
            min_price=0.1,
            min_market_cap=1_000_000,
            max_market_cap=1_000_000_000,
        ),
    )


def _engine() -> MonitorEngine:
    engine = MonitorEngine.__new__(MonitorEngine)
    engine._sudden_pool = {}
    engine._sudden_prices = {}
    engine._sudden_session = {}
    engine._sudden_discovered_at = {}
    engine._sudden_day = None
    engine._drain_client_problem = AsyncMock()
    return engine


def test_sudden_discover_uses_afterhours_rank(caplog):
    import logging

    engine = _engine()
    client = AsyncMock()
    client.scan_afterhours = AsyncMock(
        return_value=[
            (Quote(symbol="DKI", price=2.0, change_pct=35.0, volume=200_000), None),
            (Quote(symbol="THIN", price=2.0, change_pct=20.0, volume=1_000), None),
            (Quote(symbol="CHEAP", price=0.05, change_pct=40.0, volume=200_000), None),
        ]
    )
    client.smallcap_page = AsyncMock()

    async def run() -> None:
        await engine._discover_sudden_pool(_sudden_rule("afterhours"), client, "afterhours")

    with caplog.at_level(logging.INFO, logger="personaltrader.monitor"):
        asyncio.run(run())
    client.scan_afterhours.assert_awaited_once_with(
        0, 1_000_000_000, min_market_cap=1_000_000
    )
    client.smallcap_page.assert_not_called()
    # No volume floor: low-volume THIN is kept; CHEAP still fails min_price.
    assert engine._sudden_pool["afterhours-sudden-10"] == {"DKI", "THIN"}
    assert any("pool +2 DKI, THIN" in record.message for record in caplog.records)


def test_sudden_discover_uses_premarket_rank():
    engine = _engine()
    client = AsyncMock()
    client.scan_premarket = AsyncMock(
        return_value=[
            (Quote(symbol="AAA", price=3.0, change_pct=12.0, volume=150_000), None),
        ]
    )

    async def run() -> None:
        await engine._discover_sudden_pool(_sudden_rule("premarket"), client, "premarket")

    asyncio.run(run())
    client.scan_premarket.assert_awaited_once_with(
        0, 1_000_000_000, min_market_cap=1_000_000
    )
    assert engine._sudden_pool["premarket-sudden-10"] == {"AAA"}


def test_sudden_discover_uses_regular_gainers():
    engine = _engine()
    client = AsyncMock()
    client.regular_gainers = AsyncMock(
        return_value=[Quote(symbol="BBB", price=4.0, change_pct=15.0, volume=120_000)]
    )

    async def run() -> None:
        await engine._discover_sudden_pool(_sudden_rule("regular"), client, "regular")

    asyncio.run(run())
    client.regular_gainers.assert_awaited_once_with(
        0,
        max_market_cap=1_000_000_000,
        min_volume=None,
        min_price=0.1,
        min_market_cap=1_000_000,
    )
    assert engine._sudden_pool["regular-sudden-10"] == {"BBB"}


def test_sudden_discover_seeds_price_samples():
    engine = _engine()
    client = AsyncMock()
    client.scan_afterhours = AsyncMock(
        return_value=[(Quote(symbol="CRIS", price=0.95, change_pct=40.0, volume=200_000), None)]
    )

    async def run() -> None:
        await engine._discover_sudden_pool(_sudden_rule("afterhours"), client, "afterhours")

    asyncio.run(run())
    samples = engine._sudden_prices["afterhours-sudden-10"]["CRIS"]
    assert len(samples) == 1
    assert samples[0][1] == 0.95


def test_sudden_match_keeps_pool_when_quote_missing():
    from monitor.session import now_et

    engine = _engine()
    engine._sudden_day = now_et().date().isoformat()
    engine._sudden_pool["afterhours-sudden-10"] = {"CRIS", "DKI"}
    engine._sudden_prices["afterhours-sudden-10"] = {
        "CRIS": [(100.0, 1.0)],
        "DKI": [(100.0, 2.0)],
    }
    engine._sudden_discovered_at["afterhours-sudden-10"] = 10**12  # skip rediscover
    client = AsyncMock()
    engine._trend_moomoo = client
    engine.moomoo = client
    engine.verbose = False
    # Only DKI comes back this round — CRIS must stay in the pool with samples.
    client.after_hours_quotes = AsyncMock(
        return_value={"DKI": Quote(symbol="DKI", price=2.1, change_pct=12.0, volume=200_000)}
    )

    async def run() -> None:
        matched = await engine._match_sudden_rule(_sudden_rule("afterhours"))
        assert matched == []

    asyncio.run(run())
    assert engine._sudden_pool["afterhours-sudden-10"] == {"CRIS", "DKI"}
    assert "CRIS" in engine._sudden_prices["afterhours-sudden-10"]


def test_sudden_alert_shows_session_change_not_window_gain():
    import time

    from monitor.session import now_et

    engine = _engine()
    live = time.time()
    engine._sudden_day = now_et().date().isoformat()
    engine._sudden_pool["afterhours-sudden-10"] = {"AAA"}
    # Prior ~60s ago at $1.00; current $1.20 → window +20%, but alert should show session +35%.
    engine._sudden_prices["afterhours-sudden-10"] = {"AAA": [(live - 60.0, 1.0)]}
    engine._sudden_discovered_at["afterhours-sudden-10"] = 10**12
    client = AsyncMock()
    engine._trend_moomoo = client
    engine.moomoo = client
    engine.verbose = False
    client.after_hours_quotes = AsyncMock(
        return_value={
            "AAA": Quote(symbol="AAA", price=1.2, change_pct=35.0, volume=200_000)
        }
    )

    async def run() -> None:
        matched = await engine._match_sudden_rule(_sudden_rule("afterhours"))
        assert len(matched) == 1
        assert matched[0].change_pct == 35.0
        assert matched[0].price == 1.2

    asyncio.run(run())


def test_sudden_match_without_prior_session_floor():
    import time

    from monitor.session import now_et

    engine = _engine()
    live = time.time()
    engine._sudden_day = now_et().date().isoformat()
    engine._sudden_pool["afterhours-sudden-10"] = {"LOW"}
    engine._sudden_prices["afterhours-sudden-10"] = {"LOW": [(live - 60.0, 1.0)]}
    engine._sudden_discovered_at["afterhours-sudden-10"] = 10**12
    client = AsyncMock()
    engine._trend_moomoo = client
    engine.moomoo = client
    engine.verbose = False
    # Session only +5%, but 1-minute window +20% — still a match.
    client.after_hours_quotes = AsyncMock(
        return_value={
            "LOW": Quote(symbol="LOW", price=1.2, change_pct=5.0, volume=200_000)
        }
    )

    async def run() -> None:
        matched = await engine._match_sudden_rule(_sudden_rule("afterhours"))
        assert len(matched) == 1
        assert matched[0].symbol == "LOW"
        assert matched[0].change_pct == 5.0

    asyncio.run(run())


def test_sudden_first_and_hold_both_add_important(tmp_path):
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    from monitor.store import Store

    ET = ZoneInfo("America/New_York")
    store = Store(tmp_path)
    engine = MonitorEngine.__new__(MonitorEngine)
    engine.store = store
    engine._lock = asyncio.Lock()
    engine._symbol_alert_lock = asyncio.Lock()
    engine.moomoo = None
    engine._trend_moomoo = None
    engine.slack = None
    engine.pushover = None
    engine.ths = type("THS", (), {"configured": False})()
    engine.routes = type(
        "Routes",
        (),
        {
            "slack_url": lambda self, _notify=None: "",
            "pushover_token": lambda self, _notify=None: None,
        },
    )()
    engine.last_error = None
    engine._cached_config = type(
        "Cfg",
        (),
        {
            "important": type(
                "Important",
                (),
                {"high_priority_rule_ids": [], "watchlist": None, "watched_symbols": None},
            )()
        },
    )()
    engine._config_loaded_at = datetime.now(timezone.utc)
    delivered: list[str] = []
    ths_calls: list[dict] = []

    async def _deliver(alert, **_kwargs) -> None:
        delivered.append(alert.symbol)

    def _ths(rule, quotes, board=False, important_symbols=None, pin_symbols=None):
        ths_calls.append({"important_symbols": list(important_symbols or [])})

    engine._deliver = _deliver  # type: ignore[method-assign]
    engine._fill_prior_session = AsyncMock()
    engine._add_to_tonghuashun = _ths  # type: ignore[method-assign]

    rule = _sudden_rule("afterhours")
    # 19:00 ET Wed — inside afterhours late window (18:00–20:00).
    t0 = datetime(2026, 11, 11, 19, 0, tzinfo=ET).astimezone(timezone.utc)
    arm_key = "confirm:afterhours-sudden-10:SPIK"
    spike = Quote(
        symbol="SPIK",
        company="Spike Co",
        price=1.2,
        change_pct=5.0,
        volume=200_000,
        updated_at=t0,
    )

    async def run() -> None:
        state = store.load_state()
        # Legacy shared Sudden-10 arm must be cleared, not held.
        state["confirm_armed"]["confirm:Sudden-10:OLD"] = t0.isoformat()
        with patch("monitor.store._now", return_value=t0):
            await engine._emit_sudden_first_alerts(rule, [spike], state)
        assert delivered == ["SPIK"]
        assert ths_calls[-1]["important_symbols"] == ["SPIK"]
        assert arm_key in state["confirm_armed"]
        assert "confirm:Sudden-10:OLD" not in state["confirm_armed"]
        assert "Sudden-10:SPIK" not in state.get("last_fired", {})

        # Second spike while armed: no duplicate first alert.
        with patch("monitor.store._now", return_value=t0 + timedelta(seconds=1)):
            await engine._emit_sudden_first_alerts(rule, [spike], state)
        assert delivered == ["SPIK"]

        # Hold before delay: nothing.
        engine._sudden_session_quotes = AsyncMock(  # type: ignore[method-assign]
            return_value={"SPIK": spike}
        )
        engine._trend_moomoo = object()
        with patch("monitor.store._now", return_value=t0 + timedelta(minutes=1)):
            await engine._emit_sudden_hold_alerts(rule, state)
        assert delivered == ["SPIK"]
        assert arm_key in state["confirm_armed"]

        held = Quote(
            symbol="SPIK",
            company="Spike Co",
            price=1.3,
            change_pct=12.0,
            volume=200_000,
            updated_at=t0 + timedelta(minutes=11),
        )
        engine._sudden_session_quotes = AsyncMock(return_value={"SPIK": held})  # type: ignore[method-assign]
        with patch("monitor.store._now", return_value=t0 + timedelta(minutes=11)):
            await engine._emit_sudden_hold_alerts(rule, state)
        assert delivered == ["SPIK", "SPIK"]
        assert ths_calls[-1]["important_symbols"] == ["SPIK"]
        assert "Sudden-10:SPIK" in state["last_fired"]
        # Hold is one-shot — arm cleared so it cannot re-fire on reopen.
        assert arm_key not in state["confirm_armed"]

        # Regular rule must not consume afterhours arms (keys are rule-scoped).
        reg = _sudden_rule("regular")
        state["confirm_armed"][arm_key] = t0.isoformat()
        with patch("monitor.store._now", return_value=t0 + timedelta(minutes=20)):
            await engine._emit_sudden_hold_alerts(reg, state)
        assert delivered == ["SPIK", "SPIK"]
        assert arm_key in state["confirm_armed"]

    asyncio.run(run())


def test_moomoo_scanner_skips_sudden_gain():
    """Sudden must not be treated as a premarket/afterhours session-gain screen."""
    async def run():
        engine = MonitorEngine.__new__(MonitorEngine)
        client = AsyncMock()
        client.scan_premarket = AsyncMock(return_value=[])
        client.scan_afterhours = AsyncMock(return_value=[])
        engine.moomoo = client
        fired, err = await engine._run_moomoo_scanner(_sudden_rule("afterhours"), {})
        assert fired == []
        assert err is None
        client.scan_premarket.assert_not_called()
        client.scan_afterhours.assert_not_called()

    asyncio.run(run())
