"""Risk engine. Every order passes through here. Cutting risk is always allowed;
adding risk must fit inside limits.yaml. A halt stays in force until a person resumes it."""
from __future__ import annotations

import time
from typing import Dict, Tuple

from .config import Limits
from .models import Order, Tick

EPS = 1e-12


class RiskEngine:
    def __init__(self, limits: Limits):
        self.limits = limits
        self.halted = False
        self.halt_reason = ""
        self.paused_until = 0.0
        self.pause_reason = ""
        self.day_key = ""
        self.day_start_equity = 0.0
        self.rejections = 0
        self.checks = 0                 # every order that asked
        self.passed = 0
        self.by_rule: Dict[str, int] = {}   # refusals, by which rule said no
        self.last: dict = {}            # the most recent decision, for the dashboard

    # ---- pre-trade check ---------------------------------------------------
    def check(self, order: Order, tick: Tick, broker, marks: Dict[str, float]) -> Tuple[bool, str]:
        L = self.limits
        px = order.price if (order.type == "limit" and order.price) else tick.mid
        notional = order.qty * px
        pos = broker.position(order.symbol).qty
        new = pos + order.signed_qty
        reducing = abs(new) < abs(pos) - EPS and (new == 0 or (new > 0) == (pos > 0))
        self.checks += 1
        self._asking = {"strategy": order.strategy, "side": order.side, "usd": round(notional, 2), "ts": tick.ts}
        if reducing:
            return self._yes("reduces risk")
        if self.halted:
            return self._no(f"halted: {self.halt_reason}", "halted")
        if tick.ts < self.paused_until:
            return self._no(f"paused: {self.pause_reason}", "paused")
        if notional > L.max_order_usd + 1e-6:
            return self._no(f"order ${notional:,.0f} over the ${L.max_order_usd:,.0f} order cap", "order")
        pending = [o for o in broker.open_orders]
        same = sum(o.signed_qty for o in pending if o.symbol == order.symbol and o.side == order.side)
        worst = abs(pos + same + order.signed_qty) * px
        if worst > L.max_position_usd + 1e-6:
            return self._no(f"position would reach ${worst:,.0f}, cap is ${L.max_position_usd:,.0f}", "position")
        def adds_risk(o) -> bool:           # resting take-profits don't count against the cap
            q = broker.position(o.symbol).qty
            return abs(q) < EPS or (q > 0) == (o.signed_qty > 0)
        gross = broker.gross_exposure(marks) + notional + sum(
            o.qty * (o.price or marks.get(o.symbol, 0.0)) for o in pending if adds_risk(o))
        if gross > L.max_gross_exposure_usd + 1e-6:
            return self._no(f"total exposure would reach ${gross:,.0f}, cap is ${L.max_gross_exposure_usd:,.0f}", "exposure")
        equity = broker.equity(marks)
        if equity <= 0 or gross / equity > L.max_leverage + 1e-9:
            return self._no(f"leverage would exceed {L.max_leverage:g}x", "leverage")
        return self._yes("ok")

    def _no(self, why: str, rule: str = "other") -> Tuple[bool, str]:
        self.rejections += 1
        self.by_rule[rule] = self.by_rule.get(rule, 0) + 1
        self.last = {**getattr(self, "_asking", {}), "ok": False, "why": why, "rule": rule}
        return False, why

    def _yes(self, why: str) -> Tuple[bool, str]:
        self.passed += 1
        self.last = {**getattr(self, "_asking", {}), "ok": True, "why": why, "rule": ""}
        return True, why

    # ---- account-level watch -----------------------------------------------
    def on_equity(self, equity: float, ts: float) -> bool:
        """Track the day's loss. Returns True the moment the daily loss cap is hit."""
        key = time.strftime("%Y-%m-%d", time.gmtime(ts))
        if key != self.day_key:
            self.day_key, self.day_start_equity = key, equity
        return (not self.halted) and (self.day_start_equity - equity) >= self.limits.max_daily_loss_usd

    def day_pnl(self, equity: float) -> float:
        return equity - self.day_start_equity if self.day_key else 0.0

    # ---- controls ----------------------------------------------------------
    def halt(self, reason: str) -> None:
        self.halted, self.halt_reason = True, reason

    def resume(self) -> None:
        self.halted, self.halt_reason = False, ""
        self.paused_until, self.pause_reason = 0.0, ""

    def pause(self, reason: str, until_ts: float) -> None:
        if until_ts > self.paused_until:
            self.paused_until, self.pause_reason = until_ts, reason

    def to_state(self) -> dict:
        return {"halted": self.halted, "halt_reason": self.halt_reason,
                "day_key": self.day_key, "day_start_equity": self.day_start_equity}

    def load_state(self, s: dict) -> None:
        self.halted = bool(s.get("halted", False))
        self.halt_reason = s.get("halt_reason", "")
        self.day_key = s.get("day_key", "")
        self.day_start_equity = float(s.get("day_start_equity", 0.0))
