"""The engine: reads prices, runs the automatic reactions, lets strategies propose orders,
pushes every order through the risk engine, and records everything."""
from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional

from .broker_paper import PaperBroker
from .config import ROOT, Limits
from .feeds import LiveFeed, SimFeed
from .models import Fill, Order, Position, Tick
from .regime import Regime
from .risk import RiskEngine
from .scorekeeper import Scorekeeper
from .store import Store
from .strategies import REGISTRY

FUNDING_PERIOD = 8 * 3600
VERSION = "2.1"


class Engine:
    def __init__(self, cfg: dict, limits: Limits, feed=None, broker=None,
                 store: Optional[Store] = None, data_dir: Optional[Path] = None, hub=None):
        self.cfg, self.limits = cfg, limits
        self.hub = hub                      # forecast model plumbing (optional)
        self.lab = None                     # set by the program: the lab's walk-forward verdict can make real money stand aside
        self._gated = False
        self._overruled = False
        self.reexec = False                 # True = restart the whole program (picks up updated files)
        self._last_hub = 0.0
        self.mode = cfg["mode"]
        self.spot: str = cfg["spot_symbol"]
        self.perp: Optional[str] = cfg.get("perp_symbol") or None
        self.data_dir = data_dir or ROOT / "data"
        self.store = store or Store(self.data_dir / "penultimate.db")
        self.taker_fee = float(cfg["paper"]["taker_fee"])
        self.perp_taker_fee = float(cfg["paper"].get("perp_taker_fee", self.taker_fee))
        self.risk = RiskEngine(limits)
        self.flow = {"evals": 0, "placed": 0, "refused": 0, "fills": 0, "reactions": 0, "score_checks": 0}   # real event counts, drawn on the dashboard
        self._overruled = bool(self.store.get("trade_unproven"))
        saved_risk = self.store.get(f"risk:{self.mode}", {}) or {}
        self.risk.load_state(saved_risk if saved_risk.get("v") == 3 else {})    # states saved before v1.9 could hold a loss baseline from the wrong account
        self.regime = Regime(**cfg.get("regime", {}))
        self.problems: List[str] = []       # anything that blocks trading, shown on the dashboard
        self.blocked = False
        self.ticks: Dict[str, Tick] = {}
        self.marks: Dict[str, float] = {}
        self.last_seen: Dict[str, float] = {}
        self.events: deque = deque(maxlen=80)
        self.fills: deque = deque(maxlen=40)
        self.curve: deque = deque(maxlen=400)
        self.regime_hist: deque = deque(maxlen=180)
        self.commands: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.snap: dict = {"ready": False}
        self.exit = False                   # True = shut the whole program down
        self.started = time.time()
        self.n = 0
        self._stale = self._wide = self._flash = False
        self._fund_period: Optional[int] = None
        self._last_sync = 0.0
        self._last_score = 0.0
        self._reject_seen: Dict[str, float] = {}
        self._signals_mtime = 0.0
        self._last_regime = "warming"
        self._tilt = 0.0
        self._handle_fills_later: List[Fill] = []
        self._last_status = 0.0
        self._retry_feed = False
        self._score_cache: dict = {}

        self.feed = feed or self._make_feed()
        self.broker = broker or self._make_broker()
        self.score = Scorekeeper(self.store, self.spot)
        self.strategies = []
        for name, params in cfg.get("strategies", {}).items():
            if not params.get("enabled") or name not in REGISTRY:
                continue
            if name == "funding_carry" and (not self.perp or (self.mode == "live" and not params.get("allow_live"))):
                self.event("setup", "funding_carry is off: the hedge leg is not verified on this exchange yet")
                continue
            s = REGISTRY[name](params, self)
            s.load_state(self.store.get(f"strategy:{name}:{self.mode}:{cfg['feed']}", {}))
            self.strategies.append(s)
        if self.perp and not any(s.name == "funding_carry" for s in self.strategies):
            self.perp = None                    # nothing uses the futures price, so don't fetch or depend on it
            if hasattr(self.feed, "pairs"):
                self.feed.pairs = [p for p in self.feed.pairs if ":" not in p[0]]

    # ---- construction ------------------------------------------------------
    def _make_feed(self):
        if self.cfg["feed"] == "sim":
            return SimFeed(self.cfg, self.spot, self.perp)
        from .broker_live import make_exchange
        try:
            ex = make_exchange(self.cfg["venue"])
            markets = ex.load_markets()
            perp = self.perp if self.perp in markets else None
            if self.perp and not perp:
                self.event("setup", f"{self.perp} is not listed on {self.cfg['venue']}; running spot only")
                self.perp = None
            return LiveFeed(ex, self.spot, perp)
        except Exception as e:
            self.problems.append(f"Could not reach {self.cfg['venue']} for prices (retrying every 30s): {e}")
            self.blocked = True
            self._retry_feed = True
            return SimFeed({"sim": {"real_delay": 1.0}}, self.spot, None)

    def _make_broker(self):
        if self.mode == "paper":
            b = PaperBroker(**self.cfg["paper"])
            saved = self.store.get("paper_state")
            if saved and saved.get("feed") == self.cfg["feed"]:
                b.cash, b.realized, b.fees, b.funding = saved["cash"], saved["realized"], saved["fees"], saved["funding"]
                b.positions = {k: Position(**v) for k, v in saved["positions"].items()}
            elif saved:
                self.event("setup", "price source changed, practice account started fresh")
                for name in REGISTRY:
                    self.store.set(f"strategy:{name}:paper:{self.cfg['feed']}", {})
                self.risk.resume()
                self.risk.day_key = ""
            return b
        from .broker_live import LiveBroker, load_keys, make_exchange
        keys = load_keys()
        if not keys:
            self.problems.append("No exchange keys saved yet")
            self.blocked = True
            return PaperBroker(0.0, 0.0, 0.0, 0.0)
        b = LiveBroker(make_exchange(self.cfg["venue"], keys), max_transfer_usd=self.limits.max_transfer_usd)
        b.marks = self.marks
        if (self.cfg.get("live") or {}).get("own_whole_account", True):
            b.own_symbols = [self.spot]             # every dollar and every coin of this market in the account is the bot's
        probs = b.self_check([self.spot])
        if probs:
            self.problems += probs
            self.blocked = True
        else:
            b.load_state(self.store.get("live_state") or {})
            self._handle_fills_later = b.sync()     # anything that filled while the bot was off
        return b

    # ---- logging -----------------------------------------------------------
    def event(self, kind: str, detail: str, ts: Optional[float] = None) -> None:
        ts = ts if ts is not None else (self.feed.now() if hasattr(self, "feed") else time.time())
        self.events.appendleft({"ts": ts, "kind": kind, "detail": detail})
        if kind == "reaction":
            self.flow["reactions"] += 1
        self.store.event(ts, kind, detail)

    def size_mult(self, use_regime: bool = True) -> float:
        """Position-size multiplier: market regime, outside risk-off signals, and the capped
        scorekeeper tilt (real prices only)."""
        m = self.regime.size_mult if use_regime else (0.25 if self.regime.risk_off else 1.0)
        return m * (1 + (self._tilt if self.feed.live else 0.0))

    def free_cash(self) -> float:
        """Dollars not already tied up in resting buy orders."""
        b = self.broker
        if hasattr(b, "free_cash"):
            return b.free_cash
        return b.cash - sum(o.qty * (o.price or 0) for o in b.open_orders if o.side == "buy" and ":" not in o.symbol)

    def can_add_risk(self) -> bool:
        return not self.risk.halted and self.feed.now() >= self.risk.paused_until and not self.gate()

    def gate(self) -> str:
        """With real money: if three years of real prices say the live settings reliably lose, no new buys are placed.
        This can only ever hold the bot back; it never places, sizes or changes an order. You can overrule it from the dashboard."""
        return "" if self._overruled else self.gate_reason()

    def gate_reason(self) -> str:
        if self.mode != "live":
            return ""
        p = getattr(self.lab, "proof", None) if self.lab else None
        la = (p or {}).get("live_all") or {}
        if p and p.get("done") and la.get("mean", 0) < 0 and la.get("t", 0) <= -3:
            return (f"over {p['weeks']} weeks of real prices these settings lost ${-la['mean']:.2f} a week on average "
                    f"({la['lost']} losing weeks, {la['won']} winning)")
        return ""

    def would_pass(self, order: Order):
        """Ask the risk engine about an order without sending it."""
        tick = self.ticks.get(order.symbol)
        if tick is None:
            return False, "no price yet"
        r = self.risk                       # a question, not an order: leave the counters as they were
        before = (r.rejections, r.checks, r.passed, dict(r.by_rule), dict(r.last))
        ok, why = r.check(order, tick, self.broker, self.marks)
        r.rejections, r.checks, r.passed, r.by_rule, r.last = before
        return ok, why

    # ---- the only door orders go through -----------------------------------
    def place(self, order: Order) -> Order:
        tick = self.ticks.get(order.symbol)
        if tick is None:
            order.status, order.reason = "rejected", "no price yet"
            return order
        if order.side == "sell" and ":" not in order.symbol:
            held = self.broker.position(order.symbol).qty       # sell what is actually there,
            if 0 < order.qty - held < 1e-9 * max(1.0, order.qty):   # not a rounding crumb more
                order.qty = held
        ok, why = self.risk.check(order, tick, self.broker, self.marks)
        self.flow["placed" if ok else "refused"] += 1
        if not ok:
            order.status, order.reason = "rejected", why
        else:
            fills = self.broker.submit(order, tick)
            self._handle_fills(fills)
            if self.broker.name == "live":
                self.store.set("live_state", self.broker.to_state())
        if order.status == "rejected":
            key = f"{order.strategy}:{order.side}:{order.reason[:28]}"
            if tick.ts - self._reject_seen.get(key, -1e18) > 6 * 3600:
                self._reject_seen[key] = tick.ts
                self.event("blocked", f"{order.strategy} {order.side} {order.symbol}: {order.reason}", tick.ts)
        return order

    def _handle_fills(self, fills: List[Fill]) -> None:
        self.flow["fills"] += len(fills)
        for f in fills:
            self.store.fill(f, self.mode)
            self.fills.appendleft({"ts": f.ts, "symbol": f.symbol, "side": f.side, "qty": f.qty,
                                   "price": f.price, "fee": f.fee, "strategy": f.strategy, "tag": f.tag})
            for s in self.strategies:
                if s.name == f.strategy:
                    s.on_fill(f)
        if fills and self.broker.name == "live" and not self.blocked:
            self.save_state()

    # ---- automatic reactions -----------------------------------------------
    def emergency(self, reason: str) -> None:
        """Sell everything, stop, and wait for a person."""
        fills = self.broker.flatten(self.ticks)
        self.risk.halt(reason)
        for s in self.strategies:
            s.reset()
        self._handle_fills(fills)
        self.store.set(f"risk:{self.mode}", {**self.risk.to_state(), "v": 3})
        self.event("reaction", f"{reason} -> cancelled all orders, closed {len(fills)} position(s), halted")
        self.save_state()

    def _react(self, now: float, batch: List[Tick]) -> None:
        L = self.limits
        # prices stopped arriving -> cancel everything and pause
        watched = [self.spot] + ([self.perp] if self.perp in self.last_seen else [])   # the futures price is optional
        stale = any(now - self.last_seen.get(s, self.started if self.feed.live else now) > L.stale_feed_seconds
                    for s in watched)
        if stale and not self._stale:
            n = self.broker.cancel_all()
            self.risk.pause("prices stopped updating", now + L.cooldown_seconds)
            self.event("reaction", f"no fresh prices for {L.stale_feed_seconds:g}s -> cancelled {n} order(s), paused", now)
        elif self._stale and not stale:
            self.risk.pause("waiting after a price outage", now + L.cooldown_seconds)
            self.event("reaction", "prices are back -> cooling down before adding risk", now)
        self._stale = stale
        # bid/ask gap blew out -> no new entries
        wide = any(t.spread_bps > L.max_spread_bps for t in batch)
        if wide and not self._wide:
            self.risk.pause("spread too wide", now + L.cooldown_seconds)
            self.event("reaction", "spread blew out -> new entries paused, resting deep bids left working", now)
        self._wide = wide
        # liquidation-style drop -> no new entries, deep bids keep working
        flash = self.regime.flash < -0.03
        if flash and not self._flash:
            self.risk.pause("fast drop", now + L.cooldown_seconds)
            self.event("reaction", f"price fell {self.regime.flash:.1%} in 10 ticks -> new entries paused, deep bids left working", now)
        self._flash = flash if flash else (self._flash and self.regime.flash < -0.01)
        # regime switches
        if self.regime.state != self._last_regime:
            self.event("regime", f"{self._last_regime} -> {self.regime.state} (size x{self.regime.size_mult:g})", now)
            self._last_regime = self.regime.state
        # outside signals file (bond volatility, AI-stock drop, hack alert feeds write here)
        sig = self.data_dir / "signals.json"
        if self.n % 20 == 0 and sig.exists() and sig.stat().st_mtime != self._signals_mtime:
            self._signals_mtime = sig.stat().st_mtime
            try:
                data = json.loads(sig.read_text())
                self._set_risk_off(bool(data.get("risk_off")), data.get("note", "outside signal"), now)
            except Exception as e:
                self.event("warning", f"signals.json unreadable: {e}", now)

    def _set_risk_off(self, on: bool, why: str, now: float) -> None:
        if on != self.regime.risk_off:
            self.regime.risk_off = on
            self.event("reaction", f"risk-off {'ON' if on else 'off'} ({why}) -> size x{self.regime.size_mult:g}", now)

    def _funding(self, t: Tick) -> None:
        period = int(t.ts // FUNDING_PERIOD)
        if self._fund_period is not None and period != self._fund_period and t.funding_rate is not None:
            pay = self.broker.apply_funding(t.symbol, t.funding_rate, t.mid)
            if pay:
                self.event("funding", f"funding {'received' if pay > 0 else 'paid'} ${abs(pay):.2f} at {t.funding_rate:.4%}", t.ts)
        self._fund_period = period

    # ---- commands from the dashboard ---------------------------------------
    def _commands(self, now: float) -> None:
        while True:
            try:
                cmd, arg = self.commands.get_nowait()
            except queue.Empty:
                return
            if cmd == "stop_all":
                self.emergency("stop button pressed")
            elif cmd == "resume":
                self.risk.resume()
                if self.marks:
                    self.risk.day_start_equity = self.broker.equity(self.marks)
                self.store.set(f"risk:{self.mode}", {**self.risk.to_state(), "v": 3})
                self.event("control", "resumed by you; today's loss budget starts fresh", now)
            elif cmd == "trade_unproven":
                self.store.set("trade_unproven", bool(arg))
                self._overruled = bool(arg)
                self.event("control", "you chose to trade settings that lost in testing" if arg else "standing aside again until something is proven", now)
            elif cmd == "risk_off":
                self._set_risk_off(bool(arg), "set from dashboard", now)
            elif cmd == "reset_paper" and self.mode == "paper":
                self.store.set("paper_state", None)
                for s in self.strategies:
                    self.store.set(f"strategy:{s.name}:{self.mode}:{self.cfg['feed']}", {})
                self.risk.resume()
                self.risk.day_key = ""
                self.store.set(f"risk:{self.mode}", {**self.risk.to_state(), "v": 3})
                self._skip_save = True
                self.stop()
            elif cmd == "restart":
                self.stop()
            elif cmd == "quit":
                self.exit = True
                self.stop()

    def stop(self) -> None:
        self.feed.stopped = True

    # ---- main loop ---------------------------------------------------------
    def run(self, max_ticks: Optional[int] = None) -> None:
        self._skip_save = False
        self.event("start", f"mode={self.mode} feed={self.cfg['feed']} venue={self.cfg['venue']}"
                            + (" BLOCKED: " + "; ".join(self.problems) if self.problems else ""))
        live_broker = self.broker.name == "live"
        if self._handle_fills_later:
            late, self._handle_fills_later = self._handle_fills_later, []
            self.event("start", f"{len(late)} fill(s) happened while the bot was off; accounted for")
            self._handle_fills(late)
        for batch in self.feed.stream():
            now = self.feed.now()
            self._commands(now)
            for t in batch:
                self.ticks[t.symbol], self.marks[t.symbol], self.last_seen[t.symbol] = t, t.mid, now
                self._handle_fills(self.broker.on_tick(t))
                if t.symbol == self.spot:
                    self.regime.update(t.mid)
                if t.symbol == self.perp:
                    self._funding(t)
            self.n += 1
            why = self.gate() if self.n % 5 == 0 or self._gated else ""
            if why and not self._gated:
                for o in list(self.broker.open_orders):
                    if o.side == "buy":
                        self.broker.cancel(o.id)
                self.event("reaction", f"standing aside with real money: {why}. Resting buys cancelled; anything held is still managed. "
                                       "You can overrule this on the dashboard.", now)
            if self._gated and not why and self.n % 5 == 0:
                self.event("control", "no longer standing aside", now)
            if self.n % 5 == 0 or why:
                self._gated = bool(why)
            self._react(now, batch)
            if live_broker and not self.blocked and now - self._last_sync > 2:
                self._last_sync = now
                self._handle_fills(self.broker.sync())
                if getattr(self.broker, "adopted", None):       # coins that were already in the account now count as the bot's
                    for line in self.broker.adopted:
                        self.event("setup", "whole account is the bot's: " + line, now)
                    self.risk.day_start_equity = max(self.risk.day_start_equity, self.broker.equity(self.marks))   # counted as money it started with, not as profit
                    self.broker.adopted = []
            if self.spot in self.marks:
                equity = self.broker.equity(self.marks)
                if not self.blocked and self.risk.on_equity(equity, now):
                    self.emergency(f"daily loss cap of ${self.limits.max_daily_loss_usd:,.0f} hit")
                if not self.blocked and not self.risk.halted:
                    for t in batch:
                        for s in self.strategies:
                            s.on_tick(t)
                            self.flow["evals"] += 1
                if self.n % 5 == 0:
                    self.curve.append((now, round(self.broker.equity(self.marks), 2)))
                    m = self.regime.mix
                    self.regime_hist.append((round(now), m["trend"], m["chop"], m["panic"]))
                if self.n % 30 == 0:
                    self.store.equity(now, self.broker.equity(self.marks), self.marks[self.spot])
                if self.n % 200 == 0:
                    self.save_state()
                if self.feed.live and not self.problems and now - self._last_score > 60:
                    self._last_score = now
                    self.flow["score_checks"] += 1
                    for line in self.score.on_price(self.marks[self.spot], now):
                        self.event("scorekeeper", line, now)
            if self.hub and self.spot in self.marks and time.time() - self._last_hub > 5:
                self._last_hub = time.time()
                if self.feed.live and not self.problems:
                    for line in self.hub.tick(self.marks[self.spot], now, self.store):
                        self.event("forecast", line, now)
                flag = self.data_dir / "restart.flag"
                if flag.exists():                   # dropped by an update: restart on the new files
                    try:
                        flag.unlink()
                    except OSError:
                        pass
                    self.event("control", "update found -> restarting the program")
                    self.reexec = True
                    self.stop()
            self._snapshot(now)
            if self._retry_feed and time.time() - self.started > 30:
                break                               # leave so the program starts fresh and tries the exchange again
            if max_ticks and self.n >= max_ticks:
                break
        if not self._skip_save:
            self.save_state()
        self.event("stop", "engine stopped")

    def save_state(self) -> None:
        self.store.set(f"risk:{self.mode}", {**self.risk.to_state(), "v": 3})
        for s in self.strategies:
            self.store.set(f"strategy:{s.name}:{self.mode}:{self.cfg['feed']}", s.state())
        if self.broker.name == "live" and not self.blocked:
            self.store.set("live_state", self.broker.to_state())
        if self.mode == "paper" and self.broker.name == "paper" and not self.blocked:
            b = self.broker
            self.store.set("paper_state", {
                "feed": self.cfg["feed"], "cash": b.cash, "realized": b.realized, "fees": b.fees,
                "funding": b.funding,
                "positions": {k: {"qty": p.qty, "avg": p.avg} for k, p in b.positions.items() if abs(p.qty) > 1e-12}})

    def _score_view(self) -> dict:
        if not self._score_cache or self.n % 50 == 0:
            self._tilt = self.score.tilt(self.marks[self.spot]) if self.spot in self.marks else 0.0
            self._score_cache = {"score": self.score.table(), "open_calls": self.score.open_calls(),
                                 "score_tilt": self._tilt, "score_live": bool(self.feed.live)}
        return self._score_cache

    def _inner(self, now: float) -> dict:
        """The working parts behind each box on the dashboard's signal graph, with their live values."""
        t = self.ticks.get(self.spot)
        g = self.regime
        return {
            "feed": {"bid": t.bid if t else None, "ask": t.ask if t else None, "last": t.last if t else None,
                     "spread_bps": t.spread_bps if t else None,
                     "age": max(0.0, now - self.last_seen[self.spot]) if self.spot in self.last_seen else None},
            "regime": {"n": len(g.rets), "window": g.window, "recent_vol": getattr(g, "recent_vol", 0.0), "panic_vol": g.panic_vol,
                       "trend_z": g.trend_z, "cand": g._cand, "count": g._count, "need": 45 if g.state == "panic" else 90},
            "risk": {"checks": self.risk.checks, "passed": self.risk.passed, "by_rule": dict(self.risk.by_rule),
                     "last": dict(self.risk.last)},
            "tilt": self._tilt,
        }

    # ---- what the dashboard sees -------------------------------------------
    def _snapshot(self, now: float) -> None:
        b, L = self.broker, self.limits
        equity = b.equity(self.marks) if self.marks else b.cash
        gross = b.gross_exposure(self.marks)
        start = getattr(b, "starting_cash", None)
        if start is None and self.mode == "live" and not self.blocked and equity > 0:      # real money: "since start" is measured from what was really in the account
            start = self.store.get("live_start_equity")
            if not start:
                start = round(equity, 2)
                self.store.set("live_start_equity", start)
                self.event("setup", f"real money: starting value recorded as ${start:,.2f}")
        snap = {
            "ready": True, "mode": self.mode, "feed": self.cfg["feed"], "venue": self.cfg["venue"],
            "ts": now, "tick": self.n, "spot": self.spot, "perp": self.perp,
            "price": self.marks.get(self.spot),
            "funding_rate": self.ticks[self.perp].funding_rate if self.perp in self.ticks else None,
            "equity": equity, "cash": b.cash, "free_cash": self.free_cash(), "budget": L.max_gross_exposure_usd, "day_pnl": self.risk.day_pnl(equity),
            "total_pnl": equity - start if start else None,
            "gate": {"why": self.gate(), "reason": self.gate_reason(), "overruled": self._overruled},
            "realized": b.realized, "fees": b.fees, "funding": b.funding,
            "positions": [{"symbol": s, "qty": p.qty, "avg": p.avg, "mark": self.marks.get(s, p.avg),
                           "value": p.qty * self.marks.get(s, p.avg)}
                          for s, p in b.positions.items() if abs(p.qty) > 1e-12],
            "orders": [{"symbol": o.symbol, "side": o.side, "qty": o.qty, "price": o.price,
                        "strategy": o.strategy, "tag": o.tag} for o in b.open_orders],
            "regime": self.regime.view(),
            "risk": {"halted": self.risk.halted, "halt_reason": self.risk.halt_reason,
                     "paused": now < self.risk.paused_until, "pause_reason": self.risk.pause_reason,
                     "pause_left": max(0.0, self.risk.paused_until - now), "rejections": self.risk.rejections,
                     "gross": gross, "leverage": gross / equity if equity > 0 else 0.0,
                     "day_loss": max(0.0, -self.risk.day_pnl(equity))},
            "limits": {k: getattr(L, k) for k in L.__dataclass_fields__},
            "strategies": [s.view() for s in self.strategies],
            "events": list(self.events)[:60], "fills": list(self.fills)[:30],
            "curve": list(self.curve), "regime_hist": list(self.regime_hist),
            **self._score_view(),
            "health": {"feed_errors": self.feed.errors, "feed_last_error": self.feed.last_error,
                       "uptime": time.time() - self.started, "stale": self._stale},
            "problems": self.problems, "blocked": self.blocked,
            "fc": self.hub.view(self.marks.get(self.spot), now) if self.hub else {"enabled": False, "state": "off"},
            "version": VERSION,
            "flow": {**self.flow, "ticks": self.n, "checks": self.risk.checks, "passed": self.risk.passed,
                     "candles": getattr(self.hub, "fetches", 0), "forecasts": getattr(self.hub, "reads", 0)},
            "inner": self._inner(now),
        }
        with self.lock:
            self.snap = snap
        if time.time() - self._last_status > 5:          # small health file an outside watcher can read
            self._last_status = time.time()
            keep = ("mode", "feed", "venue", "ts", "tick", "price", "equity", "cash", "free_cash", "day_pnl",
                    "total_pnl", "fees", "positions", "regime", "risk", "strategies", "health", "problems", "blocked",
                    "fc", "version", "gate", "orders")
            try:
                tmp = self.data_dir / "status.json.tmp"
                tmp.write_text(json.dumps({**{k: snap[k] for k in keep}, "orders": len(snap["orders"]),
                                           "written": time.time()}))
                tmp.replace(self.data_dir / "status.json")
            except OSError:
                pass
