# PersonalTrader monitor

Poll US stocks from Alpaca and send Slack when a rule matches.

iOS and IBKR trading come later. This step is only the watcher.

```
Alpaca REST snapshots  →  rules + cooldown  →  Slack
```

## Setup

1. Create a free [Alpaca paper](https://app.alpaca.markets/paper/dashboard/overview) account and generate API keys.
2. Create a Slack [incoming webhook](https://api.slack.com/messaging/webhooks).
3. Install and copy config files:

```bash
make setup
```

4. Put your keys in `.env`:

```
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

5. Confirm Slack, then do one poll, then leave it running:

```bash
make test-slack
make once
make monitor
```

`--force-alert SPY` fetches a live quote and posts to Slack even if no rule matched. Use that to prove the full path.

```bash
.venv/bin/python -m monitor --force-alert SPY
```

## Rules

Edit `config/rules.yaml`. The file is reloaded on every poll, so you can change rules without restarting.

| `type` | Fires when |
|---|---|
| `price_above` | last price ≥ `value` |
| `price_below` | last price ≤ `value` |
| `percent_change` | daily % vs previous close (`-2` = down 2%, `3` = up 3%) |
| `crosses_above` | previous price was below `value`, current is at or above |
| `crosses_below` | previous price was above `value`, current is at or below |
| `premarket_gain` | US pre-market (4:00–9:30 ET) gain ≥ `value`%, and yesterday market cap < `max_market_cap` |

`premarket_gain` is a market-wide scan (`symbol: "*"`). It only runs in the US pre-market session, about once a minute. Yesterday's market cap is `shares outstanding × previous close`. Alpaca does not provide market cap, so that piece comes from Yahoo quote data.

`make monitor` polls REST. `python -m monitor --socket` uses the IEX WebSocket (one connection, 30 symbols on the free plan).

`enabled: false` keeps a rule saved without firing. `cooldown_minutes` stops repeat Slack messages for the same rule. The pre-market scan cools down per symbol.

The sample `SPY down 2%` rule and the pre-market small-cap scan are on.

Slack message tokens: `{name}` `{symbol}` `{price}` `{change_pct}` `{threshold}` `{market_cap}` `{market_cap_m}`.

## Tests

```bash
make test
```
