# Live runbook — Opening Drive Momentum (Si, CR)

Exact procedure the executor (Claude via T-Invest MCP) follows on each scheduled run.
Account: `2000062287`. All times are Moscow time (MSK). Weekdays only.

## Strategy (summary)
- Instruments: front quarterly `Si` (USD/RUB) and `CR` (CNY/RUB), roll 7 days before expiry.
- At 11:00, 12:00, 13:00, 14:00, 15:00: if the 5-min close is outside
  `O*(1 ± 2.0*sigma_t)` (O = 10:00 open, sigma_t = 14-day mean |move from open| at that
  time of day) — enter in the breakout direction. **One trade per instrument per day.**
- Protective stop: stop-loss at today's 10:00 open price (exchange stop order).
- Exit: market close at 23:40. Never hold overnight.
- Size: `live_config.json` → `min_qty..max_qty`, risk-based `risk` of equity in between.

## Checkpoint run (11:01, 12:01, 13:01, 14:01, 15:01)
1. `python3 src/live.py signal --equity <liquidPortfolio>` (equity from `invest_get_broker_account_margin`).
2. For each instrument with `action` BUY/SELL:
   1. `invest_get_positions` — if a position in that ticker already exists, do nothing.
   2. `invest_get_max_lots` for `instrumentId`; reduce `qty` to what margin allows; skip if 0.
   3. `invest_create_order`: MARKET, direction = action, quantity = qty, `confirmMarginTrade: true`.
   4. `invest_create_stoporder`: STOP_LOSS, direction = `stop_direction`, quantity = qty,
      stopPrice = `stop_price`, EXCHANGE_ORDER_TYPE_MARKET, GOOD_TILL_DATE, expireDate = today 20:45 UTC.
      If the market already traded through the stop, close immediately with a market order instead.
3. `action` NONE / ERROR → no orders; ERROR is reported to the user.
4. Append a line to the run log (chat summary): time, signals, orders, fills.

## Exit run (23:40)
1. `invest_get_stoporders` → cancel active stop orders for the strategy tickers.
2. `invest_get_positions` → for each Si*/CR* futures position: market order in the opposite
   direction for the full balance (`confirmMarginTrade: true`).
3. Verify flat with `invest_get_positions`; report day P&L from `invest_get_operations`.

## Safety rules
- Only touch Si*/CR* futures; other positions on the account (e.g. CNYRUBF) are not the strategy's.
- Never re-enter after a stop-out the same day. Never average or pyramid.
- If the 23:40 run fails, the stop order still limits the loss, and the next run must
  flatten any leftover position first.
- Daily loss limit: if the realized loss on the day exceeds 2% of equity, no new entries.
