"""Regime detector: is the market calm and choppy, trending, or panicking?
Strategies and position size follow the answer."""
from __future__ import annotations

import math
from collections import deque


class Regime:
    SIZE = {"warming": 0.5, "choppy": 1.0, "trending": 0.75, "panic": 0.25}

    def __init__(self, window: int = 120, panic_vol: float = 0.0045, trend_z: float = 1.2):
        self.window, self.panic_vol, self.trend_z = window, panic_vol, trend_z
        self.rets: deque = deque(maxlen=window)
        self.last = None
        self.state = "warming"
        self.vol = 0.0
        self.trend = 0.0
        self.flash = 0.0            # return over the last 10 ticks
        self._cand = None
        self._count = 0
        self.mix = {"trend": 0.0, "chop": 1.0, "panic": 0.0}
        self.risk_off = False       # set by outside signals (bond volatility, AI-stock drop, hack alert)

    def update(self, mid: float) -> str:
        if self.last:
            self.rets.append(math.log(mid / self.last))
        self.last = mid
        n = len(self.rets)
        if n < max(20, self.window // 4):
            self.state = "warming"
            return self.state
        mean = sum(self.rets) / n
        self.vol = math.sqrt(sum((r - mean) ** 2 for r in self.rets) / n)
        self.trend = sum(self.rets) / (self.vol * math.sqrt(n)) if self.vol else 0.0
        recent = list(self.rets)[-10:]
        self.flash = sum(recent)
        recent_vol = math.sqrt(sum(r * r for r in recent) / len(recent))
        squash = lambda x: 1 / (1 + math.exp(-4 * (x - 1)))        # 0.5 exactly at the switching threshold
        panic = max(squash(recent_vol / self.panic_vol), squash(-self.flash / 0.03))
        trend = (1 - panic) * squash(abs(self.trend) / self.trend_z)
        self.mix = {"trend": round(trend, 3), "chop": round(max(0.0, 1 - panic - trend), 3), "panic": round(panic, 3)}
        if recent_vol > self.panic_vol or self.flash < -0.03:
            raw = "panic"
        elif abs(self.trend) > self.trend_z:
            raw = "trending"
        else:
            raw = "choppy"
        # Enter panic at once; every other switch must hold for a while, so the state doesn't flicker.
        if raw == self.state:
            self._cand, self._count = None, 0
        elif raw == "panic" or self.state == "warming":
            self.state, self._cand, self._count = raw, None, 0
        else:
            self._count = self._count + 1 if raw == self._cand else 1
            self._cand = raw
            if self._count >= (45 if self.state == "panic" else 90):
                self.state, self._cand, self._count = raw, None, 0
        return self.state

    @property
    def size_mult(self) -> float:
        m = self.SIZE[self.state]
        return m * 0.25 if self.risk_off else m

    def view(self) -> dict:
        return {"state": self.state, "vol": self.vol, "trend": self.trend, "flash": self.flash,
                "size_mult": self.size_mult, "risk_off": self.risk_off, "mix": self.mix}
