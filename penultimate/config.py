"""Load settings. Limits live in their own file and are frozen once loaded."""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Limits:
    max_order_usd: float
    max_position_usd: float
    max_gross_exposure_usd: float
    max_leverage: float
    max_daily_loss_usd: float
    max_transfer_usd: float
    stale_feed_seconds: float
    max_spread_bps: float
    cooldown_seconds: float

    def __post_init__(self):
        for f in fields(self):
            v = getattr(self, f.name)
            if not isinstance(v, (int, float)) or v <= 0:
                raise ValueError(f"limits.yaml: {f.name} must be a positive number, got {v!r}")


def load_limits(path: Path | None = None) -> Limits:
    raw = yaml.safe_load((path or ROOT / "limits.yaml").read_text())
    names = {f.name for f in fields(Limits)}
    missing = names - set(raw)
    if missing:
        raise ValueError(f"limits.yaml is missing: {sorted(missing)}")
    return Limits(**{k: float(raw[k]) for k in names})


def load_config(path: Path | None = None) -> dict:
    cfg = yaml.safe_load((path or ROOT / "config.yaml").read_text())
    if cfg.get("mode") not in ("paper", "live"):
        raise ValueError("config.yaml: mode must be 'paper' or 'live'")
    if cfg.get("feed") not in ("sim", "live"):
        raise ValueError("config.yaml: feed must be 'sim' or 'live'")
    if cfg["mode"] == "live" and cfg["feed"] != "live":
        raise ValueError("config.yaml: live mode needs feed: live")
    return cfg
