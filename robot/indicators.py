"""Indicators over plain float lists. Values before warm-up are None."""
from __future__ import annotations

import math


def sma(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    k = 2 / (n + 1)
    e = None
    for i, x in enumerate(xs):
        if i < n - 1:
            continue
        e = sum(xs[i - n + 1:i + 1]) / n if e is None else x * k + e * (1 - k)
        out[i] = e
    return out


def stdev(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1:i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((v - m) ** 2 for v in w) / n)
    return out


def true_range(h: list[float], l: list[float], c: list[float]) -> list[float]:
    tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return tr


def _wilder(xs: list[float], n: int, start: int = 0) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    if len(xs) - start < n:
        return out
    v = sum(xs[start:start + n]) / n
    out[start + n - 1] = v
    for i in range(start + n, len(xs)):
        v = (v * (n - 1) + xs[i]) / n
        out[i] = v
    return out


def atr(h: list[float], l: list[float], c: list[float], n: int = 14) -> list[float | None]:
    return _wilder(true_range(h, l, c), n)


def adx(h: list[float], l: list[float], c: list[float], n: int = 14) -> list[float | None]:
    size = len(c)
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, size):
        up, down = h[i] - h[i - 1], l[i - 1] - l[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    tr = _wilder(true_range(h, l, c), n, 1)
    pdm, mdm = _wilder(plus_dm, n, 1), _wilder(minus_dm, n, 1)
    dx: list[float] = []
    first = None
    for i in range(size):
        if tr[i] is None or not tr[i]:
            continue
        pdi, mdi = 100 * pdm[i] / tr[i], 100 * mdm[i] / tr[i]
        if first is None:
            first = i
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
    out: list[float | None] = [None] * size
    if first is None:
        return out
    smoothed = _wilder(dx, n)
    for j, v in enumerate(smoothed):
        out[first + j] = v
    return out


def highest(xs: list[float], n: int) -> list[float | None]:
    """Max of the n values *before* index i (excludes i itself)."""
    return [max(xs[i - n:i]) if i >= n else None for i in range(len(xs))]


def lowest(xs: list[float], n: int) -> list[float | None]:
    return [min(xs[i - n:i]) if i >= n else None for i in range(len(xs))]
