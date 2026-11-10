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
NEED_ROUNDS = 30        # how many rounds in a row a result must hold before the dashboard calls it progress
KEEP_HIST = 360         # points of progress history kept (older ones are thinned, not dropped)
MAX_EPS = 1100          # with years of history the episodes are spaced further apart so a round stays quick
DEEP_MIN = 2000         # hours of history from which the lab counts as "on deep history"
WEEK = 168
PROOF_POOL = 300        # fixed set of variants replayed over the whole history for the walk-forward proof
PROOF_CHUNK = 20        # how many of them are replayed per round
PROOF_TRAIN, PROOF_TEST, PROOF_TOP = 52, 13, 10      # pick the 10 best on the past year, judge them on the next quarter, roll forward
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
    out, stride = [], STRIDE
    while (len(candles) - EP_HOURS) // stride + 1 > MAX_EPS:
        stride += STRIDE
    for i in range(0, len(candles) - EP_HOURS + 1, stride):
        seg = candles[i:i + EP_HOURS]
        out.append({"t0": seg[0][0], "bars": [(r[1], r[2], r[3], r[4]) for r in seg]})
    if len(out) < 8:
        return []
    cut = int(len(out) * 0.7)
    gap = -(-EP_HOURS // stride) - 1                                # drop episodes that straddle the boundary
    for k, e in enumerate(out):
        e["split"] = "train" if k < cut - gap else ("test" if k >= cut else "skip")
    return [e for e in out if e["split"] != "skip"]


def tstat(xs: List[float]) -> Tuple[float, float]:
    """Average, and how many standard errors it sits from zero (about 3 or more is very unlikely to be luck)."""
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else 0.0), 0.0
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m, (m / (sd / math.sqrt(n)) if sd > 1e-12 else 0.0)


def weekly(candles: List[list], g: dict, fees: dict) -> Tuple[List[float], int]:
    """One continuous replay over the whole history: profit in each full week, and how many buys it made."""
    bars = [(r[1], r[2], r[3], r[4]) for r in candles]
    _, curve, trades = simulate(bars, g, **fees)
    out, last = [], 0.0
    for k in range(WEEK - 1, len(curve), WEEK):
        out.append(curve[k] - last)
        last = curve[k]
    return out, sum(1 for t in trades if t[1] == "buy")


def walk_forward(pool: List[List[float]], live: List[float]) -> dict:
    """The honest test of learning. Again and again: pick the best variants using ONLY the past year, then see how
    those picks do over the following quarter, which they have never seen. Compare with the average variant and
    with the live settings over exactly the same unseen weeks."""
    W = len(live)
    picks, avg, lv = [], [], []
    t = PROOF_TRAIN
    while t + PROOF_TEST <= W:
        def fit(w):
            m, sd = sum(w) / len(w), math.sqrt(sum((x - sum(w) / len(w)) ** 2 for x in w) / len(w))
            return m - 0.25 * sd
        top = sorted(pool, key=lambda w: -fit(w[t - PROOF_TRAIN:t]))[:PROOF_TOP]
        for k in range(t, t + PROOF_TEST):
            picks.append(sum(w[k] for w in top) / len(top))
            avg.append(sum(w[k] for w in pool) / len(pool))
            lv.append(live[k])
        t += PROOF_TEST
    pair = lambda a, b: [x - y for x, y in zip(a, b)]
    r = lambda mt: {"mean": round(mt[0], 3), "t": round(mt[1], 2)}
    cum = lambda xs: [round(sum(xs[:i + 1]), 2) for i in range(len(xs))]
    return {"weeks": W, "unseen_weeks": len(picks), "picks": r(tstat(picks)), "average": r(tstat(avg)), "live_unseen": r(tstat(lv)),
            "vs_average": r(tstat(pair(picks, avg))), "vs_live": r(tstat(pair(picks, lv))),
            "live_all": {**r(tstat(live)), "total": round(sum(live), 2), "won": sum(x > 0 for x in live), "lost": sum(x < 0 for x in live)},
            "ahead_in_hindsight": sum(sum(w) > 0 for w in pool), "curve": {"picks": cum(picks), "live": cum(lv), "average": cum(avg)}}


def hold_table(candles: List[list], maker: float = 0.005, band: float = 0.10, month: int = 720) -> dict:
    """What simply holding would have done over the whole history, for several shares of a $100 account: no prediction,
    one buy at the start, and a move back to the chosen share only when it drifts more than `band` away, at most monthly."""
    rows = []
    for pct in (25, 50, 75, 100):
        cash, px0 = 100.0, candles[0][4]
        buy = 100.0 * pct / 100
        qty, cash, fees, trades, last = buy / px0, cash - buy * (1 + maker), buy * maker, 1, 0
        peak, dd, curve, wk_prev, worst, best = 100.0, 0.0, [], 100.0, 0.0, 0.0
        for h, r in enumerate(candles):
            px = r[4]
            eq = cash + qty * px
            if pct < 100 and h - last >= month and abs(qty * px / eq - pct / 100) > band:
                move = eq * pct / 100 - qty * px                    # dollars of coin to buy (+) or sell (-)
                qty += move / px
                cash -= move + abs(move) * maker
                fees += abs(move) * maker
                trades, last = trades + 1, h
                eq = cash + qty * px
            peak = max(peak, eq)
            dd = min(dd, eq / peak - 1)
            if h % WEEK == WEEK - 1:
                chg = eq / wk_prev - 1
                worst, best, wk_prev = min(worst, chg), max(best, chg), eq
                curve.append(round(eq, 2))
        rows.append({"pct": pct, "end": round(eq, 2), "low": round(min(curve), 2) if curve else 100.0, "drawdown": round(dd, 4),
                     "worst_week": round(worst, 4), "best_week": round(best, 4), "fees": round(fees, 2), "trades": trades, "curve": curve})
    px = [r[4] for r in candles]
    return {"weeks": len(candles) // WEEK, "from": candles[0][0], "to": candles[-1][0], "rows": rows,
            "price": {"start": px[0], "end": px[-1], "high": max(px), "low": min(px)}}


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
        self.hist: List[list] = []              # one line per round: [round, picks on unseen, picks on trained, champion on unseen, live on unseen, variants ahead on unseen]
        self.runs = {"pos": 0, "live": 0, "top_pos": 0, "top_live": 0}      # rounds in a row the lab's picks made money / beat the live settings, on unseen history
        self.deep = False                       # True once it is learning from years of history instead of days
        self.proof: Optional[dict] = None       # the walk-forward verdict (see walk_forward)
        self.holds: Optional[dict] = None       # what simply holding would have done (see hold_table)
        self._pw: List[List[float]] = []        # weekly results of the proof variants replayed so far
        self._pw_live: Optional[tuple] = None
        self._pw_key = None
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
            self.deep = bool(s.get("deep"))
            self.proof = s.get("proof") if isinstance(s.get("proof"), dict) and s["proof"].get("done") else None
            self.hist = [list(r) for r in (s.get("hist") or []) if isinstance(r, list) and len(r) == 6][-KEEP_HIST:]
            self.runs.update({k: int(v) for k, v in (s.get("runs") or {}).items() if k in self.runs})
            if not self.hist:                                       # saved by an older version: rebuild what the kept rounds still show
                for g in self.gens:
                    top = sorted(g["v"], key=lambda r: -r[2])[:ELITE]
                    self.hist.append([g["g"], round(sum(r[4] for r in top) / len(top), 3), round(sum(r[3] for r in top) / len(top), 3),
                                      top[0][4], None, sum(r[4] > 0 for r in g["v"])])
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
                                       "gens": self.gens, "genes": self.genes, "hist": self.hist, "runs": self.runs,
                                       "deep": self.deep, "proof": self.proof if self.proof and self.proof.get("done") else None}))
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
            try:
                self._proof_step()
            except Exception as e:
                self.detail_text = f"proof: {type(e).__name__}: {e}"[:160]
            self._stop.wait(8 if self.gen < 30 or (getattr(self.hub, "deep_done", False) and len(self._pw) < PROOF_POOL) else 45)

    # ---- one round ------------------------------------------------------------
    def _refresh_episodes(self) -> bool:
        hist = list(getattr(self.hub, "history", []) or [])[:-1]    # leave out the hour still forming
        if len(hist) < EP_HOURS * 3:
            return False
        key = (hist[-1][0], len(hist))
        if key != self._eps_key:
            self.eps, self._eps_key = episodes(hist), key
            if len(hist) >= DEEP_MIN and not self.deep:             # years of history have arrived: what was learned on a fortnight is set aside
                with self.lock:
                    cells = LAND_N * LAND_N
                    self.land = {"n": [0] * cells, "tr": [0.0] * cells, "te": [0.0] * cells}
                    self.tested, self.hist, self.gens = 0, [], []
                    self.runs = {"pos": 0, "live": 0, "top_pos": 0, "top_live": 0}
                    for v in self.pop:
                        v["age"] = 0
                    self.deep, self._rescore = True, bool(self.pop)
        return bool(self.eps)

    def _proof_step(self) -> None:
        """A few more variants replayed over the whole history each round; when all are in, the walk-forward verdict."""
        hist = list(getattr(self.hub, "history", []) or [])[:-1]
        if not getattr(self.hub, "deep_done", False) or len(hist) < (PROOF_TRAIN + 2 * PROOF_TEST) * WEEK:
            return
        hist = hist[len(hist) % WEEK:]                              # whole weeks, ending now
        key = (hist[-1][0] // (WEEK * 3600), len(hist) // WEEK)
        if key != self._pw_key:                                     # a new week of prices: replay again
            self._pw, self._pw_live, self._pw_key = [], None, key
            self.holds = hold_table(hist, self.fees["maker"])
            if self.proof:
                self.proof = {**self.proof, "stale": True}
        if len(self._pw) >= PROOF_POOL:
            return
        if self._pw_live is None:
            self._pw_live = weekly(hist, self.live_gene, self.fees)
        rng = random.Random(20261109 + len(self._pw))               # the same fixed set every time, so results can be compared
        for _ in range(PROOF_CHUNK):
            if len(self._pw) < PROOF_POOL:
                self._pw.append(weekly(hist, random_gene(rng), self.fees)[0])
        if len(self._pw) >= PROOF_POOL:
            p = walk_forward(self._pw, self._pw_live[0])
            p.update({"done": True, "pool": PROOF_POOL, "live_buys": self._pw_live[1], "made": time.time(),
                      "from": hist[0][0], "to": hist[-1][0], "train": PROOF_TRAIN, "test": PROOF_TEST, "top": PROOF_TOP})
            self.proof = p
            self._save()

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
            # progress: how the variants the lab would pick (the best 10 on the history it trains on) did on history it never saw
            top = sorted(new, key=lambda v: -v["fit"])[:ELITE]
            picks = sum(v["test"] for v in top) / len(top)
            self.hist.append([self.gen, round(picks, 3), round(sum(v["train"] for v in top) / len(top), 3), round(top[0]["test"], 3),
                              round(live["test"], 3), sum(v["test"] > 0 for v in new)])
            if len(self.hist) > KEEP_HIST:                          # thin the older half so a long run still fits
                half = len(self.hist) // 2
                self.hist = self.hist[:half:2] + self.hist[half:]
            r = self.runs
            r["pos"] = r["pos"] + 1 if picks > 0 else 0
            r["live"] = r["live"] + 1 if picks > live["test"] else 0
            r["top_pos"], r["top_live"] = max(r["top_pos"], r["pos"]), max(r["top_live"], r["live"])
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
                    "progress": {"hist": self.hist, "need": NEED_ROUNDS, **self.runs},
                    "holds": self.holds, "proof": self.proof, "proof_progress": round(len(self._pw) / PROOF_POOL, 3), "deep": self.deep,
                    "history_hours": len(getattr(self.hub, "history", []) or []),
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
        for e in eps[-60:]:                                         # the most recent slices; with years of history there are too many to draw
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
