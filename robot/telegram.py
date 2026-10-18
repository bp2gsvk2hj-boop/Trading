"""Telegram notifications for scheduled runs (GitHub Actions).

python3 -m robot.telegram --run run.log --status status.json --exit-code 0 [--summary]

Sends a message when the pass did something (orders, stops, exits), on any problem, or always
with --summary (end-of-day report). Quiet passes send nothing. Needs TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID; without them it prints the message and exits.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ALERT = {"error", "warn", "halted"}
ACTIVITY = {"market", "entered", "stop_placed", "stop_cancelled", "stop_reconstructed",
            "closed_outside", "signal_skipped"}
EVENT_RU = {
    "market": "Рыночная заявка",
    "entered": "Вход",
    "stop_placed": "Стоп выставлен",
    "stop_cancelled": "Стоп снят",
    "stop_reconstructed": "Стоп восстановлен",
    "closed_outside": "Позиция закрыта (стоп или вручную)",
    "signal_skipped": "Сигнал пропущен",
    "error": "Ошибка",
    "warn": "Предупреждение",
    "halted": "Робот остановлен",
}
LINE = re.compile(r"^\[(\w+)\]\s*(.*)$")


def parse_run(text: str) -> list[tuple[str, str]]:
    out = []
    for raw in text.splitlines():
        m = LINE.match(raw.strip())
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def side_ru(qty: float) -> str:
    return "лонг" if qty > 0 else "шорт"


def format_status(status: dict) -> list[str]:
    lines = []
    for s in status.get("slots", []):
        if not s.get("qty"):
            continue
        stops = ", ".join(f"{p:g}" for _, _, p in s.get("stops", [])) or "НЕТ СТОПА"
        lines.append(f"{s['ticker']}: {side_ru(s['qty'])} {abs(s['qty'])} @ {s['avg']:g}, "
                     f"сейчас {s['last']:g}, P&L {s['unrealized_rub']:+.0f} ₽, стоп {stops}")
    lines = lines or ["Открытых позиций нет"]
    if "free_money" in status:
        lines.append(f"Свободно: {status['free_money']:,.2f} ₽".replace(",", " "))
    return lines


def unprotected(status: dict) -> bool:
    return any(s.get("qty") and not s.get("stops") for s in status.get("slots", []))


def build(run_text: str, status: dict | None, exit_code: int, summary: bool, url: str = "") -> str | None:
    events = parse_run(run_text)
    kinds = {e for e, _ in events}
    alert = bool(kinds & ALERT) or exit_code != 0 or status is None or unprotected(status)
    if not (alert or kinds & ACTIVITY or summary):
        return None
    title = "⚠️ ТРЕБУЕТСЯ ВНИМАНИЕ" if alert else ("📊 Итог дня" if summary and not kinds & ACTIVITY
                                                  else "🤖 Торговый робот")
    lines = [title]
    for e, detail in events:
        if e in ALERT or e in ACTIVITY:
            lines.append(f"• {EVENT_RU.get(e, e)}: {detail}")
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
    p.add_argument("--url", default="")
    a = p.parse_args(argv)
    run_text = a.run.read_text() if a.run.exists() else ""
    try:
        status = json.loads(a.status.read_text())
    except (OSError, ValueError):
        status = None
    text = build(run_text, status, a.exit_code, a.summary, a.url)
    if text is None:
        print("quiet pass, nothing to send")
        return 0
    print(text)
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, not sent", file=sys.stderr)
        return 0
    send(token, chat, text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
