"""Telegram trading signals from the signal-only robot (robot-signals.toml, paper mode).

python3 -m robot.signals --config robot-signals.toml --run run.log --status status.json [--summary]

Turns the virtual account's actions in one pass into instructions a person can follow:
open a position (size, stop), move the stop, close the position, the stop fired.
Entry prices and stops of open virtual positions are kept in <state_dir>/signals.json,
so exits can report the result. No orders are ever sent from here.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .telegram import fields, parse_run, send

ALERT = {"error", "warn", "halted"}
WHY_RU = {
    "trail stop hit at close": "цена закрылась за трейлинг-стопом",
    "reached middle band": "цена вернулась к средней",
    "time stop": "истекло время удержания",
    "target reached": "достигнута цель",
    "session end": "конец торговой сессии",
    "manual flatten": "ручное закрытие",
}


def num(x: float) -> str:
    """1 120, 12.449, 11 157.5: thousands grouped by spaces, no trailing zeros."""
    return f"{x:,.6f}".rstrip("0").rstrip(".").replace(",", " ")


def rub(x: float) -> str:
    return f"{x:+,.0f} ₽".replace(",", " ")


def side_ru(side: int) -> str:
    return "ЛОНГ" if side > 0 else "ШОРТ"


def reason_ru(why: str) -> str:
    m = re.match(r"breakout > (.+)", why)
    if m:
        return f"пробой максимума 20 часов ({m.group(1)}) по направлению тренда"
    m = re.match(r"breakdown < (.+)", why)
    if m:
        return f"пробой минимума 20 часов ({m.group(1)}) по направлению тренда"
    return WHY_RU.get(why, why)


def build_messages(events: list[tuple[str, str]], book: dict, point_value, label: str) -> list[str]:
    """events: parsed run log. book: {ticker: {side, qty, entry, stop}} of open virtual positions,
    updated in place. point_value(ticker) -> RUB per 1.0 price point per contract."""
    head = f"📣 СИГНАЛ · {label}"
    out: list[str] = []
    last_market: dict[str, dict] = {}
    trailing: set[str] = set()
    for i, (e, detail) in enumerate(events):
        f = fields(detail)
        tk = f.get("ticker", "")
        if e == "market":
            last_market[tk] = f
            entering = any(e2 == "entered" and fields(d2).get("ticker") == tk for e2, d2 in events[i + 1:])
            pos = book.get(tk)
            if not entering and pos:
                px = float(f["price"])
                pnl = (px - pos["entry"]) * pos["side"] * pos["qty"] * point_value(tk)
                out.append(f"{head}\n🔴 Закрыть {side_ru(pos['side'])} {tk}: {num(pos['qty'])} контр. по рынку (≈{num(px)})\n"
                           f"Результат ≈ {rub(pnl)} (вход {num(pos['entry'])}, без комиссии)\n"
                           f"Снять стоп-заявку {num(pos['stop'])}\n"
                           f"Причина: {reason_ru(f.get('why', ''))}")
                book.pop(tk, None)
        elif e == "entered":
            side, qty, stop = int(f["side"]), int(f["qty"]), float(f["stop"])
            px = float(last_market.get(tk, {}).get("price", 0) or 0)
            m = re.match(r"risk (\d+) RUB, GO (\d+) RUB", f.get("sizing", ""))
            risk = f"Риск до стопа ≈ {num(int(m.group(1)))} ₽, ГО ≈ {num(int(m.group(2)))} ₽\n" if m else ""
            out.append(f"{head}\n🟢 Открыть {side_ru(side)} {tk}: {num(qty)} контр. по рынку"
                       + (f" (≈{num(px)})" if px else "") + "\n"
                       f"🛡 Стоп-лосс: {'продажа' if side > 0 else 'покупка'} {num(qty)} по {num(stop)}\n"
                       f"{risk}Причина: {reason_ru(f.get('reason', ''))}")
            book[tk] = {"side": side, "qty": qty, "entry": px, "stop": stop}
        elif e == "stop_cancelled" and f.get("why") == "trail":
            trailing.add(tk)
        elif e == "stop_placed" and tk in trailing:
            trailing.discard(tk)
            pos = book.get(tk)
            new = float(f["price"])
            was = f" (было {num(pos['stop'])})" if pos else ""
            out.append(f"{head}\n🛡 Передвинуть стоп {tk}: {'покупка' if f.get('side') == '1' else 'продажа'} "
                       f"{num(int(f['qty']))} по {num(new)}{was}")
            if pos:
                pos["stop"] = new
        elif e == "paper_fill":
            m = re.search(r"@ ([-\d.]+)", f.get("note", ""))
            pos = book.pop(tk, None)
            if pos and m:
                px = float(m.group(1))
                pnl = (px - pos["entry"]) * pos["side"] * pos["qty"] * point_value(tk)
                out.append(f"{head}\n⛔ Сработал стоп {tk} ≈{num(px)}: {side_ru(pos['side'])} {num(pos['qty'])} закрыт\n"
                           f"Результат ≈ {rub(pnl)} (вход {num(pos['entry'])}, без комиссии)\n"
                           f"Если стоп-заявка стояла на бирже, она уже исполнилась.")
        elif e in ALERT:
            out.append(f"⚠️ Сигнальный робот: {e}: {detail}")
    return out


def summary(status: dict, capital: float, label: str) -> str:
    lines = [f"📊 {label}: итог дня"]
    unreal = 0.0
    for s in status.get("slots", []):
        if not s.get("enabled", True) or not s.get("qty"):
            continue
        unreal += s.get("unrealized_rub", 0.0)
        stops = ", ".join(num(p) for _, _, p in s.get("stops", [])) or "нет"
        lines.append(f"{s['ticker']}: {side_ru(s['qty'])} {num(abs(s['qty']))} @ {num(s['avg'])}, "
                     f"сейчас {num(s['last'])}, {rub(s['unrealized_rub'])}, стоп {stops}")
    if len(lines) == 1:
        lines.append("Открытых позиций нет")
    equity = status.get("free_money", capital) + unreal
    lines.append(f"Виртуальный счёт: {equity:,.0f} ₽ ({rub(equity - capital)} с начала)".replace(",", " "))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from .config import load
    from .engine import Engine

    p = argparse.ArgumentParser(prog="robot.signals")
    p.add_argument("--config", default="robot-signals.toml")
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--status", type=Path, required=True)
    p.add_argument("--summary", action="store_true")
    a = p.parse_args(argv)
    cfg = load(a.config)
    if cfg.mode != "paper":
        print("robot.signals is only for paper-mode configs", file=sys.stderr)
        return 2
    label = f"счёт {cfg.risk.capital_rub / 1e6:g} млн ₽"
    book_path = cfg.state_dir / "signals.json"
    book = json.loads(book_path.read_text()) if book_path.exists() else {}
    events = parse_run(a.run.read_text() if a.run.exists() else "")
    eng = None

    def point_value(ticker: str) -> float:
        nonlocal eng
        eng = eng or Engine(cfg, read_only=True)
        return eng.inst(ticker).point_value

    messages = build_messages(events, book, point_value, label)
    book_path.parent.mkdir(parents=True, exist_ok=True)
    book_path.write_text(json.dumps(book, ensure_ascii=False))
    if a.summary:
        try:
            messages.append(summary(json.loads(a.status.read_text()), cfg.risk.capital_rub, label))
        except (OSError, ValueError) as e:
            messages.append(f"⚠️ Сигнальный робот: не удалось прочитать статус: {e}")
    if not messages:
        print("no signals in this pass")
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
