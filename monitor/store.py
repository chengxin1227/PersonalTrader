from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from monitor.models import Alert


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


class Store:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._state_path = data_dir / "state.json"
        self._alerts_path = data_dir / "alerts.jsonl"
        self._spikes_path = data_dir / "session_spikes.json"
        self._spikes: dict[str, Any] | None = None
        self._day_gains_path = data_dir / "day_gains.json"
        self._day_gains: dict[str, Any] | None = None
        data_dir.mkdir(parents=True, exist_ok=True)

    def load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {"last_prices": {}, "last_fired": {}}
        with self._state_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload.setdefault("last_prices", {})
        payload.setdefault("last_fired", {})
        return payload

    def save_state(self, state: dict[str, Any]) -> None:
        with self._state_path.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, default=str)

    def last_price(self, state: dict[str, Any], symbol: str) -> float | None:
        value = state.get("last_prices", {}).get(symbol.upper())
        return float(value) if value is not None else None

    def set_last_price(self, state: dict[str, Any], symbol: str, price: float | None) -> None:
        if price is None:
            return
        state.setdefault("last_prices", {})[symbol.upper()] = price

    def last_fired_at(self, state: dict[str, Any], rule_id: str) -> datetime | None:
        value = state.get("last_fired", {}).get(rule_id)
        if not value:
            return None
        return datetime.fromisoformat(value)

    def mark_fired(self, state: dict[str, Any], rule_id: str, when: datetime | None = None) -> None:
        stamp = when or _now()
        state.setdefault("last_fired", {})[rule_id] = stamp.isoformat()

    def in_cooldown(
        self,
        state: dict[str, Any],
        rule_id: str,
        cooldown_minutes: int,
        now: datetime | None = None,
    ) -> bool:
        if cooldown_minutes <= 0:
            return False
        last = self.last_fired_at(state, rule_id)
        if last is None:
            return False
        current = now or _now()
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        elapsed = (current - last).total_seconds()
        return elapsed < cooldown_minutes * 60

    def note_spike(self, day: str, session: str, symbol: str, when: datetime) -> datetime:
        """Remember the first time a symbol's session gain exceeded the prior threshold."""
        spikes = self._load_spikes()
        session_row = spikes.setdefault(day, {}).setdefault(session, {})
        key = symbol.upper()
        existing = session_row.get(key)
        if existing:
            return datetime.fromisoformat(existing)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        session_row[key] = when.isoformat()
        keep = {day}
        if _is_iso_date(day):
            keep.add((date.fromisoformat(day) - timedelta(days=1)).isoformat())
        for stale in [item for item in spikes if item not in keep]:
            spikes.pop(stale, None)
        self._save_spikes(spikes)
        return when

    def spike_seen_at(self, day: str, session: str, symbol: str) -> datetime | None:
        spikes = self._load_spikes()
        raw = spikes.get(day, {}).get(session, {}).get(symbol.upper())
        if not raw:
            return None
        seen = datetime.fromisoformat(raw)
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        return seen

    def session_symbols(self, day: str, session: str) -> list[str]:
        row = self._load_spikes().get(day, {}).get(session, {})
        if not isinstance(row, dict):
            return []
        return sorted(symbol for symbol in row if isinstance(symbol, str))

    def note_day_gains(self, day: str, minimum: float, symbols: list[str]) -> list[str]:
        """Remember symbols whose regular-session gain exceeded `minimum` on `day`."""
        gains = self._load_day_gains()
        bucket = gains.setdefault(f"{minimum:g}", {})
        row = bucket.setdefault(day, [])
        if not isinstance(row, list):
            row = []
            bucket[day] = row
        have = {symbol for symbol in row if isinstance(symbol, str)}
        added: list[str] = []
        for symbol in symbols:
            key = symbol.upper()
            if key in have:
                continue
            row.append(key)
            have.add(key)
            added.append(key)
        if not added:
            return []
        cutoff = date.fromisoformat(day) - timedelta(days=21)
        for days in gains.values():
            if not isinstance(days, dict):
                continue
            for stale in [
                item
                for item in days
                if _is_iso_date(item) and date.fromisoformat(item) < cutoff
            ]:
                days.pop(stale, None)
        self._save_day_gains(gains)
        return added

    def saw_day_gain(self, symbol: str, minimum: float, days: list[str]) -> bool:
        bucket = self._load_day_gains().get(f"{minimum:g}", {})
        if not isinstance(bucket, dict):
            return False
        key = symbol.upper()
        return any(isinstance(bucket.get(day), list) and key in bucket[day] for day in days)

    def _load_day_gains(self) -> dict[str, Any]:
        if self._day_gains is not None:
            return self._day_gains
        if not self._day_gains_path.exists():
            self._day_gains = {}
            return self._day_gains
        with self._day_gains_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self._day_gains = payload if isinstance(payload, dict) else {}
        return self._day_gains

    def _save_day_gains(self, gains: dict[str, Any]) -> None:
        self._day_gains = gains
        with self._day_gains_path.open("w", encoding="utf-8") as handle:
            json.dump(gains, handle, indent=2)

    def _load_spikes(self) -> dict[str, Any]:
        if self._spikes is not None:
            return self._spikes
        if not self._spikes_path.exists():
            self._spikes = {}
            return self._spikes
        with self._spikes_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self._spikes = payload if isinstance(payload, dict) else {}
        return self._spikes

    def _save_spikes(self, spikes: dict[str, Any]) -> None:
        self._spikes = spikes
        with self._spikes_path.open("w", encoding="utf-8") as handle:
            json.dump(spikes, handle, indent=2)

    def append_alert(self, alert: Alert) -> None:
        with self._alerts_path.open("a", encoding="utf-8") as handle:
            handle.write(alert.model_dump_json() + "\n")

    def recent_alerts(self, limit: int = 50) -> list[Alert]:
        if not self._alerts_path.exists():
            return []
        lines = self._alerts_path.read_text(encoding="utf-8").splitlines()
        alerts: list[Alert] = []
        for line in lines[-limit:]:
            if line.strip():
                alerts.append(Alert.model_validate_json(line))
        return list(reversed(alerts))

    def new_alert(
        self,
        *,
        rule_id: str,
        rule_name: str,
        symbol: str,
        message: str,
        price: float | None,
        change_pct: float | None,
        label: str | None = None,
        notify: str | None = None,
    ) -> Alert:
        return Alert(
            id=str(uuid4()),
            rule_id=rule_id,
            rule_name=rule_name,
            label=label,
            notify=notify,
            symbol=symbol,
            message=message,
            price=price,
            change_pct=change_pct,
            fired_at=_now(),
        )
