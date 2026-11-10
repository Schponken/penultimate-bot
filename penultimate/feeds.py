"""Price feeds. SimFeed is a built-in practice market with volatility shifts, liquidation
wicks and funding swings. LiveFeed reads real public prices from the exchange."""
from __future__ import annotations

import math
import random
import time
from typing import Iterator, List, Optional

from .models import Tick


class SimFeed:
    live = False

    def __init__(self, cfg: dict, spot: str, perp: Optional[str]):
        s = cfg.get("sim", {})
        self.rng = random.Random(s.get("seed", 7))
        self.dt = float(s.get("seconds_per_tick", 60))
        self.delay = float(s.get("real_delay", 0.05))
        self.spot, self.perp = spot, perp
        self.base = float(s.get("start_price", 75000))
        self.ts = 1_780_000_000.0          # fixed start so runs are repeatable
        self.wild = False
        self.shock = 0.0                   # current liquidation dislocation (negative = dumped)
        self.shock_left = 0
        self.shock_target = 0.0
        self.funding = 0.0001
        self.hot = 0                       # ticks left of crowded-long funding
        self.stopped = False
        self.errors = 0
        self.last_error = ""

    def now(self) -> float:
        return self.ts

    def step(self) -> List[Tick]:
        r = self.rng
        self.ts += self.dt
        if r.random() < (0.01 if self.wild else 0.004):
            self.wild = not self.wild
        sigma = 0.0016 if self.wild else 0.0007
        self.base *= math.exp(r.gauss(0.0, sigma))
        if self.shock_left == 0 and r.random() < (0.0007 if self.wild else 0.0002):
            self.shock_target = -r.uniform(0.04, 0.09)
            self.shock_left = 5
        if self.shock_left > 0:            # the dump: fast
            self.shock += (self.shock_target - self.shock) / self.shock_left
            self.shock_left -= 1
            if self.shock_left == 0:
                self.base *= 1 + self.shock * 0.1       # 10% of the drop sticks
                self.shock *= 0.9
                self.funding -= 0.0003
        else:                              # the bounce: slower
            self.shock *= 0.96
        price = self.base * (1 + self.shock)
        stress = min(1.0, abs(self.shock) * 12 + (0.25 if self.wild else 0.0))
        half = price * (0.00005 + 0.0015 * stress)
        if self.hot == 0 and r.random() < 0.003:
            self.hot = r.randint(300, 1500)
        target = 0.0006 if self.hot else 0.0001
        self.hot = max(0, self.hot - 1)
        self.funding += (target - self.funding) * 0.01 + r.gauss(0, 0.000008)
        out = [Tick(self.ts, self.spot, price - half, price + half, price)]
        if self.perp:
            pp = price * (1 + self.funding * 2.5)
            out.append(Tick(self.ts, self.perp, pp - half, pp + half, pp, funding_rate=self.funding))
        return out

    def stream(self) -> Iterator[List[Tick]]:
        while not self.stopped:
            yield self.step()
            if self.delay:
                time.sleep(self.delay)


class LiveFeed:
    """Real prices, polled about once a second through the ccxt library.
    Not yet run against a real exchange from the build environment; the engine's
    start-up self-check verifies it on your machine before anything trades."""
    live = True

    def __init__(self, exchange, spot: str, perp: Optional[str], perp_exchange=None, interval: float = 1.0):
        self.pairs = [(spot, exchange)]
        if perp:
            self.pairs.append((perp, perp_exchange or exchange))
        self.interval = interval
        self.stopped = False
        self.errors = 0
        self.last_error = ""
        self._funding = {}
        self._funding_at = 0.0

    def now(self) -> float:
        return time.time()

    def _tick(self, sym: str, ex) -> Tick:
        t = ex.fetch_ticker(sym)
        last = t.get("last") or t.get("close")
        bid, ask = t.get("bid") or last, t.get("ask") or last
        fr = None
        if ":" in sym:
            if time.time() - self._funding_at > 60 and ex.has.get("fetchFundingRate"):
                try:
                    self._funding[sym] = ex.fetch_funding_rate(sym).get("fundingRate")
                except Exception as e:      # funding is optional; prices still flow
                    self.last_error = f"funding: {e}"
                self._funding_at = time.time()
            fr = self._funding.get(sym)
        return Tick(time.time(), sym, float(bid), float(ask), float(last), fr)

    def stream(self) -> Iterator[List[Tick]]:
        while not self.stopped:
            out: List[Tick] = []
            for sym, ex in self.pairs:
                try:
                    out.append(self._tick(sym, ex))
                except Exception as e:
                    self.errors += 1
                    self.last_error = f"{sym}: {e}"
            yield out                      # empty list = heartbeat, lets stale-feed detection run
            time.sleep(self.interval)
