from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from monitor.models import Alert


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._state_path = data_dir / "state.json"
        self._alerts_path = data_dir / "alerts.jsonl"
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
    ) -> Alert:
        return Alert(
            id=str(uuid4()),
            rule_id=rule_id,
            rule_name=rule_name,
            symbol=symbol,
            message=message,
            price=price,
            change_pct=change_pct,
            fired_at=_now(),
        )
