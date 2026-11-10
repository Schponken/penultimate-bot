"""Strategies. Each one only proposes orders; the risk engine decides what goes through.

DeepBids      - resting buy orders far below the market that catch forced-selling wicks,
                then sell the bounce.
FundingCarry  - when leveraged longs are paying a high funding rate, buy spot and short the
                perpetual future in equal size. Price moves cancel out; the bot collects funding.
                It skips the trade when fees would take too long to earn back.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .models import Fill, Order, Tick


class Strategy:
    name = "base"

    def __init__(self, params: dict, eng):
        self.p = params
        self.eng = eng              # gives place(), broker, regime, ticks, fees
        self.note = ""

    def on_tick(self, tick: Tick) -> None: ...
    def on_fill(self, fill: Fill) -> None: ...
    def reset(self) -> None:
        """Called after the risk engine sells everything, so no stale state survives."""
    def state(self) -> dict:
        return {}
    def load_state(self, s: dict) -> None: ...
    def view(self) -> dict:
        return {"name": self.name, "note": self.note}


class DeepBids(Strategy):
    name = "deep_bids"

    def __init__(self, params: dict, eng):
        super().__init__(params, eng)
        self.anchor: Optional[float] = None
        self.anchor_ts = 0.0
        self.lots: Dict[int, dict] = {}     # buy order id -> {qty, entry, ts, tp_id}
        self.wins = 0
        self.losses = 0

    def _ladder(self) -> List[Order]:
        return [o for o in self.eng.broker.open_orders
                if o.strategy == self.name and o.tag.startswith("ladder")]

    def on_tick(self, tick: Tick) -> None:
        if tick.symbol != self.eng.spot:
            return
        mid = tick.mid
        # 0. every lot must have a live take-profit order (covers restarts and cancelled orders)
        live_ids = {o.id for o in self.eng.broker.open_orders}
        by_tag = {o.tag: o.id for o in self.eng.broker.open_orders if o.strategy == self.name}
        for oid, lot in self.lots.items():
            if lot.get("tp_id") not in live_ids and f"tp-{oid}" in by_tag:
                lot["tp_id"] = by_tag[f"tp-{oid}"]      # still resting on the exchange from before a restart
            if lot.get("tp_id") not in live_ids:
                tp = self.eng.place(Order(tick.symbol, "sell", lot["qty"], "limit",
                                          lot["entry"] * (1 + self.p["take_profit"]), self.name, f"tp-{oid}"))
                lot["tp_id"] = tp.id if tp.status == "open" else None
        # 1. exits: stop-loss and time stop on anything we hold
        for oid, lot in list(self.lots.items()):
            too_old = tick.ts - lot["ts"] > self.p["max_hold_hours"] * 3600
            stopped = tick.bid <= lot.get("stop", lot["entry"] * (1 - self.p["stop_loss"]))
            if too_old or stopped:
                if lot.get("tp_id"):
                    self.eng.broker.cancel(lot["tp_id"])
                o = self.eng.place(Order(tick.symbol, "sell", lot["qty"], strategy=self.name,
                                         tag="stop" if stopped else "timeout"))
                if o.status in ("filled", "open"):
                    self.lots.pop(oid, None)
                    self.losses += 1
        # 2. keep the ladder of deep bids under the market.
        #    Follow the price UP promptly. On the way DOWN leave the bids where they are,
        #    so a fast drop runs into them; only re-centre lower after the price has settled.
        ladder = self._ladder()
        if self.anchor is None or not ladder:
            move = True
        else:
            drift = mid / self.anchor - 1
            settled = tick.ts - self.anchor_ts > self.p.get("settle_hours", 6) * 3600
            move = drift > self.p["recenter"] or (drift < -self.p["recenter"] and settled and not self.lots)
        if not move:
            return
        free = len(self.p["levels"]) - len(self.lots)
        if free <= 0:
            self.note = "holding the maximum number of lots"
            return
        if not self.eng.can_add_risk():
            self.note = "waiting: new entries are paused"
            return
        mult = self.eng.size_mult(use_regime=False)     # deep bids exist for panics, so no regime cut
        usd = min(self.p["order_usd"] * mult, self.eng.limits.max_order_usd)
        cash = self.eng.free_cash()
        if cash < usd * 1.01:
            self.note = f"waiting for funds: ${cash:,.2f} available, needs ${usd:,.0f} per order"
            return
        free = min(free, int(cash // (usd * 1.01)))
        # stay inside the position and total-exposure caps without asking for orders that would be refused
        b, L = self.eng.broker, self.eng.limits
        mine = {o.id for o in ladder}
        other_buys = sum(o.qty * (o.price or mid) for o in b.open_orders
                         if o.side == "buy" and o.id not in mine and o.symbol == tick.symbol)
        room = min(L.max_position_usd - abs(b.position(tick.symbol).qty) * mid - other_buys,
                   L.max_gross_exposure_usd - b.gross_exposure(self.eng.marks) - other_buys)
        free = min(free, int(room // (usd * 1.02)))
        if free <= 0:
            if ladder:
                return                                  # keep what is already resting
            self.note = "at the position limit; waiting for room"
            return
        for o in ladder:
            self.eng.broker.cancel(o.id)
        placed = 0
        for i, depth in enumerate(self.p["levels"][:free]):
            price = mid * (1 - depth)
            o = self.eng.place(Order(tick.symbol, "buy", usd / price, "limit", price, self.name, f"ladder-{i}"))
            placed += o.status == "open"
        self.anchor, self.anchor_ts = mid, tick.ts
        self.note = (f"{placed} resting bids, {self.p['levels'][0]:.0%} to {self.p['levels'][free - 1]:.0%} below"
                     if placed else "bids refused by the risk limits")

    def on_fill(self, fill: Fill) -> None:
        if fill.tag.startswith("ladder"):
            prev = self.lots.pop(fill.order_id, None)
            if prev:                                    # the same order filling in pieces: one lot, one exit
                if prev.get("tp_id"):
                    self.eng.broker.cancel(prev["tp_id"])
                fill = Fill(fill.order_id, prev["ts"], fill.symbol, fill.side, fill.qty + prev["qty"],
                            fill.price, 0.0, fill.strategy, fill.tag)
            tp = self.eng.place(Order(fill.symbol, "sell", fill.qty, "limit",
                                      fill.price * (1 + self.p["take_profit"]), self.name, f"tp-{fill.order_id}"))
            # One stop for the whole ladder, set below its deepest rung. A stop inside the
            # ladder would sell the first fills at the exact moment the deeper ones are bought.
            stop = (self.anchor or fill.price) * (1 - self.p["stop_loss"])
            self.lots[fill.order_id] = {"qty": fill.qty, "entry": fill.price, "ts": fill.ts, "stop": stop,
                                        "tp_id": tp.id if tp.status == "open" else None}
        elif fill.tag.startswith("tp-"):
            lot = self.lots.get(int(fill.tag[3:]))
            if lot and fill.qty < lot["qty"] * 0.999:   # exit filling in pieces
                lot["qty"] -= fill.qty
            else:
                self.lots.pop(int(fill.tag[3:]), None)
                self.wins += 1

    def reset(self) -> None:
        self.lots.clear()
        self.anchor = None
        self.note = "reset after emergency exit"

    def state(self) -> dict:
        return {"lots": {str(k): v for k, v in self.lots.items()}, "wins": self.wins, "losses": self.losses,
                "anchor": self.anchor, "anchor_ts": self.anchor_ts}

    def load_state(self, s: dict) -> None:
        self.lots = {int(k): {**v, "tp_id": None} for k, v in s.get("lots", {}).items()}
        self.wins, self.losses = s.get("wins", 0), s.get("losses", 0)
        self.anchor, self.anchor_ts = s.get("anchor"), s.get("anchor_ts", 0.0)

    def view(self) -> dict:
        mid = self.eng.marks.get(self.eng.spot)
        return {"name": self.name, "note": self.note, "lots": len(self.lots),
                "wins": self.wins, "losses": self.losses,
                "inner": {"anchor": self.anchor, "drift": (mid / self.anchor - 1) if (mid and self.anchor) else None,
                          "levels": list(self.p["levels"]), "recenter": self.p["recenter"], "take_profit": self.p["take_profit"],
                          "stop_loss": self.p["stop_loss"], "max_hold_hours": self.p["max_hold_hours"],
                          "settle_hours": self.p.get("settle_hours", 6), "order_usd": self.p["order_usd"],
                          "resting": len(self._ladder()),
                          "held": [{"entry": l["entry"], "stop": l.get("stop"), "qty": l["qty"]} for l in self.lots.values()]}}


class FundingCarry(Strategy):
    name = "funding_carry"

    def __init__(self, params: dict, eng):
        super().__init__(params, eng)
        self.qty = 0.0
        self.opened = 0
        self.skipped = 0
        self._was_skipping = False
        self._retry_after = 0.0

    def on_tick(self, tick: Tick) -> None:
        if tick.symbol != self.eng.perp or tick.funding_rate is None:
            return
        spot = self.eng.ticks.get(self.eng.spot)
        if spot is None:
            return
        rate = tick.funding_rate
        if self.qty == 0:
            if rate < self.p["enter_rate"] or self.eng.regime.state in ("panic", "warming"):
                self._was_skipping = False
                return
            if not self.eng.can_add_risk():
                return
            notional = min(self.p["notional_usd"] * self.eng.size_mult(), self.eng.limits.max_order_usd)
            # fees: open and close, on both legs
            fees = notional * 2 * (self.eng.taker_fee + self.eng.perp_taker_fee)
            breakeven = fees / (notional * rate)
            if breakeven > self.p["max_breakeven_periods"]:
                if not self._was_skipping:
                    self.skipped += 1
                self._was_skipping = True
                self.note = (f"skipped: at {rate:.3%} per 8h, fees need {breakeven:.0f} funding periods "
                             f"to earn back (limit {self.p['max_breakeven_periods']})")
                return
            self._was_skipping = False
            if tick.ts < self._retry_after:
                return
            qty = notional * 0.98 / max(spot.ask, tick.ask)     # headroom so neither leg trips a cap
            leg_a = Order(self.eng.spot, "buy", qty, strategy=self.name, tag="carry-spot")
            leg_b = Order(self.eng.perp, "sell", qty, strategy=self.name, tag="carry-perp")
            for leg in (leg_a, leg_b):                          # ask first, so no half-open pairs
                ok, why = self.eng.would_pass(leg)
                if not ok:
                    self.note = f"not opened: {why}"
                    self._retry_after = tick.ts + 3600
                    return
            a = self.eng.place(leg_a)
            if a.status not in ("filled", "open"):
                self.note = f"spot leg refused: {a.reason}"
                self._retry_after = tick.ts + 3600
                return
            b = self.eng.place(leg_b)
            if b.status not in ("filled", "open"):              # never hold half a hedge
                self.eng.place(Order(self.eng.spot, "sell", qty, strategy=self.name, tag="carry-unwind"))
                self.note = f"hedge leg refused, unwound: {b.reason}"
                self._retry_after = tick.ts + 3600
                return
            self.qty = qty
            self.opened += 1
            self.note = f"hedged pair open, collecting {rate:.3%} per 8h"
        elif rate <= self.p["exit_rate"]:
            self.eng.place(Order(self.eng.perp, "buy", self.qty, strategy=self.name, tag="carry-close"))
            self.eng.place(Order(self.eng.spot, "sell", self.qty, strategy=self.name, tag="carry-close"))
            self.qty = 0.0
            self.note = "funding faded, pair closed"

    def reset(self) -> None:
        self.qty = 0.0
        self.note = "reset after emergency exit"

    def state(self) -> dict:
        return {"qty": self.qty, "opened": self.opened, "skipped": self.skipped}

    def load_state(self, s: dict) -> None:
        self.qty, self.opened, self.skipped = s.get("qty", 0.0), s.get("opened", 0), s.get("skipped", 0)

    def view(self) -> dict:
        return {"name": self.name, "note": self.note, "open": self.qty > 0,
                "opened": self.opened, "skipped": self.skipped,
                "inner": {"enter_rate": self.p.get("enter_rate"), "exit_rate": self.p.get("exit_rate"),
                          "max_breakeven_periods": self.p.get("max_breakeven_periods"), "notional_usd": self.p.get("notional_usd")}}


class Hold(Strategy):
    """Hold the market: keep a chosen share of the account in the coin and otherwise leave it alone.

    No prediction and no edge: it earns whatever the market does, minus one small fee to get in. It buys with
    resting orders (the cheaper fee), in one go or in equal weekly parts, and afterwards trades only if the
    holding has drifted far from the chosen share, at most once a month. Off until you choose a share on the
    dashboard. It still lives inside the hard limits in limits.yaml."""
    name = "hold"
    WEEK = 7 * 86400
    BAND = 0.10                 # rebalance only when the holding is this far (as a share of the account) from the target
    REBALANCE_EVERY = 30 * 86400
    REPRICE_S = 180

    def __init__(self, params: dict, eng):
        super().__init__(params, eng)
        self.pct, self.weeks, self.since = 0.0, 1, 0.0
        self.built, self.last_reb, self.adjust = False, 0.0, False
        self._last = 0.0
        self.capped = False
        self._v: dict = {}

    def set(self, pct: float, weeks: int, now: float) -> None:
        """Your choice from the dashboard."""
        pct, weeks = max(0.0, min(100.0, float(pct))), max(1, int(weeks))
        if self.pct <= 0 < pct:
            self.since, self.built = now, False                    # starting from nothing: the weekly parts begin now
        self.adjust = True                                          # you asked for it, so move toward it now (no monthly wait)
        self.pct, self.weeks = pct, weeks
        for o in self._mine():
            self.eng.broker.cancel(o.id)

    def _mine(self) -> List[Order]:
        return [o for o in self.eng.broker.open_orders if o.strategy == self.name]

    def on_tick(self, tick: Tick) -> None:
        eng = self.eng
        if tick.symbol != eng.spot or tick.ts - self._last < 5:
            return
        self._last = tick.ts
        b, mid = eng.broker, tick.mid
        equity = b.equity(eng.marks)
        val = b.position(tick.symbol).qty * mid
        want = equity * self.pct / 100.0
        target = min(want, eng.limits.max_position_usd * 0.98)     # never more than the hard position limit allows
        self.capped = target < want - 1.0
        parts = min(self.weeks, int((tick.ts - self.since) // self.WEEK) + 1) if self.pct > 0 else 0
        allowed = target if self.built else target * parts / max(1, self.weeks)
        self._v = {"value": val, "target": target, "allowed": allowed, "parts": parts, "equity": equity,
                   "next_part": self.since + parts * self.WEEK if self.pct > 0 and not self.built and parts < self.weeks else None}
        mine = self._mine()
        for o in mine:                                              # a resting order the market has moved away from is re-priced
            if tick.ts - o.ts > self.REPRICE_S:
                b.cancel(o.id)
        if mine:
            return
        small = max(5.0, 0.02 * equity)
        if self.pct <= 0:
            if val > small and self.adjust:                         # you turned it off: sell what it holds
                self._sell(tick, val, val)
            else:
                self.adjust = False
                self.note = "off"
            return
        gap = allowed - val
        if not self.built and val >= target - small:
            self.built, self.last_reb, self.adjust = True, tick.ts, False
        if not self.built or self.adjust:
            if gap > small:
                return self._buy(tick, gap)
            if self.adjust and -gap > small:                        # you lowered the share: sell down to it
                return self._sell(tick, -gap, val)
            self.adjust = False
            self.note = (f"holding ${val:,.2f}, part {parts} of {self.weeks} bought" if not self.built
                         else f"holding ${val:,.2f} ({val / equity:.0%} of the account)")
            return
        drift = (val - target) / equity if equity > 0 else 0.0
        if abs(drift) > self.BAND and tick.ts - self.last_reb > self.REBALANCE_EVERY:
            self.last_reb = tick.ts
            self.adjust = True                                      # one move back to the target, then quiet again
            return
        self.note = f"holding ${val:,.2f} ({val / equity:.0%} of the account, target {self.pct:.0f}%)"

    def _buy(self, tick: Tick, usd: float) -> None:
        eng = self.eng
        if not eng.can_add_risk(self.name):
            self.note = "waiting: new buys are paused"
            return
        usd = min(usd, eng.limits.max_order_usd * 0.98, eng.free_cash() / 1.012)
        if usd < 2.0:
            self.note = "waiting for cash"
            return
        o = eng.place(Order(tick.symbol, "buy", usd / tick.bid, "limit", tick.bid, self.name, "hold-buy"))
        self.note = f"buying ${usd:,.2f} with a resting order" if o.status == "open" else f"buy refused: {o.reason}"

    def _sell(self, tick: Tick, usd: float, val: float) -> None:
        eng = self.eng
        usd = min(usd, eng.limits.max_order_usd * 0.98)
        qty = eng.broker.position(tick.symbol).qty * min(1.0, usd / val) if val > 0 else 0.0
        if qty <= 0:
            return
        o = eng.place(Order(tick.symbol, "sell", qty, "limit", tick.ask, self.name, "hold-sell"))
        self.note = f"selling ${usd:,.2f} with a resting order" if o.status == "open" else f"sell refused: {o.reason}"

    def reset(self) -> None:
        self.pct, self.built, self.adjust = 0.0, False, False      # an emergency stop sold everything: holding is off until you choose again
        self.note = "off: stopped by the emergency exit"

    def state(self) -> dict:
        return {"pct": self.pct, "weeks": self.weeks, "since": self.since, "built": self.built, "last_reb": self.last_reb, "adjust": self.adjust}

    def load_state(self, s: dict) -> None:
        self.pct, self.weeks, self.since = float(s.get("pct", 0.0)), int(s.get("weeks", 1)), float(s.get("since", 0.0))
        self.built, self.last_reb, self.adjust = bool(s.get("built")), float(s.get("last_reb", 0.0)), bool(s.get("adjust"))

    def view(self) -> dict:
        v = self._v
        return {"name": self.name, "note": self.note, "pct": self.pct, "weeks": self.weeks, "since": self.since, "built": self.built,
                "value": round(v.get("value", 0.0), 2), "target": round(v.get("target", 0.0), 2), "parts": v.get("parts", 0),
                "next_part": v.get("next_part"), "last_reb": self.last_reb, "band": self.BAND, "capped": self.capped,
                "inner": {"pct": self.pct, "weeks": self.weeks, "value": round(v.get("value", 0.0), 2), "target": round(v.get("target", 0.0), 2),
                          "band": self.BAND, "built": self.built}}


REGISTRY = {"deep_bids": DeepBids, "funding_carry": FundingCarry}
