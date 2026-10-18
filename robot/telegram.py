"""Telegram notifications for scheduled runs (GitHub Actions).

python3 -m robot.telegram --run run.log --status status.json --exit-code 0 [--summary] [--trades]

--trades: one message per executed trade on the robot's instruments since the previous run
(opened / added / closed with P&L, marked when a stop fired), taken from the broker, plus one per
stop order the robot placed, moved or cancelled in this run. The cursor and the last known
positions live in state/notify.json.
Then one summary message on any problem, or always with --summary (end-of-day report).
Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID; without them messages are only printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ALERT = {"error", "warn", "halted"}
EVENT_RU = {
    "closed_outside": "Позиция закрыта вне робота (стоп или вручную)",
    "signal_skipped": "Сигнал пропущен",
    "error": "Ошибка",
    "warn": "Предупреждение",
    "halted": "Робот остановлен",
}
SUMMARY_EVENTS = ALERT | {"signal_skipped"}
LINE = re.compile(r"^\[(\w+)\]\s*(.*)$")
FIELD = re.compile(r"(\w+)=(.*?)(?=, \w+=|$)")


def parse_run(text: str) -> list[tuple[str, str]]:
    out = []
    for raw in text.splitlines():
        m = LINE.match(raw.strip())
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def fields(detail: str) -> dict[str, str]:
    return {k: v for k, v in FIELD.findall(detail)}


def side_ru(qty: float) -> str:
    return "лонг" if qty > 0 else "шорт"


def rub(x: float) -> str:
    return f"{x:+,.0f} ₽".replace(",", " ")


# ---- per-trade and per-order messages ---------------------------------------
def trade_messages(trades: list[dict], pos: dict[str, dict], reasons: dict[str, str]) -> list[str]:
    """trades: [{ticker, t, side (+1 buy/-1 sell), qty, price, point_value, by_stop}] sorted by time.
    pos: {ticker: {qty, avg}} before the first trade; updated in place."""
    out = []
    for tr in trades:
        tk, px, pv = tr["ticker"], tr["price"], tr["point_value"]
        p = pos.setdefault(tk, {"qty": 0, "avg": 0.0})
        q, avg, signed = p["qty"], p["avg"], tr["side"] * tr["qty"]
        when = tr["t"].astimezone(timezone(timedelta(hours=3))).strftime("%H:%M")
        how = " по стопу" if tr["by_stop"] else ""
        why = f"\nПричина: {reasons[tk]}" if tk in reasons and not tr["by_stop"] else ""
        if q and (q > 0) != (signed > 0):
            closed = min(abs(signed), abs(q))
            pnl = (px - avg) * (1 if q > 0 else -1) * closed * pv
            left = q + (closed if q < 0 else -closed)
            head = "🔴 Закрыта позиция" if left == 0 else "🟠 Частично закрыта позиция"
            out.append(f"{head}{how} {tk}: {side_ru(q)} {closed} по {px:g} ({when} МСК)\n"
                       f"Вход {avg:g}, результат {rub(pnl)} (без комиссии)"
                       + (f"\nОсталось: {side_ru(left)} {abs(left)}" if left else "") + why)
            q, signed = left, signed + (closed if signed < 0 else -closed)
            if q == 0:
                avg = 0.0
        if signed:
            head = "🟢 Открыта позиция" if q == 0 else "➕ Увеличена позиция"
            new_q = q + signed
            avg = (avg * abs(q) + px * abs(signed)) / abs(new_q)
            out.append(f"{head}{how} {tk}: {side_ru(signed)} {abs(signed)} по {px:g} ({when} МСК)"
                       + (f"\nВсего: {side_ru(new_q)} {abs(new_q)}, средняя {avg:g}" if q else "") + why)
            q = new_q
        p.update(qty=q, avg=avg)
    return out


def order_messages(events: list[tuple[str, str]]) -> list[str]:
    """Stop orders the robot placed / moved / cancelled in this run."""
    out, pending_trail = [], set()
    for e, detail in events:
        f = fields(detail)
        tk = f.get("ticker", "?")
        if e == "stop_cancelled":
            if f.get("why") == "trail":
                pending_trail.add(tk)
            elif f.get("why") != "exit":  # an exit is reported as the closing trade
                out.append(f"✖️ Стоп-заявка снята {tk}: {f.get('why', '')}")
        elif e == "stop_placed":
            side = "покупка" if f.get("side") == "1" else "продажа"
            if tk in pending_trail:
                pending_trail.discard(tk)
                out.append(f"🛡 Стоп передвинут {tk}: {side} {f.get('qty')} по {f.get('price')}")
            else:
                out.append(f"🛡 Стоп-заявка выставлена {tk}: {side} {f.get('qty')} по {f.get('price')}")
    out += [f"✖️ Стоп-заявка снята {tk}: перенос стопа не завершён" for tk in sorted(pending_trail)]
    return out


def run_reasons(events: list[tuple[str, str]]) -> dict[str, str]:
    return {f["ticker"]: f["why"] for e, d in events if e == "market"
            for f in [fields(d)] if "ticker" in f and "why" in f}


# ---- summary message ---------------------------------------------------------
def format_status(status: dict) -> list[str]:
    lines = []
    for s in status.get("slots", []):
        if not s.get("qty"):
            continue
        stops = ", ".join(f"{p:g}" for _, _, p in s.get("stops", [])) or "НЕТ СТОПА"
        lines.append(f"{s['ticker']}: {side_ru(s['qty'])} {abs(s['qty'])} @ {s['avg']:g}, "
                     f"сейчас {s['last']:g}, P&L {rub(s['unrealized_rub'])}, стоп {stops}")
    lines = lines or ["Открытых позиций нет"]
    if "free_money" in status:
        lines.append(f"Свободно: {status['free_money']:,.2f} ₽".replace(",", " "))
    return lines


def unprotected(status: dict) -> bool:
    return any(s.get("qty") and not s.get("stops") for s in status.get("slots", []))


def build(run_text: str, status: dict | None, exit_code: int, summary: bool, url: str = "",
          problems: list[str] = ()) -> str | None:
    events = parse_run(run_text)
    kinds = {e for e, _ in events}
    alert = bool(kinds & ALERT) or exit_code != 0 or status is None or unprotected(status) or bool(problems)
    if not (alert or summary):
        return None
    lines = ["⚠️ ТРЕБУЕТСЯ ВНИМАНИЕ" if alert else "📊 Итог дня"]
    for e, detail in events:
        if e in SUMMARY_EVENTS:
            lines.append(f"• {EVENT_RU.get(e, e)}: {detail}")
    lines += [f"• {p}" for p in problems]
    if exit_code != 0:
        lines.append(f"• Проход завершился с кодом {exit_code}")
        tail = [l for l in run_text.strip().splitlines() if l.strip()][-5:]
        lines += [f"  {l[:300]}" for l in tail]
    if status is None:
        lines.append("• Статус получить не удалось")
    else:
        if unprotected(status):
            lines.append("• Есть позиция без стопа!")
        lines.append("")
        lines += format_status(status)
    if url:
        lines.append(url)
    return "\n".join(lines)[:4000]


# ---- broker access -----------------------------------------------------------
def fetch_trades(since: datetime, now: datetime) -> list[dict]:
    from .api import q2f
    from .config import load
    from .engine import Engine

    cfg = load()
    eng = Engine(cfg, read_only=True)
    insts = {i.uid: i for i in (eng.inst(s.ticker) for s in cfg.slots)}
    fmt = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ops = eng.c.call("OperationsService/GetOperations", {
        "accountId": cfg.account_id, "from": fmt(since), "to": fmt(now),
        "state": "OPERATION_STATE_EXECUTED"}).get("operations", [])
    stops = eng.c.call("StopOrdersService/GetStopOrders", {
        "accountId": cfg.account_id, "status": "STOP_ORDER_STATUS_EXECUTED",
        "from": fmt(since - timedelta(days=30)), "to": fmt(now)}).get("stopOrders", [])
    fired = [(s["instrumentUid"], 1 if s.get("direction") == "STOP_ORDER_DIRECTION_BUY" else -1,
              datetime.fromisoformat(s["activationDateTime"].replace("Z", "+00:00")))
             for s in stops if s.get("activationDateTime") and s.get("instrumentUid") in insts]
    trades = []
    for o in ops:
        kind = {"OPERATION_TYPE_BUY": 1, "OPERATION_TYPE_SELL": -1}.get(o.get("operationType"))
        inst = insts.get(o.get("instrumentUid"))
        if not kind or not inst:
            continue
        t = datetime.fromisoformat(o["date"].replace("Z", "+00:00"))
        trades.append({"ticker": inst.ticker, "t": t, "side": kind, "qty": int(o["quantity"]),
                       "price": q2f(o["price"]), "point_value": inst.point_value,
                       "by_stop": any(u == inst.uid and d == kind and abs((t - at).total_seconds()) < 120
                                      for u, d, at in fired)})
    return sorted(trades, key=lambda x: x["t"])


def send(token: str, chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="robot.telegram")
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--status", type=Path, required=True)
    p.add_argument("--exit-code", type=int, default=0)
    p.add_argument("--summary", action="store_true")
    p.add_argument("--trades", action="store_true")
    p.add_argument("--cursor", type=Path, default=Path("state/notify.json"))
    p.add_argument("--url", default="")
    a = p.parse_args(argv)
    run_text = a.run.read_text() if a.run.exists() else ""
    try:
        status = json.loads(a.status.read_text())
    except (OSError, ValueError):
        status = None
    events = parse_run(run_text)

    messages, problems = [], []
    if a.trades:
        now = datetime.now(timezone.utc)
        try:
            cur = json.loads(a.cursor.read_text())
        except (OSError, ValueError):
            cur = {}
        since = datetime.fromisoformat(cur["since"]) if "since" in cur else now - timedelta(minutes=70)
        try:
            trades = fetch_trades(since, now)
            pos = cur.get("pos")
            if pos is None and status is not None:
                # No cursor yet: rewind the current positions by the trades in the window.
                pos = {s["ticker"]: {"qty": s.get("qty", 0), "avg": s.get("avg", 0.0)} for s in status["slots"]}
                for tr in trades:
                    pos.setdefault(tr["ticker"], {"qty": 0, "avg": 0.0})["qty"] -= tr["side"] * tr["qty"]
            messages += trade_messages(trades, pos or {}, run_reasons(events))
            if status is not None:
                pos = {s["ticker"]: {"qty": s.get("qty", 0), "avg": s.get("avg", 0.0)} for s in status["slots"]}
            a.cursor.parent.mkdir(parents=True, exist_ok=True)
            a.cursor.write_text(json.dumps({"since": now.isoformat(), "pos": pos}, ensure_ascii=False))
        except Exception as e:  # never lose the summary because of the trade feed
            problems.append(f"Не удалось получить сделки: {type(e).__name__}: {e}")
        messages += order_messages(events)

    summary = build(run_text, status, a.exit_code, a.summary, a.url, problems)
    if summary:
        messages.append(summary)
    if not messages:
        print("quiet pass, nothing to send")
        return 0
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    for m in messages:
        print(m, end="\n\n")
        if token and chat:
            send(token, chat, m)
    if not (token and chat):
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, not sent", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
