"""robot.toml loading."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .api import ROOT
from .risk import RiskConfig, Sessions

MODES = ("paper", "sandbox", "live")


@dataclass
class Slot:
    ticker: str                 # "CNYRUBF" or "front:GAZP"
    strategy: str
    interval: str
    params: dict = field(default_factory=dict)
    enabled: bool = True
    fee_rate: float | None = None   # instrument-specific exchange fee, overrides [risk].fee_rate


@dataclass
class CollateralCfg:
    """Idle cash kept in a money-market fund that the broker accepts as futures collateral."""
    enabled: bool = False
    ticker: str = "LQDT"
    class_code: str = "SPBRU"      # the API-tradeable board for LQDT at T-Invest
    cash_target: float = 0.02      # after a rebalance, keep GO + this share of equity in cash
    cash_max: float = 0.04         # buy the fund when cash exceeds GO + this share (sell when cash < GO)
    haircut: float = 0.40          # broker risk rate: the fund counts as (1 - haircut) of its value
    fee_rate: float = 0.0005       # fund trade fee, paper/backtest only


@dataclass
class Config:
    mode: str
    account_id: str
    state_dir: Path
    risk: RiskConfig
    sessions: Sessions
    slots: list[Slot]
    path: Path
    name: str = "Торговый робот"
    started: str = ""           # ISO time the robot started on the account: returns are counted from it
    collateral: CollateralCfg = field(default_factory=CollateralCfg)


def _pick(cls, d: dict):
    names = {f.name for f in fields(cls)}
    unknown = set(d) - names
    if unknown:
        raise ValueError(f"{cls.__name__}: unknown keys {sorted(unknown)}")
    return cls(**d)


def load(path: str | Path = ROOT / "robot.toml") -> Config:
    path = Path(path)
    raw = tomllib.loads(path.read_text())
    r = raw.get("robot", {})
    mode = r.get("mode", "paper")
    if mode not in MODES:
        raise ValueError(f"robot.mode must be one of {MODES}, got {mode!r}")
    state_dir = Path(r.get("state_dir", "state"))
    if not state_dir.is_absolute():
        state_dir = path.parent / state_dir
    return Config(
        mode=mode,
        account_id=str(r.get("account_id", "")),
        state_dir=state_dir,
        risk=_pick(RiskConfig, raw.get("risk", {})),
        sessions=_pick(Sessions, raw.get("sessions", {})),
        slots=[_pick(Slot, s) for s in raw.get("slots", [])],
        path=path,
        name=str(r.get("name", "Торговый робот")),
        started=str(r.get("started", "")),
        collateral=_pick(CollateralCfg, raw.get("collateral", {})),
    )
