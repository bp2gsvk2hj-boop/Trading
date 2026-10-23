"""One trading pass: reconcile with the broker, then act on the last closed bar.

run_once() is idempotent and safe to call at any frequency (cron, /loop, a
scheduled task). Positions and protective stops are always re-read from the
broker; the local state file only remembers which bar was already handled and
small strategy bookkeeping, so losing it is harmless.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import Client, q2f
from .broker import ApiBroker, Broker, PaperBroker, order_key
from .config import Config
from . import continuous
from .market import INTERVALS, Instrument, fetch_candles, resolve
from .risk import size
from .strategies import MSK, Action, Position, make

LIVE_ENV = "ROBOT_LIVE"


class Halted(RuntimeError):
    pass


class Engine:
    def __init__(self, cfg: Config, client: Client | None = None, dry_run: bool = False, read_only: bool = False):
        self.cfg = cfg
        self.read_only = read_only  # status queries: no orders, so no live opt-in needed
        self.c = client or Client()
        self.dry = dry_run
        self.state_path = cfg.state_dir / "state.json"
        self.journal_path = cfg.state_dir / "journal.jsonl"
        self.halt_path = cfg.state_dir / "HALT"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {"slots": {}}
        self.notes: list[str] = []
        self.broker = self._broker()
        self._insts: dict[str, Instrument] = {}

    # ---- setup -------------------------------------------------------------
    def _broker(self) -> Broker:
        cfg = self.cfg
        if cfg.mode == "paper":
            return PaperBroker(self.c, cfg.state_dir / "paper.json", cfg.risk.capital_rub,
                               cfg.risk.slippage_ticks, cfg.risk.fee_rate, cfg.risk.fee_per_contract)
        if not cfg.account_id:
            raise ValueError(f"mode={cfg.mode} needs robot.account_id in {cfg.path.name}")
        if cfg.mode == "live" and not self.read_only and os.environ.get(LIVE_ENV) != "yes":
            raise Halted(f"live mode needs {LIVE_ENV}=yes in the environment (explicit opt-in)")
        return ApiBroker(self.c, cfg.account_id, sandbox=cfg.mode == "sandbox")

    def inst(self, spec: str) -> Instrument:
        if spec not in self._insts:
            self._insts[spec] = resolve(self.c, spec)
        return self._insts[spec]

    # ---- bookkeeping -------------------------------------------------------
    def log(self, event: str, **kw):
        rec = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mode": self.cfg.mode,
               "dry": self.dry, "event": event, **kw}
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.notes.append(f"[{event}] " + ", ".join(f"{k}={v}" for k, v in kw.items()))

    def save(self):
        if self.dry:
            return
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=1, ensure_ascii=False))

    def halted(self) -> str | None:
        return self.halt_path.read_text().strip() or "halted" if self.halt_path.exists() else None

    def market_open(self, inst: Instrument) -> bool:
        s = self.c.call("MarketDataService/GetTradingStatus", {"instrumentId": inst.uid})
        return (s.get("tradingStatus") == "SECURITY_TRADING_STATUS_NORMAL_TRADING"
                and s.get("apiTradeAvailableFlag") and s.get("marketOrderAvailableFlag"))

    # ---- order helpers (respect dry-run) -----------------------------------
    def _market(self, inst, side, qty, key, why):
        if self.dry:
            self.log("would_market", ticker=inst.ticker, side=side, qty=qty, why=why)
            return 0.0
        px = self.broker.market(inst, side, qty, key)
        self.log("market", ticker=inst.ticker, side=side, qty=qty, price=round(px, 6), why=why)
        return px

    def _stop(self, inst, side, qty, price, key):
        if self.dry:
            self.log("would_stop", ticker=inst.ticker, side=side, qty=qty, price=inst.round_price(price))
            return "dry"
        sid = self.broker.post_stop(inst, side, qty, price, key)
        self.log("stop_placed", ticker=inst.ticker, side=side, qty=qty, price=inst.round_price(price))
        return sid

    def _cancel(self, inst, stop_id, why):
        if self.dry:
            self.log("would_cancel_stop", ticker=inst.ticker, why=why)
            return
        self.broker.cancel_stop(stop_id)
        self.log("stop_cancelled", ticker=inst.ticker, why=why)

    # ---- main --------------------------------------------------------------
    def run_once(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        if isinstance(self.broker, PaperBroker):
            self.broker.clock = lambda: now
        reason = self.halted()
        if reason:
            self.log("halted", reason=reason)
            return self.notes
        slots = [s for s in self.cfg.slots if s.enabled]
        insts = [self.inst(s.ticker) for s in slots]
        holdings = {i.uid: self.broker.holding(i) for i in insts}
        margin = sum(abs(h.qty) * i.go for i, h in zip(insts, holdings.values()))
        day_start = now.astimezone(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
        self.stopouts = self.broker.stopouts_today(insts, day_start)
        if self.cfg.collateral.enabled:
            # Capital = cash (incl. variation margin) + the collateral fund at market value.
            cash, units, px = self._fund_state()
            capital = cash + units * px
        else:
            capital = self.broker.free_money() + margin
        risk = replace(self.cfg.risk, capital_rub=min(self.cfg.risk.capital_rub, capital))
        for slot, inst in zip(slots, insts):
            try:
                margin += self._slot(slot, inst, now, risk, margin)
            except Exception as e:  # one bad slot must not stop the others
                self.log("error", ticker=inst.ticker, error=f"{type(e).__name__}: {e}")
        if self.cfg.collateral.enabled:
            try:
                self._rebalance(now)
            except Exception as e:
                self.log("error", ticker=self.cfg.collateral.ticker, error=f"{type(e).__name__}: {e}")
        self.save()
        return self.notes

    # ---- collateral fund (LQDT) ----------------------------------------------
    def _fund_uid(self) -> str:
        if not hasattr(self, "_fund"):
            cc = self.cfg.collateral
            found = self.c.call("InstrumentsService/FindInstrument", {"query": cc.ticker})["instruments"]
            match = [i for i in found if i.get("ticker") == cc.ticker and i.get("classCode") == cc.class_code]
            if not match:
                raise LookupError(f"{cc.ticker} on {cc.class_code} not found")
            self._fund = match[0]["uid"]
        return self._fund

    def _fund_state(self) -> tuple[float, float, float]:
        uid = self._fund_uid()
        px = q2f(self.c.call("MarketDataService/GetLastPrices", {"instrumentId": [uid]})["lastPrices"][0]["price"])
        return self.broker.cash(), self.broker.units(uid), px

    def _go_in_use(self) -> float:
        """RUB blocked as futures GO. The broker's figure is what the carry fee is charged on
        (it is lower than the exchange GO for our risk level); paper falls back to positions x GO."""
        blocked = self.broker.blocked_go()
        if blocked is not None:
            return blocked
        return sum(abs(self.broker.holding(i).qty) * i.go
                   for i in (self.inst(s.ticker) for s in self.cfg.slots if s.enabled))

    def _rebalance(self, now: datetime) -> None:
        """Keep the blocked GO plus ~cash_target of equity in cash and the rest in the fund.
        T-Bank blocks GO in rubles: GO not covered by cash is a loan from the broker with a daily
        carry fee (free only if repaid the same day), so the fund is sold right after an entry.
        Sell when cash < GO, buy when cash > GO + cash_max of equity."""
        cc = self.cfg.collateral
        uid = self._fund_uid()
        if isinstance(self.broker, PaperBroker):
            self.broker.fund_fee = cc.fee_rate
        cash, units, px = self._fund_state()
        equity = cash + units * px
        go = self._go_in_use()
        if px <= 0 or (go <= cash <= go + cc.cash_max * equity):
            return
        amount = cash - go - cc.cash_target * equity     # > 0: buy the fund, < 0: sell it
        side = 1 if amount > 0 else -1
        qty = math.floor(amount / px) if side > 0 else min(int(units), math.ceil(-amount / px))
        if qty <= 0:
            return
        s = self.c.call("MarketDataService/GetTradingStatus", {"instrumentId": uid})
        if s.get("tradingStatus") != "SECURITY_TRADING_STATUS_NORMAL_TRADING" or not s.get("apiTradeAvailableFlag"):
            self.log("warn" if cash < 0 else "collateral_wait", ticker=cc.ticker,
                     why=f"cash {cash:.0f} RUB, fund not tradeable now")
            return
        key = order_key(self.cfg.account_id or "paper", uid, "rebalance", now.strftime("%Y-%m-%dT%H:%M"))
        what = "buy" if side > 0 else "sell"
        if self.dry:
            self.log("would_collateral", ticker=cc.ticker, action=what, units=qty, amount=round(qty * px))
            return
        self.broker.trade_units(uid, side, qty, key)
        self.log("collateral", ticker=cc.ticker, action=what, units=qty, price=px, amount=round(qty * px),
                 cash_before=round(cash), go=round(go))

    def _slot(self, slot, inst: Instrument, now: datetime, risk, margin: float) -> float:
        if slot.fee_rate is not None:
            risk = replace(risk, fee_rate=slot.fee_rate)
            if isinstance(self.broker, PaperBroker):
                self.broker.fee_rates[inst.uid] = slot.fee_rate
        strat = make(slot.strategy, slot.interval, slot.params)
        bar = INTERVALS[slot.interval][2]
        key = f"{slot.ticker}:{slot.strategy}:{slot.interval}"
        sst = self.state["slots"].setdefault(key, {"strategy": {}})
        lookback = bar * (strat.warmup + 20) * 3 + timedelta(days=4)
        if slot.ticker.startswith("front:"):
            asset, prefix = (slot.ticker.split(":")[1:] + [""])[:2]
            self._roll(asset, prefix, inst, key, now)
            # back-adjusted across the last roll, like the backtest; the front keeps real prices
            bars = continuous.recent(self.c, asset, prefix, slot.interval, now - lookback, now)
        else:
            bars = fetch_candles(self.c, inst.uid, slot.interval, now - lookback, now)
        if len(bars) <= strat.warmup:
            self.log("skip", ticker=inst.ticker, why=f"only {len(bars)} bars")
            return 0.0
        for n in self.broker.sync(inst, bars):
            self.log("paper_fill", ticker=inst.ticker, note=n)
            self.stopouts += 1

        h = self.broker.holding(inst)
        stops = self.broker.stops(inst)
        side = (h.qty > 0) - (h.qty < 0)
        is_open = None  # lazily fetched trading status

        def market_open():
            nonlocal is_open
            if is_open is None:
                is_open = self.market_open(inst)
            return is_open

        # -- reconcile protective stops with the actual position
        if side == 0:
            for s in stops:
                self._cancel(inst, s.id, "orphan stop, no position")
            if sst.get("side"):
                self.log("closed_outside", ticker=inst.ticker, why="position gone (stop filled or manual close)")
            sst.update(side=0, stop=None, entry_time=None)
            stop_price = None
        else:
            for s in [s for s in stops if s.side == side]:
                self._cancel(inst, s.id, "stop on wrong side")
            prot = sorted([s for s in stops if s.side == -side], key=lambda s: -side * s.price)
            for s in prot[1:]:
                self._cancel(inst, s.id, "duplicate stop")
            stop_price = prot[0].price if prot else sst.get("stop")
            if prot and prot[0].qty != abs(h.qty):
                self._cancel(inst, prot[0].id, "stop qty mismatch")
                prot = []
            if not prot:
                if stop_price is None:
                    ind = strat.prepare(bars)
                    a = (ind.get("atr") or [None])[-1] or (bars[-1].h - bars[-1].l)
                    stop_price = h.avg_price - side * float(strat.p.get("stop_atr", 2.0)) * a
                    self.log("stop_reconstructed", ticker=inst.ticker, price=inst.round_price(stop_price))
                if market_open():
                    self._stop(inst, -side, abs(h.qty), stop_price,
                               order_key(self.cfg.account_id, inst.uid, "restore", now.isoformat()))
                else:
                    self.log("warn", ticker=inst.ticker, why="position without stop, market closed")
            if not sst.get("side"):
                sst["entry_time"] = sst.get("entry_time") or now.isoformat()
            sst.update(side=side, stop=stop_price)

        entry_time = datetime.fromisoformat(sst["entry_time"]) if sst.get("entry_time") else None
        held = sum(1 for b in bars if entry_time and b.t >= entry_time)
        pos = Position(side, abs(h.qty), h.avg_price, stop_price, entry_time, held)

        # -- one decision per closed bar
        last = bars[-1]
        closed_at = strat.close_time(last)
        if sst.get("last_bar") == last.t.isoformat():
            return 0.0
        sst["last_bar"] = last.t.isoformat()
        if now - closed_at > 2 * bar:
            self.log("stale", ticker=inst.ticker, last_bar=closed_at.isoformat())
            return 0.0

        ind = strat.prepare(bars)
        i = len(bars) - 1
        if pos.side and strat.intraday and self.cfg.sessions.must_flatten(now):
            act = Action("exit", reason="session end")
        else:
            act = strat.decide(i, ind, pos, sst["strategy"])
        tag = (self.cfg.account_id or "paper", inst.uid, last.t.isoformat())

        if act.kind == "exit" and pos.side:
            if not market_open():
                self.log("blocked", ticker=inst.ticker, action="exit", why="market closed")
                sst["last_bar"] = None  # retry on the next run
                return 0.0
            for s in self.broker.stops(inst):
                self._cancel(inst, s.id, "exit")
            self._market(inst, -pos.side, pos.qty, order_key(*tag, "exit"), act.reason)
            sst.update(side=0, stop=None, entry_time=None)
            return -pos.qty * inst.go

        if act.kind == "stop" and pos.side and act.stop is not None:
            new = inst.round_price(act.stop)
            if pos.stop is None or (new - pos.stop) * pos.side >= inst.step - 1e-9:
                if not market_open():
                    return 0.0
                for s in self.broker.stops(inst):
                    self._cancel(inst, s.id, "trail")
                self._stop(inst, -pos.side, pos.qty, new, order_key(*tag, "trail"))
                sst["stop"] = new
            return 0.0

        if act.kind == "enter" and not pos.side:
            why = None
            if not self.cfg.sessions.can_enter(now):
                why = "outside entry window"
            elif strat.intraday and self.cfg.sessions.must_flatten(now):
                why = "too late for intraday"
            elif self.stopouts >= self.cfg.risk.max_stopouts_per_day:
                why = f"{self.stopouts} stop-outs today"
            elif not market_open():
                why = "market closed"
            qty, note = size(inst, last.c, act.stop, risk, margin) if not why else (0, why)
            if not qty:
                self.log("signal_skipped", ticker=inst.ticker, side=act.side, reason=act.reason, why=note)
                return 0.0
            self._market(inst, act.side, qty, order_key(*tag, "enter"), act.reason)
            try:
                self._stop(inst, -act.side, qty, act.stop, order_key(*tag, "stop"))
            except Exception as e:
                # Never keep an unprotected position.
                self.log("error", ticker=inst.ticker, error=f"stop failed, closing: {e}")
                self._market(inst, -act.side, qty, order_key(*tag, "abort"), "stop placement failed")
                return 0.0
            sst.update(side=act.side, stop=inst.round_price(act.stop), entry_time=now.isoformat())
            self.log("would_enter" if self.dry else "entered", ticker=inst.ticker, side=act.side, qty=qty, stop=inst.round_price(act.stop),
                     reason=act.reason, sizing=note)
            return qty * inst.go
        return 0.0

    def _roll(self, asset: str, prefix: str, front: Instrument, key: str, now: datetime) -> None:
        """Move a position left in an older contract of this slot into the current front."""
        for f in continuous.contracts(self.c, asset, prefix):
            if f["uid"] == front.uid or f["expirationDate"][:10] < now.strftime("%Y-%m-%d"):
                continue
            old = self.inst(f["ticker"])
            h = self.broker.holding(old)
            if not h.qty:
                continue
            side = (h.qty > 0) - (h.qty < 0)
            if not (self.market_open(old) and self.market_open(front)):
                self.log("warn", ticker=old.ticker, why=f"roll to {front.ticker} pending, market closed")
                continue
            px = self.c.call("MarketDataService/GetLastPrices", {"instrumentId": [old.uid, front.uid]})["lastPrices"]
            last = {p["instrumentUid"]: q2f(p["price"]) for p in px}
            stops = self.broker.stops(old)
            sst = self.state["slots"].setdefault(key, {"strategy": {}})
            old_stop = stops[0].price if stops else sst.get("stop")
            for s in stops:
                self._cancel(old, s.id, f"roll to {front.ticker}")
            tag = (self.cfg.account_id or "paper", old.uid, front.uid, "roll", now.strftime("%Y-%m-%d"))
            self._market(old, -side, abs(h.qty), order_key(*tag, "close"), f"roll to {front.ticker}")
            self._market(front, side, abs(h.qty), order_key(*tag, "open"), f"roll from {old.ticker}")
            if old_stop is not None:
                new_stop = front.round_price(old_stop + last[front.uid] - last[old.uid])
                try:
                    self._stop(front, -side, abs(h.qty), new_stop, order_key(*tag, "stop"))
                except Exception as e:
                    self.log("error", ticker=front.ticker, error=f"stop after roll failed, closing: {e}")
                    self._market(front, -side, abs(h.qty), order_key(*tag, "abort"), "stop placement failed")
                    continue
                sst["stop"] = new_stop
            self.log("rolled", frm=old.ticker, to=front.ticker, side=side, qty=abs(h.qty))

    # ---- manual controls ---------------------------------------------------
    def flatten(self, why: str = "manual flatten") -> list[str]:
        for slot in self.cfg.slots:
            inst = self.inst(slot.ticker)
            for s in self.broker.stops(inst):
                self._cancel(inst, s.id, why)
            h = self.broker.holding(inst)
            if h.qty:
                side = (h.qty > 0) - (h.qty < 0)
                self._market(inst, -side, abs(h.qty),
                             order_key(self.cfg.account_id or "paper", inst.uid, "flatten",
                                       datetime.now(timezone.utc).isoformat()), why)
            self.state["slots"].get(f"{slot.ticker}:{slot.strategy}:{slot.interval}", {}).update(
                side=0, stop=None, entry_time=None)
        self.save()
        return self.notes

    def status(self) -> dict:
        out = {"mode": self.cfg.mode, "account": self.cfg.account_id or "paper", "halted": self.halted(),
               "free_money": round(self.broker.free_money(), 2), "slots": []}
        if self.cfg.collateral.enabled:
            cash, units, px = self._fund_state()
            out["cash"] = round(cash, 2)
            out["go"] = round(self._go_in_use(), 2)
            out["collateral"] = {"ticker": self.cfg.collateral.ticker, "units": units, "price": px,
                                 "value": round(units * px, 2), "haircut": self.cfg.collateral.haircut}
        for slot in self.cfg.slots:
            inst = self.inst(slot.ticker)
            h = self.broker.holding(inst)
            last = self.c.call("MarketDataService/GetLastPrices", {"instrumentId": [inst.uid]})["lastPrices"][0]
            lp = q2f(last.get("price"))
            out["slots"].append({
                "ticker": inst.ticker, "strategy": slot.strategy, "interval": slot.interval,
                "enabled": slot.enabled, "qty": h.qty, "avg": h.avg_price, "last": lp,
                "unrealized_rub": round((lp - h.avg_price) * h.qty * inst.point_value, 2) if h.qty else 0.0,
                "stops": [(s.side, s.qty, s.price) for s in self.broker.stops(inst)],
                "go_per_contract": round(inst.go),
            })
        return out
