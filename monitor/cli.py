from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import TypeVar

from monitor.alpaca import AlpacaClient
from monitor.config import Settings, get_settings
from monitor.engine import MonitorEngine
from monitor.moomoo_client import MoomooClient
from monitor.pushover import PushoverClient
from monitor.rules import load_rules
from monitor.session import is_premarket
from monitor.slack import SlackClient
from monitor.store import Store

T = TypeVar("T")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Watch quotes and send Slack when a rule matches.",
    )
    parser.add_argument(
        "--source",
        choices=("alpaca", "moomoo"),
        default="alpaca",
        help="Quote source. moomoo uses the local OpenD gateway.",
    )
    parser.add_argument(
        "--socket",
        action="store_true",
        help="Use the Alpaca WebSocket stream instead of REST polling.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll once, evaluate rules, then exit.",
    )
    parser.add_argument(
        "--test-slack",
        action="store_true",
        help="Send a Slack test message and exit.",
    )
    parser.add_argument(
        "--test-pushover",
        action="store_true",
        help="Send a Pushover test message and exit.",
    )
    parser.add_argument(
        "--force-alert",
        metavar="SYMBOL",
        help="Fetch a live quote and send a Slack alert for SYMBOL, then exit.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Log every rule evaluation, not only alerts.",
    )
    return parser


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def require_alpaca(settings: Settings) -> None:
    if not settings.alpaca_configured:
        sys.exit("Set ALPACA_API_KEY and ALPACA_API_SECRET in .env")


def require_slack(settings: Settings) -> None:
    if not settings.slack_configured:
        sys.exit("Set SLACK_WEBHOOK_URL in .env")


def require_pushover(settings: Settings) -> None:
    if not settings.pushover_configured:
        sys.exit("Set PUSHOVER_TOKEN and PUSHOVER_USER in .env")


def require_notifier(settings: Settings) -> None:
    if not settings.slack_configured and not settings.pushover_configured:
        sys.exit("Set SLACK_WEBHOOK_URL or PUSHOVER_TOKEN and PUSHOVER_USER in .env")


def print_startup(settings: Settings, verbose: bool, source: str) -> None:
    try:
        config = load_rules(settings.rules_path)
        enabled = [rule for rule in config.rules if rule.enabled]
        symbols = ", ".join(config.all_symbols()) or "(none)"
        print("PersonalTrader monitor")
        print(f"  rules     {len(enabled)} enabled / {len(config.rules)} total")
        print(f"  symbols   {symbols}")
        print(f"  source    {source}")
        print(f"  transport REST poll every {config.poll_interval_seconds}s ({config.feed})")
        print(f"  slack     {'yes' if settings.slack_configured else 'NO'}")
        print(f"  pushover  {'yes' if settings.pushover_configured else 'NO'}")
        if source == "alpaca":
            print(f"  alpaca    {'yes' if settings.alpaca_configured else 'NO'}")
        else:
            print(f"  opend     {settings.moomoo_host}:{settings.moomoo_port}")
        if verbose:
            print("  verbose   on")
        if enabled:
            print("  watching")
            for rule in enabled:
                if rule.is_scanner:
                    volume = ""
                    if rule.condition.min_volume is not None:
                        volume = f" and volume > {rule.condition.min_volume:,.0f}"
                    if rule.condition.min_regular_volume is not None:
                        volume += f" and regular volume > {rule.condition.min_regular_volume:,.0f}"
                    if rule.condition.min_regular_change is not None:
                        volume += f" and regular change > {rule.condition.min_regular_change:g}%"
                    if rule.condition.min_prior_change is not None:
                        volume += (
                            f" and gain > {rule.condition.min_prior_change:g}% "
                            f"before the last {rule.condition.prior_change_lag_minutes}m"
                        )
                    if rule.condition.require_uptrend:
                        volume += " and regular-session trend up"
                    if rule.condition.require_circuit_breaker:
                        volume += " and upward circuit breaker"
                    if rule.condition.skip_market_cap or (
                        rule.condition.max_market_cap is None
                        and rule.condition.require_circuit_breaker
                    ):
                        cap_text = "any market cap"
                    else:
                        cap = rule.condition.max_market_cap or 100_000_000
                        cap_text = f"total cap <= ${cap:,.0f}"
                    comparator = (
                        ">"
                        if rule.condition.min_regular_change is not None
                        or rule.condition.min_prior_change is not None
                        else ">="
                    )
                    print(
                        f"    - {rule.name}: scan {rule.condition.type.value} "
                        f"{comparator}{rule.condition.value:g}% and {cap_text}{volume}"
                    )
                else:
                    print(
                        f"    - {rule.name}: {rule.symbol} {rule.condition.type.value} "
                        f"{rule.condition.value}"
                    )
        else:
            print("  watching  (no enabled rules — nothing will alert)")
        print()
    except FileNotFoundError as exc:
        sys.exit(str(exc))


async def with_engine(
    settings: Settings,
    verbose: bool,
    source: str,
    work: Callable[[MonitorEngine], Awaitable[T]],
) -> T:
    alpaca = AlpacaClient(settings)
    slack = SlackClient(settings)
    pushover = PushoverClient(settings)
    moomoo = MoomooClient(settings) if source == "moomoo" else None
    engine = MonitorEngine(
        settings,
        alpaca,
        slack,
        Store(settings.data_dir),
        verbose=verbose,
        moomoo=moomoo,
        pushover=pushover,
    )
    try:
        return await work(engine)
    finally:
        await engine.close()
        await alpaca.close()
        await slack.close()
        await pushover.close()


async def test_slack(engine: MonitorEngine) -> None:
    await engine.slack.send_text("PersonalTrader test: Slack is connected.")
    print("Sent Slack test message.")


async def test_pushover(engine: MonitorEngine) -> None:
    if engine.pushover is None:
        raise RuntimeError("Pushover client is not configured")
    await engine.pushover.send_text("PersonalTrader test: Pushover is connected.")
    print("Sent Pushover test message.")


async def async_main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)
    settings = get_settings()

    if args.test_slack:
        require_slack(settings)
        await with_engine(settings, args.verbose, args.source, test_slack)
        return

    if args.test_pushover:
        require_pushover(settings)
        await with_engine(settings, args.verbose, args.source, test_pushover)
        return

    if args.source == "alpaca":
        require_alpaca(settings)
    require_notifier(settings)
    print_startup(settings, args.verbose, args.source)

    if args.force_alert:
        symbol = args.force_alert

        async def send_force(engine: MonitorEngine) -> None:
            alert = await engine.force_alert(symbol)
            print(f"Sent alert for {alert.symbol}.")

        await with_engine(settings, args.verbose, args.source, send_force)
        return

    if args.once:

        async def poll_once(engine: MonitorEngine) -> None:
            alerts = await engine.poll_once()
            if args.source == "moomoo" and not is_premarket():
                await engine.preview_premarket()
            if engine.last_error:
                print(f"Finished with warning: {engine.last_error}")
            elif alerts:
                print(f"Fired {len(alerts)} alert(s).")
            else:
                print("No rules matched.")

        await with_engine(settings, args.verbose, args.source, poll_once)
        return

    transport = "socket" if args.socket else "rest"
    await with_engine(
        settings,
        args.verbose,
        args.source,
        lambda engine: engine.run_forever(transport=transport),
    )


def main(argv: list[str] | None = None) -> None:
    try:
        asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        print("\nStopped.")
