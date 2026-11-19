from __future__ import annotations

import os
import re

from dotenv import dotenv_values

from monitor.config import ROOT, Settings

_SUFFIX = re.compile(r"[^A-Za-z0-9]+")

# Pushover From = application name. One app per ET session (今日盘前/盘中/盘后自选).
SESSION_PUSHOVER_ENV = {
    "盘前": "PUSHOVER_TOKEN_PREMARKET",
    "盘中": "PUSHOVER_TOKEN_REGULAR",
    "盘后": "PUSHOVER_TOKEN_AFTERHOURS",
}


def env_suffix(notify: str) -> str:
    return _SUFFIX.sub("_", notify).strip("_").upper()


class NotifyRoutes:
    """Slack webhooks (optional) and session Pushover application tokens.

    Market alerts use the session token (盘前/盘中/盘后 → 今日盘前/盘中/盘后自选).
    A rule's ``notify`` name still maps to ``SLACK_WEBHOOK_<NAME>`` when Slack is on.
    Per-rule ``PUSHOVER_TOKEN_<NAME>`` remains as a fallback only when no session token.
    """

    def __init__(
        self,
        values: dict[str, str],
        default_slack: str = "",
        default_token: str = "",
        *,
        slack_enabled: bool = True,
    ) -> None:
        self._values = {key: value for key, value in values.items() if value}
        self.default_slack = default_slack
        self.default_token = default_token
        self.slack_enabled = slack_enabled

    @classmethod
    def load(cls, settings: Settings) -> NotifyRoutes:
        return cls(
            _route_values(),
            settings.slack_webhook_url,
            settings.pushover_token,
            slack_enabled=settings.slack_enabled,
        )

    def slack_url(self, notify: str | None) -> str:
        if not self.slack_enabled:
            return ""
        return self._pick("SLACK_WEBHOOK_", notify, self.default_slack)

    def pushover_token(self, notify: str | None) -> str:
        return self._pick("PUSHOVER_TOKEN_", notify, self.default_token)

    def session_pushover_token(self, session: str | None) -> str:
        """Token for 今日盘前/盘中/盘后自选; falls back to default ``PUSHOVER_TOKEN``."""
        key = SESSION_PUSHOVER_ENV.get((session or "").strip())
        if key:
            token = self._values.get(key, "")
            if token:
                return token
        return self.default_token

    def has_session_pushover(self, session: str | None) -> bool:
        return bool(self.session_pushover_token(session))

    def has_slack(self, notify: str) -> bool:
        if not self.slack_enabled:
            return False
        return bool(self._values.get(f"SLACK_WEBHOOK_{env_suffix(notify)}"))

    def has_pushover(self, notify: str) -> bool:
        return bool(self._values.get(f"PUSHOVER_TOKEN_{env_suffix(notify)}"))

    def _pick(self, prefix: str, notify: str | None, default: str) -> str:
        if notify:
            specific = self._values.get(f"{prefix}{env_suffix(notify)}")
            if specific:
                return specific
        return default


def _route_values() -> dict[str, str]:
    found: dict[str, str] = {}
    for key, value in dotenv_values(ROOT / ".env").items():
        if key and value and _is_route_key(key):
            found[key] = value
    for key, value in os.environ.items():
        if value and _is_route_key(key):
            found[key] = value
    return found


def _is_route_key(key: str) -> bool:
    if key in {"SLACK_WEBHOOK_URL", "PUSHOVER_TOKEN"}:
        return False
    return key.startswith("SLACK_WEBHOOK_") or key.startswith("PUSHOVER_TOKEN_")
