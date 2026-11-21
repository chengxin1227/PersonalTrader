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
    if lowered.startswith("tonghuashun add failed"):
        # Timeouts share one key so the latch can remind at most every remind_seconds.
        if any(
            part in lowered
            for part in ("timed out", "timeout", "handshake", "temporarily")
        ):
            return (
                "error:tonghuashun:timeout",
                "加自选超时",
                "同花顺加自选超时（网络或握手）。",
            )
        symbols = _ths_fail_symbols(raw)
        if symbols:
            return (
                f"error:tonghuashun:{symbols}",
                f"加自选失败 {symbols}",
                f"同花顺加自选失败：{symbols}。",
            )
        return "error:tonghuashun", "加自选失败", "同花顺加自选失败。"
    if lowered.startswith("slack send failed"):
        return "error:slack", "发送失败", "Slack 发送失败。"
    if lowered.startswith("pushover send failed"):
        return "error:pushover", "发送失败", "Pushover 发送失败。"
    if _is_state_corrupt(lowered):
        return (
            "error:state-corrupt",
            "监控已暂停",
            "state.json 损坏，扫描已停，请修复后重启。",
        )
    first = raw[:180]
    return f"error:{first[:80]}", "扫描出错", first


def _is_state_corrupt(lowered: str) -> bool:
    if "expecting" in lowered and ("delimiter" in lowered or "value" in lowered):
        return True
    if "json" in lowered and any(
        part in lowered for part in ("decode", "parse", "expecting", "unterminated")
    ):
        return True
    return "state.json" in lowered and any(
        part in lowered for part in ("corrupt", "invalid", "decode", "parse")
    )


class ProblemLatch:
    """Dedupe problem notices with an optional periodic re-remind.

    - ``quiet_seconds``: if no report for this long, the next one is a fresh alert.
    - ``remind_seconds``: while reports keep coming, send at most this often
      (default 30 minutes). ``None`` means only one alert until quiet.
    """

    def __init__(
        self, quiet_seconds: int = 120, remind_seconds: int | None = 1800
    ) -> None:
        self.quiet_seconds = quiet_seconds
        self.remind_seconds = remind_seconds
        self._last: dict[str, datetime] = {}
        self._sent: dict[str, datetime] = {}

    def should_send(self, key: str, now: datetime) -> bool:
        last = self._last.get(key)
        # Require a quiet stretch at least as long as the remind interval, otherwise
        # a board that fails every 5 minutes would look "quiet" every time (quiet=2m)
        # and spam a fresh alert.
        reset_after = self.quiet_seconds
        if self.remind_seconds is not None:
            reset_after = max(reset_after, self.remind_seconds)
        if last is not None and (now - last).total_seconds() >= reset_after:
            self._sent.pop(key, None)
        self._last[key] = now
        sent = self._sent.get(key)
        if sent is None:
            self._sent[key] = now
            return True
        if self.remind_seconds is None:
            return False
        if (now - sent).total_seconds() >= self.remind_seconds:
            self._sent[key] = now
            return True
        return False


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


def _ths_fail_symbols(raw: str) -> str:
    """Parse ``Tonghuashun add failed for AAPL, TSLA: ...`` → ``AAPL TSLA``."""
    lowered = raw.lower()
    marker = "tonghuashun add failed for "
    start = lowered.find(marker)
    if start < 0:
        return ""
    rest = raw[start + len(marker) :]
    cut = rest.find(":")
    chunk = rest[:cut] if cut >= 0 else rest
    symbols: list[str] = []
    seen: set[str] = set()
    for part in chunk.replace("，", ",").replace("、", ",").split(","):
        symbol = part.strip().upper()
        if not symbol or not symbol.replace(".", "").isalnum():
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return " ".join(symbols)
