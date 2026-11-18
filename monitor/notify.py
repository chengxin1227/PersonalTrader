from __future__ import annotations

import os
import re

from dotenv import dotenv_values

from monitor.config import ROOT, Settings

_SUFFIX = re.compile(r"[^A-Za-z0-9]+")

# Pushover "From" is the application name for the API token. Each halt-or-5d OR-leg
# needs its own app named as the Chinese trigger; tokens live under these env keys.
HALT_OR_5D_FROM_ENV = {
    "今日盘中一度涨超50%后续超10%": "PUSHOVER_TOKEN_HALT_FROM_TODAY_50",
    "过去5日曾收盘涨超50%后续超10%": "PUSHOVER_TOKEN_HALT_FROM_PAST_50",
    "今日盘中一度涨超100%后续超5%": "PUSHOVER_TOKEN_HALT_FROM_TODAY_100",
    "过去5日曾收盘涨超100%后续超5%": "PUSHOVER_TOKEN_HALT_FROM_PAST_100",
    "涨停后续超5%": "PUSHOVER_TOKEN_HALT_FROM_LIMIT_UP",
}


def env_suffix(notify: str) -> str:
    return _SUFFIX.sub("_", notify).strip("_").upper()


class NotifyRoutes:
    """Per-rule Slack webhooks and Pushover application tokens.

    A rule's ``notify`` name maps to ``SLACK_WEBHOOK_<NAME>`` and
    ``PUSHOVER_TOKEN_<NAME>``. Missing names use the default webhook and token.
    """

    def __init__(
        self,
        values: dict[str, str],
        default_slack: str = "",
        default_token: str = "",
    ) -> None:
        self._values = {key: value for key, value in values.items() if value}
        self.default_slack = default_slack
        self.default_token = default_token

    @classmethod
    def load(cls, settings: Settings) -> NotifyRoutes:
        return cls(_route_values(), settings.slack_webhook_url, settings.pushover_token)

    def slack_url(self, notify: str | None) -> str:
        return self._pick("SLACK_WEBHOOK_", notify, self.default_slack)

    def pushover_token(self, notify: str | None) -> str:
        return self._pick("PUSHOVER_TOKEN_", notify, self.default_token)

    def halt_or_5d_from_token(self, trigger: str | None) -> str:
        """Token whose Pushover app name is the OR-leg (shown as From)."""
        if not trigger:
            return ""
        key = HALT_OR_5D_FROM_ENV.get(trigger.strip())
        if not key:
            return ""
        return self._values.get(key, "")

    def has_slack(self, notify: str) -> bool:
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
