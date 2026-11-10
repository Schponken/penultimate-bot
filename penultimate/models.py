"""Shared data types."""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Optional

_ids = itertools.count(int(time.time() * 1000))     # unique across restarts


def is_perp(symbol: str) -> bool:
    """Perpetual futures use the ccxt form 'BTC/USD:USD'. Spot is 'BTC/USD'."""
    return ":" in symbol


@dataclass
class Tick:
    ts: float                      # unix seconds (simulated or real)
    symbol: str
    bid: float
    ask: float
    last: float
    funding_rate: Optional[float] = None   # per 8h period, perps only

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 1e4 if self.mid else 0.0


@dataclass
class Order:
    symbol: str
    side: str                      # 'buy' | 'sell'
    qty: float
    type: str = "market"           # 'market' | 'limit'
    price: Optional[float] = None
    strategy: str = "manual"
    tag: str = ""                  # free label, e.g. 'tp', 'ladder-2'
    id: int = field(default_factory=lambda: next(_ids))
    ts: float = 0.0
    status: str = "new"            # new | open | filled | cancelled | rejected
    reason: str = ""
    venue_id: str = ""             # id assigned by a real exchange

    @property
    def signed_qty(self) -> float:
        return self.qty if self.side == "buy" else -self.qty


@dataclass
class Fill:
    order_id: int
    ts: float
    symbol: str
    side: str
    qty: float
    price: float
    fee: float
    strategy: str
    tag: str = ""

    @property
    def signed_qty(self) -> float:
        return self.qty if self.side == "buy" else -self.qty


@dataclass
class Position:
    qty: float = 0.0               # signed; spot can't go below zero
    avg: float = 0.0               # average entry price
