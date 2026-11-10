"""Strategy lab (shadow only): tests many variants of the resting-bid strategy on real past prices,
keeps the best, mutates them and tests again, round after round.

It never places an order and never changes the live strategy's settings. Its purpose is to show what
would have happened, including on a slice of recent history the selection never saw (the "holdout"),
because the best of many tries always looks better on the data it was picked on than it really is.
"""
from __future__ import annotations

import json
import math
import os
import random
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

EP_HOURS = 36           # length of one test episode
STRIDE = 6              # a new episode starts every 6 hours of history
POP = 48                # variants alive per round
ELITE = 10              # the best are kept unchanged and tested again
KEEP_GENS = 36          # rounds remembered for the dashboard
LAND_N = 12             # the learned map is LAND_N x LAND_N cells
LAND_X = (0.002, 0.08)  # first bid: how far below the price (log scale)
LAND_Y = (0.005, 0.05)  # take-profit: how far above the fill (log scale)


def land_cell(first: float, tp: float) -> int:
    """Which cell of the learned map a variant's settings fall in."""
    def ix(v, lo, hi):
        return int(_clamp((math.log(_clamp(v, lo, hi)) - math.log(lo)) / (math.log(hi) - math.log(lo)) * LAND_N, 0, LAND_N - 1))
    return ix(tp, *LAND_Y) * LAND_N + ix(first, *LAND_X)


def _clamp(v, a, b):
    return max(a, min(b, v))


def random_gene(rng: random.Random) -> dict:
    lg = lambda a, b: math.exp(rng.uniform(math.log(a), math.log(b)))     # spread evenly across small and large values
    return {"first": lg(0.003, 0.06), "gap": lg(0.003, 0.03), "n": rng.randint(1, 3),
            "tp": lg(0.006, 0.04), "stop_gap": lg(0.008, 0.06), "hold": rng.randint(4, 72)}


def mutate(g: dict, rng: random.Random) -> dict:
    c = dict(g)
    for k, (lo, hi) in (("first", (0.002, 0.08)), ("gap", (0.003, 0.04)), ("tp", (0.005, 0.05)), ("stop_gap", (0.008, 0.08))):
        if rng.random() < 0.5:
            c[k] = _clamp(c[k] * math.exp(rng.gauss(0, 0.22)), lo, hi)
    if rng.random() < 0.2:
        c["n"] = _clamp(c["n"] + rng.choice((-1, 1)), 1, 3)
    if rng.random() < 0.35:
        c["hold"] = int(_clamp(c["hold"] * math.exp(rng.gauss(0, 0.3)), 4, 96))
    return c


def cross(a: dict, b: dict, rng: random.Random) -> dict:
    return {k: (a if rng.random() < 0.5 else b)[k] for k in a}


def plan(g: dict) -> Tuple[List[float], float, float, int]:
    """Gene -> (bid distances below the anchor, take-profit, stop distance below the anchor, max hours held)."""
    if "levels" in g:
        return list(g["levels"]), g["tp"], g["stop"], int(g["hold"])
    levels = [g["first"] + i * g["gap"] for i in range(g["n"])]
    return levels, g["tp"], levels[-1] + g["stop_gap"], int(g["hold"])


def simulate(bars: List[tuple], g: dict, usd: float = 95.0, maker: float = 0.005, taker: float = 0.009,
             max_pos: float = 300.0, recenter: float = 0.012):
    """Run one variant over hourly bars [(open, high, low, close), ...].
    Deliberately pessimistic where an hour's bar cannot say what happened first: a stop beats a
    take-profit, and a take-profit is only allowed from the hour after the buy.
    Returns (profit in dollars, hourly profit curve, trades [(hour, kind, price, dollars)])."""
    levels, tp, stop, hold = plan(g)
    anchor = bars[0][0]
    resting = set(range(len(levels)))
    lots: List[dict] = []
    cash, trades, curve = 0.0, [], []
    for h, (o, hi, lo, c) in enumerate(bars):
        stop_px = anchor * (1 - stop)
        for i in sorted(resting):                                   # resting bids that the hour's low reached
            px = anchor * (1 - levels[i])
            if lo <= px and sum(l["qty"] * px for l in lots) + usd <= max_pos + 1e-9:
                cash -= usd * (1 + maker)
                lots.append({"qty": usd / px, "px": px, "h": h})
                trades.append((h, "buy", px, -usd))
                resting.discard(i)
        exited = False
        for lot in lots[:]:
            if lo <= stop_px:                                       # stop first, always
                got = lot["qty"] * stop_px * (1 - taker)
                kind, px = "stop", stop_px
            elif lot["h"] < h and hi >= lot["px"] * (1 + tp):
                px = lot["px"] * (1 + tp)
                got, kind = lot["qty"] * px * (1 - maker), "profit"
            elif h - lot["h"] >= hold:
                got, kind, px = lot["qty"] * c * (1 - taker), "time", c
            else:
                continue
            cash += got
            trades.append((h, kind, px, got))
            lots.remove(lot)
            exited = True
        if not lots and (exited or not resting or c > anchor * (1 + recenter)):
            anchor, resting = c, set(range(len(levels)))            # flat: follow the price and set the bids again
        curve.append(cash + sum(l["qty"] * c * (1 - taker) for l in lots))
    return curve[-1] if curve else 0.0, curve, trades


def episodes(candles: List[list]) -> List[dict]:
    """Cut hourly candles [ts, o, h, l, c, ...] into overlapping test episodes; the newest 30% are the holdout."""
    out = []
    for i in range(0, len(candles) - EP_HOURS + 1, STRIDE):
        seg = candles[i:i + EP_HOURS]
        out.append({"t0": seg[0][0], "bars": [(r[1], r[2], r[3], r[4]) for r in seg]})
    if len(out) < 8:
        return []
    cut = int(len(out) * 0.7)
    gap = EP_HOURS // STRIDE - 1                                    # drop episodes that straddle the boundary
    for k, e in enumerate(out):
        e["split"] = "train" if k < cut - gap else ("test" if k >= cut else "skip")
    return [e for e in out if e["split"] != "skip"]


def score(g: dict, eps: List[dict], fees: dict) -> dict:
    tr, te, wins, ntr, each = [], [], 0, 0, []
    for e in eps:
        pnl, _, trades = simulate(e["bars"], g, **fees)
        each.append(round(pnl, 2))
        (tr if e["split"] == "train" else te).append(pnl)
        if e["split"] == "train":
            ntr += sum(1 for t in trades if t[1] == "buy")
            wins += pnl > 0
    m = sum(tr) / len(tr) if tr else 0.0
    sd = math.sqrt(sum((x - m) ** 2 for x in tr) / len(tr)) if tr else 0.0
    buys = ntr / len(tr) if tr else 0.0
    # Ranked on the average minus a quarter of the spread. A variant that almost never trades is ranked
    # just under breakeven, so "do nothing" cannot win the search by default.
    return {"fit": m - 0.25 * sd - (0.05 if buys < 0.15 else 0.0), "train": m, "test": sum(te) / len(te) if te else 0.0,
            "win": wins / len(tr) if tr else 0.0, "buys": buys, "each": each}


class Lab:
    def __init__(self, cfg: dict, hub, limits):
        p = cfg.get("paper", {})
        d = (cfg.get("strategies", {}) or {}).get("deep_bids", {}) or {}
        self.fees = {"usd": float(min(d.get("order_usd", 95), limits.max_order_usd)), "maker": float(p.get("maker_fee", 0.005)),
                     "taker": float(p.get("taker_fee", 0.009)), "max_pos": float(limits.max_position_usd),
                     "recenter": float(d.get("recenter", 0.012))}
        self.live_gene = {"levels": list(d.get("levels", [0.03, 0.05, 0.08])), "tp": float(d.get("take_profit", 0.02)),
                          "stop": float(d.get("stop_loss", 0.12)), "hold": int(d.get("max_hold_hours", 48))}
        self.hub = hub
        self.enabled = bool((cfg.get("lab") or {}).get("enabled", True)) and hub is not None and hub.enabled
        self.rng = random.Random()
        self.pop: List[dict] = []           # [{"id", "parent", "gene", "age", **score}]
        self.genes = {}                     # id -> gene, for every variant still shown on the dashboard
        self.gens: List[dict] = []
        cells = LAND_N * LAND_N                 # everything the lab has learned so far, by region of settings
        self.land = {"n": [0] * cells, "tr": [0.0] * cells, "te": [0.0] * cells}
        self.tested = 0
        self.gen, self._next_id = 0, 1
        self.eps: List[dict] = []
        self._eps_key = None
        self.live_score: Optional[dict] = None
        self.state, self.detail_text = ("off", "") if not self.enabled else ("waiting", "waiting for price history")
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._rescore = False
        d_ = getattr(hub, "data_dir", None)
        self.file = Path(d_) / "lab.json" if d_ and self.enabled else None
        self._load()

    # ---- what the lab has learned survives a restart ---------------------------
    def _load(self) -> None:
        try:
            s = json.loads(self.file.read_text())
            cells, keys = LAND_N * LAND_N, set(random_gene(random.Random(0)))
            land, pop = s["land"], s["pop"]
            ok = (s["v"] == 1 and all(len(land[k]) == cells for k in ("n", "tr", "te")) and len(pop) == POP
                  and all(set(v["gene"]) == keys and all(isinstance(x, (int, float)) for x in v["gene"].values()) for v in pop))
            if not ok:
                return
            self.land = {"n": [int(x) for x in land["n"]], "tr": [float(x) for x in land["tr"]], "te": [float(x) for x in land["te"]]}
            self.tested, self.gen, self._next_id = int(s["tested"]), int(s["gen"]), int(s["next"])
            self.pop = [{"id": int(v["id"]), "parent": int(v["parent"]), "gene": v["gene"], "age": int(v["age"])} for v in pop]
            self.gens = list(s.get("gens") or [])[-KEEP_GENS:]
            self.genes = {int(k): g for k, g in (s.get("genes") or {}).items()}
            self._rescore = True
        except Exception:
            pass                                                    # no saved state, or one this version cannot read: start fresh

    def _save(self) -> None:
        if not self.file:
            return
        try:
            tmp = self.file.with_name("lab.json.tmp")
            tmp.write_text(json.dumps({"v": 1, "gen": self.gen, "next": self._next_id, "tested": self.tested, "land": self.land,
                                       "pop": [{k: v[k] for k in ("id", "parent", "gene", "age")} for v in self.pop],
                                       "gens": self.gens, "genes": self.genes}))
            os.replace(tmp, self.file)
        except OSError:
            pass

    def start(self) -> None:
        if self.enabled:
            threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:                                  # the lab must never disturb the trading loop
                self.state, self.detail_text = "error", f"{type(e).__name__}: {e}"[:160]
            self._stop.wait(8 if self.gen < 30 else 45)

    # ---- one round ------------------------------------------------------------
    def _refresh_episodes(self) -> bool:
        hist = list(getattr(self.hub, "history", []) or [])[:-1]    # leave out the hour still forming
        if len(hist) < EP_HOURS * 3:
            return False
        key = hist[-1][0]
        if key != self._eps_key:
            self.eps, self._eps_key = episodes(hist), key
        return bool(self.eps)

    def step(self) -> None:
        if not self.hub.live_prices:
            self.state, self.detail_text = "idle", "the lab needs real prices"
            return
        if not self._refresh_episodes():
            self.state, self.detail_text = "waiting", "waiting for price history"
            return
        rng = self.rng
        if self._rescore:                        # a population loaded from disk: score it on today's history first
            for v in self.pop:
                v.update(score(v["gene"], self.eps, self.fees))
            self._rescore = False
        if not self.pop:
            new = [{"id": self._id(), "parent": 0, "gene": random_gene(rng), "age": 0} for _ in range(POP)]
        else:
            ranked = sorted(self.pop, key=lambda v: -v["fit"])
            elite = ranked[:ELITE]
            new = [{**v, "age": v["age"] + 1} for v in elite]       # kept and tested again
            while len(new) < POP - 5:
                a, b = rng.choice(ranked[:16]), rng.choice(ranked[:16])
                child = mutate(cross(a["gene"], b["gene"], rng) if rng.random() < 0.4 else a["gene"], rng)
                new.append({"id": self._id(), "parent": a["id"], "gene": child, "age": 0})
            while len(new) < POP:                                   # a few complete strangers every round
                new.append({"id": self._id(), "parent": 0, "gene": random_gene(rng), "age": 0})
        for v in new:
            v.update(score(v["gene"], self.eps, self.fees))
            if v["age"] == 0:                                       # a variant tried for the first time: remember what it showed
                levels, tp, _, _ = plan(v["gene"])
                c = land_cell(levels[0], tp)
                v["cell"] = c
                self.land["n"][c] += 1
                self.land["tr"][c] += v["train"]
                self.land["te"][c] += v["test"]
                self.tested += 1
        live = score(self.live_gene, self.eps, self.fees)
        with self.lock:
            self.pop, self.gen, self.live_score = new, self.gen + 1, live
            self.gens.append({"g": self.gen, "v": [[v["id"], v["parent"], round(v["fit"], 3), round(v["train"], 3),
                                                    round(v["test"], 3), round(v["win"], 3), round(v["buys"], 2), v["age"]]
                                                   for v in new]})
            self.gens = self.gens[-KEEP_GENS:]
            for v in new:
                self.genes[v["id"]] = v["gene"]
            shown = {row[0] for g in self.gens for row in g["v"]}
            self.genes = {k: g for k, g in self.genes.items() if k in shown}
            self.state = "running"
            self.detail_text = f"round {self.gen}: {POP} variants on {len(self.eps)} slices of real history"
        self._save()

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    # ---- for the dashboard --------------------------------------------------------
    def view(self, since: int = -1) -> dict:
        if since == self.gen and self.gen:                          # nothing new since the dashboard last asked
            return {"state": self.state, "detail": self.detail_text, "gen": self.gen, "unchanged": True}
        with self.lock:
            pop = [] if self._rescore else self.pop                 # loaded from disk and not yet scored on today's history
            best = max(pop, key=lambda v: v["fit"]) if pop else None
            n_tr = sum(e["split"] == "train" for e in self.eps)
            race = {"t0": [e["t0"] for e in self.eps], "split": [e["split"] for e in self.eps],
                    "v": [[v["id"], v["each"]] for v in pop], "live": self.live_score["each"] if self.live_score else []}
            n = self.land["n"]
            land = {"nx": LAND_N, "ny": LAND_N, "x": LAND_X, "y": LAND_Y, "n": n, "tested": self.tested,
                    "tr": [round(a / c, 3) if c else None for a, c in zip(self.land["tr"], n)],
                    "te": [round(a / c, 3) if c else None for a, c in zip(self.land["te"], n)],
                    "pop": [[v["id"], round(plan(v["gene"])[0][0], 4), round(plan(v["gene"])[1], 4), round(v["test"], 3),
                             round(v["train"], 3), v["age"]] for v in pop],
                    "live": [self.live_gene["levels"][0], self.live_gene["tp"],
                             round(self.live_score["test"], 3) if self.live_score else 0.0]}
            return {"state": self.state, "detail": self.detail_text, "gen": self.gen, "gens": self.gens, "race": race, "land": land,
                    "best": best["id"] if best else None, "live": {k: x for k, x in (self.live_score or {}).items() if k != "each"} or None,
                    "episodes": len(self.eps), "train": n_tr, "test": len(self.eps) - n_tr,
                    "hours": EP_HOURS, "pop": POP, "elite": ELITE}

    def variant(self, vid) -> Optional[dict]:
        """Everything about one variant: its settings and every trade path it produced, good and bad."""
        with self.lock:
            gene = self.live_gene if vid == "live" else self.genes.get(int(vid)) if str(vid).isdigit() else None
            eps = list(self.eps)
        if gene is None:
            return None
        levels, tp, stop, hold = plan(gene)
        paths = []
        for e in eps:
            pnl, curve, trades = simulate(e["bars"], gene, **self.fees)
            paths.append({"t0": e["t0"], "split": e["split"], "pnl": round(pnl, 2), "eq": [round(x, 2) for x in curve],
                          "hi": [round(b[1], 1) for b in e["bars"]], "lo": [round(b[2], 1) for b in e["bars"]],
                          "c": [round(b[3], 1) for b in e["bars"]], "o": round(e["bars"][0][0], 1),
                          "tr": [[t[0], t[1], round(t[2], 1), round(t[3], 2)] for t in trades]})
        fut = []
        f = self.hub.forecast if self.hub.is_fresh(self.hub.candles[-1][4] if self.hub.candles else None, time.time()) else None
        if f:
            for cl, hi, lo in zip(f["closes"], f["highs"], f["lows"]):
                bars, prev = [], f["last_close"]
                for c, h_, l_ in zip(cl, hi, lo):
                    bars.append((prev, h_, l_, c))
                    prev = c
                pnl, curve, trades = simulate(bars, gene, **self.fees)
                fut.append({"t0": f["base_ts"] + 3600, "split": "model", "pnl": round(pnl, 2), "eq": [round(x, 2) for x in curve],
                            "hi": [round(b[1], 1) for b in bars], "lo": [round(b[2], 1) for b in bars],
                            "c": [round(b[3], 1) for b in bars], "o": round(bars[0][0], 1),
                            "tr": [[t[0], t[1], round(t[2], 1), round(t[3], 2)] for t in trades]})
        sc = {k: x for k, x in score(gene, eps, self.fees).items() if k != "each"}
        return {"id": "live" if vid == "live" else int(vid), "levels": [round(x, 4) for x in levels], "tp": round(tp, 4), "stop": round(stop, 4), "hold": hold,
                "usd": self.fees["usd"], "paths": paths, "futures": fut, **sc}
