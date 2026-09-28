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
