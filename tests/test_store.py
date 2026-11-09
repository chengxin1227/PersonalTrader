from datetime import date, datetime, timedelta, timezone

from monitor.models import Alert
from monitor.store import Store


def test_cooldown_and_price_state(tmp_path):
    store = Store(tmp_path)
    state = store.load_state()
    now = datetime.now(timezone.utc)

    assert store.last_price(state, "AAPL") is None
    store.set_last_price(state, "aapl", 190.5)
    assert store.last_price(state, "AAPL") == 190.5

    store.mark_fired(state, "rule-1", now - timedelta(minutes=10))
    assert store.in_cooldown(state, "rule-1", 60, now)
    assert not store.in_cooldown(state, "rule-1", 5, now)
    assert not store.in_cooldown(state, "missing", 60, now)

    first = now - timedelta(minutes=6)
    assert store.note_spike("2026-10-02", "premarket", "amod", first) == first
    assert store.note_spike("2026-10-02", "premarket", "AMOD", now) == first
    assert store.spike_seen_at("2026-10-02", "premarket", "AMOD") == first
    assert store.spike_seen_at("2026-10-02", "premarket", "NIVF") is None


def test_day_gains_keep_every_name_seen_above_the_threshold(tmp_path):
    store = Store(tmp_path)
    assert store.note_day_gains("2026-10-08", 50, ["mi", "OLB"]) == ["MI", "OLB"]
    assert store.note_day_gains("2026-10-08", 50, ["MI", "CDT"]) == ["CDT"]
    assert store.saw_day_gain("mi", 50, ["2026-10-08", "2026-10-07"])
    assert not store.saw_day_gain("MI", 50, ["2026-10-07"])
    assert not store.saw_day_gain("MI", 60, ["2026-10-08"])
    store.note_day_gains("2026-10-07", 50, ["CDT"])
    store.note_day_gains("2026-10-09", 90, ["ZYBT"])
    assert store.day_gain_symbols(50, ["2026-10-08", "2026-10-07"]) == ["CDT", "MI", "OLB"]
    assert store.day_gain_symbols(90, ["2026-10-09"]) == ["ZYBT"]


def test_closing_day_gains_drop_names_that_faded(tmp_path):
    store = Store(tmp_path)
    store.replace_day_gains("2026-10-09", 50, ["ZYBT", "veea", "HCMC"])
    store.replace_day_gains("2026-10-09", 50, ["zybt"])
    assert store.day_gain_symbols(50, ["2026-10-09"]) == ["ZYBT"]


def test_new_high_watch_tracks_first_touch_and_peak(tmp_path):
    store = Store(tmp_path)
    now = datetime.now(timezone.utc)
    first, peak = store.note_new_high_watch(
        "2026-10-30", "premarket-30-newhigh", "brai", 32.0, now
    )
    assert first == now
    assert peak == 32.0
    watched = store.new_high_watch("2026-10-30", "premarket-30-newhigh", "BRAI")
    assert watched == (now, 32.0)
    assert store.raise_new_high_peak("2026-10-30", "premarket-30-newhigh", "BRAI", 31.0) == 32.0
    assert store.raise_new_high_peak("2026-10-30", "premarket-30-newhigh", "BRAI", 40.5) == 32.0
    assert store.new_high_watch("2026-10-30", "premarket-30-newhigh", "BRAI") == (now, 40.5)
    again, again_peak = store.note_new_high_watch(
        "2026-10-30", "premarket-30-newhigh", "BRAI", 50.0, now + timedelta(minutes=1)
    )
    assert again == now
    assert again_peak == 40.5


def test_peak_gains_keep_the_intraday_high(tmp_path):
    store = Store(tmp_path)
    assert store.note_peak_gain("2026-10-09", "aaa", 82.0) == 82.0
    assert store.note_peak_gain("2026-10-09", "AAA", 90.5) == 90.5
    assert store.note_peak_gain("2026-10-09", "aaa", 88.0) == 90.5
    assert store.peak_gain("2026-10-09", "aaa") == 90.5
    assert store.peak_gains("2026-10-09") == {"AAA": 90.5}
    assert store.peak_gain("2026-10-08", "AAA") is None


def test_afterhours_peaks_stay_separate_from_regular_peaks(tmp_path):
    store = Store(tmp_path)
    store.note_peak_gain("2026-10-09", "AAA", 90.0)
    assert store.note_afterhours_peak("2026-10-09", "aaa", 55.0) == 55.0
    assert store.note_afterhours_peak("2026-10-09", "AAA", 72.5) == 72.5
    assert store.note_afterhours_peak("2026-10-09", "aaa", 60.0) == 72.5
    assert store.afterhours_peak("2026-10-09", "aaa") == 72.5
    assert store.afterhours_peaks("2026-10-09") == {"AAA": 72.5}
    assert store.peak_gain("2026-10-09", "AAA") == 90.0
    assert store.afterhours_peak("2026-10-08", "AAA") is None


def test_trend_watch_replaces_the_close_list(tmp_path):
    store = Store(tmp_path)
    store.replace_trend_watch(
        "trend",
        "2026-10-09",
        [{"symbol": "AAA", "change": 25, "volume": 2_000_000, "company": "Aaa"}],
    )
    store.replace_trend_watch(
        "trend",
        "2026-10-09",
        [{"symbol": "BBB", "change": 22, "volume": 1_500_000, "company": "Bbb"}],
    )
    saved = store.trend_watch("trend", "2026-10-09")
    assert [item["symbol"] for item in saved] == ["BBB"]
    assert store.trend_watch("trend", "2026-10-08") == []


def test_symbol_alerts_in_session_reset_each_session(tmp_path):
    store = Store(tmp_path)
    day = date(2026, 11, 3)
    # 07:00 ET premarket, 09:00 ET still premarket, 10:00 ET regular
    pre_a = Alert(
        id="1",
        rule_id="premarket-a",
        rule_name="a",
        symbol="INLF",
        message="pre-1",
        price=0.3,
        change_pct=10.0,
        fired_at=datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc),
    )
    pre_b = Alert(
        id="2",
        rule_id="premarket-b",
        rule_name="b",
        symbol="INLF",
        message="pre-2",
        price=0.37,
        change_pct=27.6,
        fired_at=datetime(2026, 11, 3, 14, 0, tzinfo=timezone.utc),
    )
    regular = Alert(
        id="3",
        rule_id="regular-a",
        rule_name="c",
        symbol="INLF",
        message="regular-1",
        price=0.4,
        change_pct=30.0,
        fired_at=datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc),
    )
    other_symbol = Alert(
        id="4",
        rule_id="premarket-a",
        rule_name="a",
        symbol="ONEG",
        message="other",
        price=1.0,
        change_pct=6.0,
        fired_at=datetime(2026, 11, 3, 13, 0, tzinfo=timezone.utc),
    )
    for alert in (pre_b, regular, pre_a, other_symbol):
        store.append_alert(alert)
    assert [item.message for item in store.symbol_alerts_in_session("inlf", "盘前", day)] == [
        "pre-1",
        "pre-2",
    ]
    assert [item.message for item in store.symbol_alerts_in_session("inlf", "盘中", day)] == [
        "regular-1"
    ]
    assert store.symbol_alerts_in_session("inlf", "盘后", day) == []
