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

---
## v2: alternative strategies, latency-aware (Oct 2026)
The execution model now simulates a slow client on 1-min bars (`src/engine.py`):
- market orders fill at the open 2 min after the decision, with 1 tick slippage;
- broker-side stops fill with 2 ticks slippage;
- everything is closed at 23:40.

Families scanned on 15 futures (`src/scan2.py`, `src/scan3.py`):
- 10:00 opening drive;
- US cash-open drive;
- EIA crude/gas releases and NFP;
- volatility breakout;
- range fade with limit orders.

**Selected portfolio (`src/portfolio2.py`):**
- **VB** (volatility breakout): stop-entry at O ± 0.8·(avg 20d range), entries 10:00–18:00, stop at O, exit 23:40.
  Instruments: Si, CR, Eu, RI, MX, GZ.
- **ODM** (as above): Si, CR.

Capital 3 M ₽, risk 0.3% per trade:

| | CAGR | Sharpe | MaxDD | Trades | PF |
|---|---|---|---|---|---|
| Total 2024-02…2026-10 | 20.4% | 1.87 | −5.4% | 1551 | 1.53 |
| IS (to 2025-06) | 19.9% | 1.69 | −4.7% | 776 | 1.50 |
| OOS (2025-07…) | 20.9% | 2.12 | −5.4% | 775 | 1.56 |

- The result is the same with a 5-min delay (Sharpe 1.88).
- At 0.5% risk: CAGR 36%, MaxDD −9%.
- Rejected: range fade (negative everywhere), US-open / EIA / NFP drives (no stable edge), commodities and US index futures.
- Most of the edge comes from holding into the evening session; exiting at 18:40 removes it.
