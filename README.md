# Intraday FORTS strategy: Opening Drive Momentum

Fully automated intraday strategy on Moscow Exchange futures; orders go through the T-Invest MCP server.

## Strategy
- **Instruments:** front quarterly `Si` (USD/RUB) and `CR` (CNY/RUB). These have no funding,
  unlike the perpetuals USDRUBF/CNYRUBF.
- **Signal:** O is the 10:00 open. sigma_t is the 14-day average |close_t / O − 1| at the same time of day.
  At 11:00…15:00 (hourly), the first time the close is outside `O·(1 ± 2·sigma_t)`, enter in the
  breakout direction. At most one trade per instrument per day.
- **Risk:** stop-loss at O (exchange stop order). Exit at 23:40, flat overnight. Size is 0.5% of equity risked to the stop.

## Backtest (5-min bars, 2024-01 … 2026-10)
Costs: 0.04% per side (actual T-Invest "Trader" tariff fee) + 1 tick slippage. Fills at next bar open.
In-sample is 2024-01…2025-06, out-of-sample is 2025-07…2026-10.

| | CAGR | Sharpe | MaxDD | Trades | Win% | PF |
|---|---|---|---|---|---|---|
| Total | 14.4% | 1.95 | −2.7% | 362 | 58% | 1.94 |
| IS | 17.1% | 2.04 | −2.5% | 192 | 56% | 2.04 |
| OOS | 11.4% | 1.86 | −2.7% | 170 | 60% | 1.82 |

At 1% risk per trade: CAGR ≈ 30%, MaxDD ≈ −5.2%. Every year is positive (2024, 2025, 2026), the
neighbouring parameter values work too, and the edge survives 0.05% commission or 3-tick slippage.
Rejected after testing (costs eat the edge): ORB, the noise-area breakout with VWAP trailing, VWAP
mean reversion, Donchian channels, and linear daily models (overfit).

## Layout
- `src/download.py` downloads 1-min history from the T-Invest `history-data` archive.
- `src/strategies.py` holds the strategy simulators. `src/research.py` runs the grid. `src/robustness.py` runs the sensitivity checks.
- `src/portfolio.py` runs the RUB portfolio backtest. `src/plot.py` draws the equity chart (`results/equity.png`).
- `src/live.py` and `live_config.json` produce the stateless live signal. `LIVE_RUNBOOK.md` is the execution procedure.
