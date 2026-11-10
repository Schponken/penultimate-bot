"""Paper broker: fake money, real accounting. Charges fees and slippage so results are honest."""
from __future__ import annotations

from typing import Dict, List, Optional

from .models import Fill, Order, Position, Tick, is_perp

EPS = 1e-12


def apply_fill(pos: Position, signed_qty: float, price: float) -> float:
    """Update a position with a fill. Returns realized profit from any part that closed."""
    realized = 0.0
    if abs(pos.qty) < EPS or (pos.qty > 0) == (signed_qty > 0):
        new = pos.qty + signed_qty
        pos.avg = (pos.avg * abs(pos.qty) + price * abs(signed_qty)) / abs(new)
        pos.qty = new
        return 0.0
    closing = min(abs(signed_qty), abs(pos.qty))
    realized = closing * (price - pos.avg) * (1 if pos.qty > 0 else -1)
    leftover = abs(signed_qty) - closing
    pos.qty += signed_qty
    if abs(pos.qty) < EPS:
        pos.qty, pos.avg = 0.0, 0.0
    elif leftover > EPS:            # flipped through zero
        pos.avg = price
    return realized


class PaperBroker:
    name = "paper"

    def __init__(self, starting_cash: float, taker_fee: float, maker_fee: float, slippage_bps: float,
                 perp_taker_fee: float | None = None, perp_maker_fee: float | None = None):
        self.cash = float(starting_cash)
        self.starting_cash = float(starting_cash)
        self.taker_fee = taker_fee
        self.maker_fee = maker_fee
        self.perp_taker_fee = taker_fee if perp_taker_fee is None else perp_taker_fee
        self.perp_maker_fee = maker_fee if perp_maker_fee is None else perp_maker_fee
        self.slip = slippage_bps / 1e4
        self.positions: Dict[str, Position] = {}
        self.open_orders: List[Order] = []
        self.realized = 0.0
        self.fees = 0.0
        self.funding = 0.0

    # ---- views -------------------------------------------------------------
    def position(self, symbol: str) -> Position:
        return self.positions.setdefault(symbol, Position())

    def equity(self, marks: Dict[str, float]) -> float:
        eq = self.cash
        for sym, p in self.positions.items():
            m = marks.get(sym, p.avg)
            eq += p.qty * (m - p.avg) if is_perp(sym) else p.qty * m
        return eq

    def gross_exposure(self, marks: Dict[str, float]) -> float:
        return sum(abs(p.qty) * marks.get(s, p.avg) for s, p in self.positions.items())

    # ---- orders ------------------------------------------------------------
    def submit(self, order: Order, tick: Tick) -> List[Fill]:
        order.ts = tick.ts
        if order.qty <= 0:
            return self._reject(order, "quantity must be positive")
        if order.type == "limit":
            crosses = (order.side == "buy" and order.price >= tick.ask) or (
                order.side == "sell" and order.price <= tick.bid)
            if not crosses:
                if not self._affordable(order, order.price):
                    return self._reject(order, "not enough cash or holdings")
                order.status = "open"
                self.open_orders.append(order)
                return []
        px = tick.ask * (1 + self.slip) if order.side == "buy" else tick.bid * (1 - self.slip)
        return self._fill(order, px, self.perp_taker_fee if is_perp(order.symbol) else self.taker_fee, tick.ts)

    def on_tick(self, tick: Tick) -> List[Fill]:
        """Fill resting limit orders the market has traded through."""
        fills: List[Fill] = []
        for o in [o for o in self.open_orders if o.symbol == tick.symbol]:
            hit = (o.side == "buy" and tick.ask < o.price) or (o.side == "sell" and tick.bid > o.price)
            if hit:
                self.open_orders.remove(o)
                fills += self._fill(o, o.price, self.perp_maker_fee if is_perp(o.symbol) else self.maker_fee, tick.ts)
        return fills

    def cancel_all(self, symbol: Optional[str] = None, strategy: Optional[str] = None) -> int:
        keep, n = [], 0
        for o in self.open_orders:
            if (symbol is None or o.symbol == symbol) and (strategy is None or o.strategy == strategy):
                o.status = "cancelled"
                n += 1
            else:
                keep.append(o)
        self.open_orders = keep
        return n

    def cancel(self, order_id: int) -> bool:
        for o in self.open_orders:
            if o.id == order_id:
                o.status = "cancelled"
                self.open_orders.remove(o)
                return True
        return False

    def flatten(self, ticks: Dict[str, Tick], strategy: str = "risk") -> List[Fill]:
        """Cancel everything and close every position at market."""
        self.cancel_all()
        fills: List[Fill] = []
        for sym, p in list(self.positions.items()):
            if abs(p.qty) > EPS and sym in ticks:
                side = "sell" if p.qty > 0 else "buy"
                fills += self.submit(Order(sym, side, abs(p.qty), strategy=strategy, tag="flatten"), ticks[sym])
        return fills

    def apply_funding(self, symbol: str, rate: float, mark: float) -> float:
        """Longs pay shorts when the rate is positive. Returns cash received (negative = paid)."""
        p = self.positions.get(symbol)
        if not p or abs(p.qty) < EPS:
            return 0.0
        pay = -p.qty * mark * rate
        self.cash += pay
        self.funding += pay
        return pay

    def sync(self) -> None:      # same interface as the live broker
        return None

    # ---- internals ---------------------------------------------------------
    def _affordable(self, order: Order, px: float) -> bool:
        if is_perp(order.symbol):
            return True                      # leverage is the risk engine's job
        if order.side == "buy":
            reserved = sum(o.qty * o.price for o in self.open_orders
                           if o.side == "buy" and not is_perp(o.symbol))
            return self.cash - reserved >= order.qty * px
        reserved = sum(o.qty for o in self.open_orders if o.side == "sell" and o.symbol == order.symbol)
        return self.position(order.symbol).qty - reserved >= order.qty - 1e-9

    def _reject(self, order: Order, reason: str) -> List[Fill]:
        order.status, order.reason = "rejected", reason
        return []

    def _fill(self, order: Order, px: float, fee_rate: float, ts: float) -> List[Fill]:
        notional = order.qty * px
        fee = notional * fee_rate
        pos = self.position(order.symbol)
        if not is_perp(order.symbol):
            if order.side == "buy" and self.cash < notional + fee:
                return self._reject(order, "not enough cash")
            if order.side == "sell" and pos.qty < order.qty - 1e-9:
                return self._reject(order, "not enough holdings")
        realized = apply_fill(pos, order.signed_qty, px)
        if is_perp(order.symbol):
            self.cash += realized - fee
        else:
            self.cash -= order.signed_qty * px + fee
        self.realized += realized
        self.fees += fee
        order.status = "filled"
        return [Fill(order.id, ts, order.symbol, order.side, order.qty, px, fee, order.strategy, order.tag)]
