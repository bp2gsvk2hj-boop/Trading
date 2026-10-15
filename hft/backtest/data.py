"""Загрузка исторических данных из CSV.

Формат стакана (`book.csv`), N уровней:
    ts,bid_px_1,bid_qty_1,...,bid_px_N,bid_qty_N,ask_px_1,ask_qty_1,...,ask_px_N,ask_qty_N
Формат сделок (`trades.csv`):
    ts,price,qty,side          # side: B — агрессор покупатель, S — продавец

`ts` — микросекунды от эпохи (UTC) или ISO-время (`2024-03-01T10:00:00.123456+03:00`).
Подходят выгрузки MOEX AlgoPack/DataShop и записи стакана через API брокера
после приведения к этому формату.
"""
from __future__ import annotations

import csv
import heapq
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from ..types import OrderBook, Side, Trade


def parse_ts(s: str) -> int:
    s = s.strip()
    if s.lstrip("-").isdigit():
        return int(s)
    return int(datetime.fromisoformat(s).timestamp() * 1_000_000)


def read_books(path: str | Path) -> Iterator[OrderBook]:
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        n = max(int(c.rsplit("_", 1)[1]) for c in cols if c.startswith("bid_px_"))
        for row in reader:
            bids, asks = [], []
            for i in range(1, n + 1):
                bp, bq = row.get(f"bid_px_{i}"), row.get(f"bid_qty_{i}")
                ap, aq = row.get(f"ask_px_{i}"), row.get(f"ask_qty_{i}")
                if bp and bq and int(float(bq)) > 0:
                    bids.append((float(bp), int(float(bq))))
                if ap and aq and int(float(aq)) > 0:
                    asks.append((float(ap), int(float(aq))))
            yield OrderBook(parse_ts(row["ts"]), bids, asks)


def read_trades(path: str | Path) -> Iterator[Trade]:
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            side = Side.BUY if row["side"].strip().upper() in ("B", "BUY", "1") else Side.SELL
            yield Trade(parse_ts(row["ts"]), float(row["price"]), int(float(row["qty"])), side)


def merge_events(books, trades) -> Iterator[OrderBook | Trade]:
    """Сливает два отсортированных по времени потока. При равном времени сделки идут раньше стакана."""
    keyed_b = ((b.ts, 1, i, b) for i, b in enumerate(books))
    keyed_t = ((t.ts, 0, i, t) for i, t in enumerate(trades))
    for *_, ev in heapq.merge(keyed_b, keyed_t, key=lambda x: (x[0], x[1], x[2])):
        yield ev


def write_books(path: str | Path, books, levels: int = 5) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        header = ["ts"]
        header += [x for i in range(1, levels + 1) for x in (f"bid_px_{i}", f"bid_qty_{i}")]
        header += [x for i in range(1, levels + 1) for x in (f"ask_px_{i}", f"ask_qty_{i}")]
        w.writerow(header)
        for b in books:
            row = [b.ts]
            for lv in (b.bids, b.asks):
                for i in range(levels):
                    row += list(lv[i]) if i < len(lv) else ["", ""]
            w.writerow(row)


def write_trades(path: str | Path, trades) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "price", "qty", "side"])
        for t in trades:
            w.writerow([t.ts, t.price, t.qty, "B" if t.aggressor is Side.BUY else "S"])
