"""robot.toml loading."""
from __future__ import annotations

import os
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
class TelegramCfg:
    """Names of the environment variables (GitHub secrets) with this robot's Telegram target.
    Empty ones fall back to the shared TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID."""
    token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_env: str = "TELEGRAM_CHAT_ID"
    thread_env: str = ""        # forum topic id in a group with topics

    def resolve(self) -> tuple[str | None, str | None, str | None]:
        env = os.environ
        token = env.get(self.token_env) or env.get("TELEGRAM_BOT_TOKEN")
        chat = env.get(self.chat_env) or env.get("TELEGRAM_CHAT_ID")
        thread = env.get(self.thread_env) if self.thread_env else None
        return token or None, chat or None, thread or None


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
    telegram: TelegramCfg = field(default_factory=TelegramCfg)


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
        telegram=_pick(TelegramCfg, raw.get("telegram", {})),
    )
