from datetime import datetime, timedelta, timezone

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
