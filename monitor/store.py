from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from monitor.models import Alert

_ET = ZoneInfo("America/New_York")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON via temp file + replace so a crash can't truncate the live file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


class Store:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._state_path = data_dir / "state.json"
        self._alerts_path = data_dir / "alerts.jsonl"
        self._spikes_path = data_dir / "session_spikes.json"
        self._spikes: dict[str, Any] | None = None
        self._day_gains_path = data_dir / "day_gains.json"
        self._day_gains: dict[str, Any] | None = None
        self._peak_gains_path = data_dir / "peak_gains.json"
        self._peak_gains: dict[str, Any] | None = None
        self._afterhours_peaks_path = data_dir / "afterhours_peaks.json"
        self._afterhours_peaks: dict[str, Any] | None = None
        self._premarket_peaks_path = data_dir / "premarket_peaks.json"
        self._premarket_peaks: dict[str, Any] | None = None
        self._new_highs_path = data_dir / "session_new_highs.json"
        self._new_highs: dict[str, Any] | None = None
        self._trend_watch_path = data_dir / "trend_watch.json"
        self._trend_watch: dict[str, Any] | None = None
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
        _atomic_write_json(self._state_path, state)

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
        self._prune_day_gains(gains, day)
        self._save_day_gains(gains)
        return added

    def replace_day_gains(self, day: str, minimum: float, symbols: list[str]) -> None:
        """Replace one day's names. A later check near the close drops names that faded."""
        gains = self._load_day_gains()
        bucket = gains.setdefault(f"{minimum:g}", {})
        row: list[str] = []
        have: set[str] = set()
        for symbol in symbols:
            key = symbol.upper()
            if key in have:
                continue
            row.append(key)
            have.add(key)
        bucket[day] = row
        self._prune_day_gains(gains, day)
        self._save_day_gains(gains)

    def _prune_day_gains(self, gains: dict[str, Any], day: str) -> None:
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

    def day_gain_symbols(self, minimum: float, days: list[str]) -> list[str]:
        bucket = self._load_day_gains().get(f"{minimum:g}", {})
        if not isinstance(bucket, dict):
            return []
        found: set[str] = set()
        for day in days:
            row = bucket.get(day, [])
            if isinstance(row, list):
                found.update(symbol for symbol in row if isinstance(symbol, str))
        return sorted(found)

    def saw_day_gain(self, symbol: str, minimum: float, days: list[str]) -> bool:
        bucket = self._load_day_gains().get(f"{minimum:g}", {})
        if not isinstance(bucket, dict):
            return False
        key = symbol.upper()
        return any(isinstance(bucket.get(day), list) and key in bucket[day] for day in days)

    def note_peak_gain(self, day: str, symbol: str, change_pct: float) -> float:
        """Remember the highest regular-session change seen for `symbol` on `day`."""
        peaks = self._load_peak_gains()
        row = peaks.setdefault(day, {})
        if not isinstance(row, dict):
            row = {}
            peaks[day] = row
        key = symbol.upper()
        existing = row.get(key)
        if existing is not None:
            try:
                prior = float(existing)
            except (TypeError, ValueError):
                prior = float("-inf")
            if prior >= change_pct:
                return prior
        row[key] = change_pct
        self._prune_peak_gains(peaks, day)
        self._save_peak_gains(peaks)
        return change_pct

    def peak_gain(self, day: str, symbol: str) -> float | None:
        row = self._load_peak_gains().get(day, {})
        if not isinstance(row, dict):
            return None
        value = row.get(symbol.upper())
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def peak_gains(self, day: str) -> dict[str, float]:
        row = self._load_peak_gains().get(day, {})
        if not isinstance(row, dict):
            return {}
        out: dict[str, float] = {}
        for symbol, value in row.items():
            if not isinstance(symbol, str):
                continue
            try:
                out[symbol.upper()] = float(value)
            except (TypeError, ValueError):
                continue
        return out

    def _prune_peak_gains(self, peaks: dict[str, Any], day: str) -> None:
        cutoff = date.fromisoformat(day) - timedelta(days=21)
        for stale in [
            item
            for item in peaks
            if _is_iso_date(item) and date.fromisoformat(item) < cutoff
        ]:
            peaks.pop(stale, None)

    def _load_peak_gains(self) -> dict[str, Any]:
        if self._peak_gains is not None:
            return self._peak_gains
        if not self._peak_gains_path.exists():
            self._peak_gains = {}
            return self._peak_gains
        with self._peak_gains_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self._peak_gains = payload if isinstance(payload, dict) else {}
        return self._peak_gains

    def _save_peak_gains(self, peaks: dict[str, Any]) -> None:
        self._peak_gains = peaks
        _atomic_write_json(self._peak_gains_path, peaks)

    def note_afterhours_peak(self, day: str, symbol: str, change_pct: float) -> float:
        """Remember the highest after-hours change seen for `symbol` on `day`."""
        peaks = self._load_afterhours_peaks()
        row = peaks.setdefault(day, {})
        if not isinstance(row, dict):
            row = {}
            peaks[day] = row
        key = symbol.upper()
        existing = row.get(key)
        if existing is not None:
            try:
                prior = float(existing)
            except (TypeError, ValueError):
                prior = float("-inf")
            if prior >= change_pct:
                return prior
        row[key] = change_pct
        self._prune_afterhours_peaks(peaks, day)
        self._save_afterhours_peaks(peaks)
        return change_pct

    def afterhours_peak(self, day: str, symbol: str) -> float | None:
        row = self._load_afterhours_peaks().get(day, {})
        if not isinstance(row, dict):
            return None
        value = row.get(symbol.upper())
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def afterhours_peaks(self, day: str) -> dict[str, float]:
        row = self._load_afterhours_peaks().get(day, {})
        if not isinstance(row, dict):
            return {}
        out: dict[str, float] = {}
        for symbol, value in row.items():
            if not isinstance(symbol, str):
                continue
            try:
                out[symbol.upper()] = float(value)
            except (TypeError, ValueError):
                continue
        return out

    def _prune_afterhours_peaks(self, peaks: dict[str, Any], day: str) -> None:
        cutoff = date.fromisoformat(day) - timedelta(days=21)
        for stale in [
            item
            for item in peaks
            if _is_iso_date(item) and date.fromisoformat(item) < cutoff
        ]:
            peaks.pop(stale, None)

    def _load_afterhours_peaks(self) -> dict[str, Any]:
        if self._afterhours_peaks is not None:
            return self._afterhours_peaks
        if not self._afterhours_peaks_path.exists():
            self._afterhours_peaks = {}
            return self._afterhours_peaks
        with self._afterhours_peaks_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self._afterhours_peaks = payload if isinstance(payload, dict) else {}
        return self._afterhours_peaks

    def _save_afterhours_peaks(self, peaks: dict[str, Any]) -> None:
        self._afterhours_peaks = peaks
        _atomic_write_json(self._afterhours_peaks_path, peaks)

    def note_premarket_peak(self, day: str, symbol: str, change_pct: float) -> float:
        """Remember the highest pre-market change seen for `symbol` on `day`."""
        peaks = self._load_premarket_peaks()
        row = peaks.setdefault(day, {})
        if not isinstance(row, dict):
            row = {}
            peaks[day] = row
        key = symbol.upper()
        existing = row.get(key)
        if existing is not None:
            try:
                prior = float(existing)
            except (TypeError, ValueError):
                prior = float("-inf")
            if prior >= change_pct:
                return prior
        row[key] = change_pct
        self._prune_premarket_peaks(peaks, day)
        self._save_premarket_peaks(peaks)
        return change_pct

    def premarket_peaks(self, day: str) -> dict[str, float]:
        row = self._load_premarket_peaks().get(day, {})
        if not isinstance(row, dict):
            return {}
        out: dict[str, float] = {}
        for symbol, value in row.items():
            if not isinstance(symbol, str):
                continue
            try:
                out[symbol.upper()] = float(value)
            except (TypeError, ValueError):
                continue
        return out

    def _prune_premarket_peaks(self, peaks: dict[str, Any], day: str) -> None:
        cutoff = date.fromisoformat(day) - timedelta(days=21)
        for stale in [
            item
            for item in peaks
            if _is_iso_date(item) and date.fromisoformat(item) < cutoff
        ]:
            peaks.pop(stale, None)

    def _load_premarket_peaks(self) -> dict[str, Any]:
        if self._premarket_peaks is not None:
            return self._premarket_peaks
        if not self._premarket_peaks_path.exists():
            self._premarket_peaks = {}
            return self._premarket_peaks
        with self._premarket_peaks_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self._premarket_peaks = payload if isinstance(payload, dict) else {}
        return self._premarket_peaks

    def _save_premarket_peaks(self, peaks: dict[str, Any]) -> None:
        self._premarket_peaks = peaks
        _atomic_write_json(self._premarket_peaks_path, peaks)

    def new_high_watch(
        self, day: str, rule_id: str, symbol: str
    ) -> tuple[datetime, float] | None:
        """First time above the entry threshold, and the highest gain seen since."""
        row = self._load_new_highs().get(day, {}).get(rule_id, {}).get(symbol.upper())
        if not isinstance(row, dict):
            return None
        raw_at = row.get("first_at")
        raw_peak = row.get("peak")
        if not raw_at or raw_peak is None:
            return None
        try:
            peak = float(raw_peak)
        except (TypeError, ValueError):
            return None
        seen = datetime.fromisoformat(str(raw_at))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        return seen, peak

    def note_new_high_watch(
        self,
        day: str,
        rule_id: str,
        symbol: str,
        change_pct: float,
        when: datetime,
    ) -> tuple[datetime, float]:
        """Start watching a name the first time it clears the entry threshold."""
        highs = self._load_new_highs()
        rule_row = highs.setdefault(day, {}).setdefault(rule_id, {})
        key = symbol.upper()
        existing = rule_row.get(key)
        if isinstance(existing, dict) and existing.get("first_at") is not None:
            watched = self.new_high_watch(day, rule_id, key)
            if watched is not None:
                return watched
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        rule_row[key] = {"first_at": when.isoformat(), "peak": float(change_pct)}
        self._prune_new_highs(highs, day)
        self._save_new_highs(highs)
        return when, float(change_pct)

    def raise_new_high_peak(
        self, day: str, rule_id: str, symbol: str, change_pct: float
    ) -> float | None:
        """Raise the watched peak when `change_pct` is higher. Returns the previous peak."""
        highs = self._load_new_highs()
        rule_row = highs.setdefault(day, {}).setdefault(rule_id, {})
        key = symbol.upper()
        existing = rule_row.get(key)
        if not isinstance(existing, dict):
            return None
        try:
            prior = float(existing.get("peak"))
        except (TypeError, ValueError):
            return None
        if change_pct <= prior:
            return prior
        existing["peak"] = float(change_pct)
        rule_row[key] = existing
        self._save_new_highs(highs)
        return prior

    def _prune_new_highs(self, highs: dict[str, Any], day: str) -> None:
        keep = {day}
        if _is_iso_date(day):
            keep.add((date.fromisoformat(day) - timedelta(days=1)).isoformat())
        for stale in [item for item in highs if item not in keep]:
            highs.pop(stale, None)

    def _load_new_highs(self) -> dict[str, Any]:
        if self._new_highs is not None:
            return self._new_highs
        if not self._new_highs_path.exists():
            self._new_highs = {}
            return self._new_highs
        with self._new_highs_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self._new_highs = payload if isinstance(payload, dict) else {}
        return self._new_highs

    def _save_new_highs(self, highs: dict[str, Any]) -> None:
        self._new_highs = highs
        _atomic_write_json(self._new_highs_path, highs)

    def replace_trend_watch(self, rule_id: str, day: str, rows: list[dict[str, Any]]) -> None:
        """Replace today's trend names. A later check near the close drops names that faded."""
        watch = self._load_trend_watch()
        rule_row = watch.setdefault(rule_id, {})
        rule_row[day] = rows
        cutoff = date.fromisoformat(day) - timedelta(days=21)
        for days in watch.values():
            if not isinstance(days, dict):
                continue
            for stale in [
                item
                for item in days
                if _is_iso_date(item) and date.fromisoformat(item) < cutoff
            ]:
                days.pop(stale, None)
        self._save_trend_watch(watch)

    def trend_watch(self, rule_id: str, day: str) -> list[dict[str, Any]]:
        row = self._load_trend_watch().get(rule_id, {}).get(day, [])
        if not isinstance(row, list):
            return []
        return [item for item in row if isinstance(item, dict) and item.get("symbol")]

    def _load_trend_watch(self) -> dict[str, Any]:
        if self._trend_watch is not None:
            return self._trend_watch
        if not self._trend_watch_path.exists():
            self._trend_watch = {}
            return self._trend_watch
        with self._trend_watch_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self._trend_watch = payload if isinstance(payload, dict) else {}
        return self._trend_watch

    def _save_trend_watch(self, watch: dict[str, Any]) -> None:
        self._trend_watch = watch
        _atomic_write_json(self._trend_watch_path, watch)

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
        _atomic_write_json(self._day_gains_path, gains)

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
        _atomic_write_json(self._spikes_path, spikes)

    def append_alert(self, alert: Alert) -> None:
        with self._alerts_path.open("a", encoding="utf-8") as handle:
            handle.write(alert.model_dump_json() + "\n")

    def symbol_alerts_in_session(self, symbol: str, session: str, day: date) -> list[Alert]:
        """Alerts for ``symbol`` in one ET session window (盘前/盘中/盘后) on ``day``.

        Oldest first. Count resets each session, not across the whole day.
        """
        from monitor.session import et_session_name

        if not self._alerts_path.exists() or not session:
            return []
        wanted = symbol.strip().upper()
        found: list[Alert] = []
        for line in self._alerts_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            alert = Alert.model_validate_json(line)
            if alert.symbol.upper() != wanted:
                continue
            fired = alert.fired_at
            if fired.tzinfo is None:
                fired = fired.replace(tzinfo=timezone.utc)
            local = fired.astimezone(_ET)
            if local.date() != day:
                continue
            if et_session_name(local) != session:
                continue
            found.append(alert)
        found.sort(key=lambda item: item.fired_at)
        return found

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
