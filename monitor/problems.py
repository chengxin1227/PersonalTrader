from __future__ import annotations

from datetime import datetime


def problem_notice(text: str) -> tuple[str, str, str] | None:
    """Return a dedupe key, title, and body. Disconnects are handled separately."""
    raw = " ".join(text.split())
    if not raw:
        return None
    lowered = raw.lower()
    if "network interruption" in lowered:
        return None
    if "moomoo" in lowered and any(
        part in lowered for part in ("disconnect", "interruption", "connect", "timeout", "timed out")
    ):
        return None
    if "quota" in lowered:
        detail = _clause(raw, "stock:")
        body = "历史K线额度已用完。"
        if detail:
            body = f"{body}{detail}"
        return "quota:kline", "OpenD 额度用完", body
    if _is_rate_limit(lowered):
        if "after-hours" in lowered or "after hours" in lowered:
            key = "limit:after-hours-rank"
            body = "盘后涨幅榜超过频率限制。"
        elif "pre-market" in lowered or "premarket" in lowered:
            key = "limit:pre-market-rank"
            body = "盘前涨幅榜超过频率限制。"
        elif "snapshot" in lowered:
            key = "limit:snapshot"
            body = "快照接口超过频率限制。"
        elif "kline" in lowered or "k-line" in lowered:
            key = "limit:kline"
            body = "K线接口超过频率限制。"
        elif "filter" in lowered or "screen" in lowered:
            key = "limit:stock-filter"
            body = "选股接口超过频率限制。"
        elif "429" in lowered or "alpaca" in lowered:
            key = "limit:alpaca"
            body = "行情接口超过频率限制。"
        else:
            key = "limit:opend"
            body = "OpenD 超过频率限制。"
        detail = _clause(raw, "maximum")
        if detail:
            body = f"{body}{detail}"
        return key, "OpenD 超限", body
    if lowered.startswith("slack send failed"):
        return "error:slack", "发送失败", "Slack 发送失败。"
    if lowered.startswith("pushover send failed"):
        return "error:pushover", "发送失败", "Pushover 发送失败。"
    first = raw[:180]
    return f"error:{first[:80]}", "扫描出错", first


class ProblemLatch:
    """One notice per problem until it has been quiet, then a new one can send."""

    def __init__(self, quiet_seconds: int = 120) -> None:
        self.quiet_seconds = quiet_seconds
        self._open: set[str] = set()
        self._last: dict[str, datetime] = {}

    def should_send(self, key: str, now: datetime) -> bool:
        last = self._last.get(key)
        if last is not None and (now - last).total_seconds() >= self.quiet_seconds:
            self._open.discard(key)
        self._last[key] = now
        if key in self._open:
            return False
        self._open.add(key)
        return True


def _is_rate_limit(lowered: str) -> bool:
    if "high frequency" in lowered or "too frequent" in lowered:
        return True
    if "429" in lowered or "rate limit" in lowered:
        return True
    return "maximum" in lowered and "times per" in lowered


def _clause(raw: str, marker: str) -> str:
    index = raw.lower().find(marker)
    if index < 0:
        return ""
    sentence = raw[index:].split(".")[0].strip()
    if not sentence:
        return ""
    return f"{sentence}."
