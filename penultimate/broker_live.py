"""Live broker: real orders on your own exchange account, through the ccxt library.

What it can do: place and cancel orders, read balances and positions, and move money
between your own wallets on the same exchange (capped by limits.yaml).
What it cannot do: withdraw. There is no withdrawal code in this project at all.

IMPORTANT: this file was written and unit-tested against a stand-in exchange, but has not
been run against a real one (the build environment has no exchange access). The engine
runs a read-only self-check before going live and refuses to start if anything fails.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from .broker_paper import apply_fill
from .models import Fill, Order, Position, Tick, is_perp

KEYS_FILE = Path(__file__).resolve().parent.parent / "data" / "keys.json"


def load_keys() -> Optional[dict]:
    if KEYS_FILE.exists():
        return json.loads(KEYS_FILE.read_text())
    return None


def normalize_keys(api_key: str, secret: str = "") -> tuple:
    """Accepts what people actually paste: the whole key file Coinbase downloads (JSON with
    "name"/"id" and "privateKey"), a private key whose line breaks were flattened to \\n or lost,
    or a plain key and secret."""
    api_key, secret = (api_key or "").strip(), (secret or "").strip()
    for blob in (api_key, secret):
        if blob.startswith("{"):
            try:
                d = json.loads(blob)
                api_key = str(d.get("name") or d.get("id") or d.get("apiKey") or "").strip()
                secret = str(d.get("privateKey") or d.get("secret") or "").strip()
                break
            except ValueError:
                pass
    secret = secret.replace("\\n", "\n").strip()
    m = re.match(r"^(-----BEGIN [A-Z ]+-----)\s*(.+?)\s*(-----END [A-Z ]+-----)$", secret, re.S)
    if m:                                   # rebuild proper PEM line breaks
        body = re.sub(r"\s+", "", m.group(2))
        lines = [body[i:i + 64] for i in range(0, len(body), 64)]
        secret = "\n".join([m.group(1)] + lines + [m.group(3)]) + "\n"
    return api_key, secret


def save_keys(api_key: str, secret: str, password: str = "") -> None:
    api_key, secret = normalize_keys(api_key, secret)
    if not api_key or not secret:
        raise ValueError("could not find both a key name and a secret in what was pasted")
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(KEYS_FILE.parent, 0o700)                  # the data folder is yours alone
    except OSError:
        pass
    tmp = KEYS_FILE.with_name(KEYS_FILE.name + ".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # created readable by you only, never wider
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"apiKey": api_key, "secret": secret, "password": password}))
    os.chmod(tmp, 0o600)
    os.replace(tmp, KEYS_FILE)


def make_exchange(venue: str, keys: Optional[dict] = None):
    import ccxt                            # imported lazily so paper mode needs nothing extra
    params = {"enableRateLimit": True}
    if keys:
        params.update({k: v for k, v in keys.items() if v})
    return getattr(ccxt, venue)(params)


class LiveBroker:
    name = "live"

    def __init__(self, exchange, quote: str = "USD", max_transfer_usd: float = 0.0):
        self.ex = exchange
        self.quote = quote
        self.max_transfer_usd = max_transfer_usd
        self.cash = 0.0
        self.free_cash = 0.0
        self.positions: Dict[str, Position] = {}
        self.open_orders: List[Order] = []
        self.realized = 0.0
        self.fees = 0.0
        self.funding = 0.0
        self._seen_trades: set = set()
        self._known: Dict[str, Order] = {}      # every order we sent, by exchange id
        self.own_symbols: List[str] = []        # spot markets where the WHOLE exchange balance is the bot's (set by the engine)
        self.marks: Dict[str, float] = {}       # latest prices, shared with the engine
        self.adopted: List[str] = []            # plain-language notes for the event log
        self._took_on: set = set()

    # ---- start-up check (read-only) ----------------------------------------
    def self_check(self, symbols: List[str]) -> List[str]:
        """Returns a list of problems. Empty list = safe to start."""
        problems: List[str] = []
        try:
            markets = self.ex.load_markets()
        except Exception as e:
            return [f"Could not reach the exchange: {e}"]
        for s in symbols:
            if s not in markets:
                problems.append(f"{s} is not offered on this exchange account")
        try:
            self.ex.fetch_balance()
        except Exception as e:
            problems.append(f"API keys were rejected: {e}")
        return problems

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
        try:
            qty = float(self.ex.amount_to_precision(order.symbol, order.qty))
            price = float(self.ex.price_to_precision(order.symbol, order.price)) if order.type == "limit" else None
            if order.type == "market" and order.side == "buy" and not is_perp(order.symbol):
                price = float(tick.ask)         # spot market buys are sized in dollars: qty x this price
            res = self.ex.create_order(order.symbol, order.type, order.side, qty, price)
        except Exception as e:
            order.status, order.reason = "rejected", f"exchange refused: {e}"
            return []
        order.venue_id = str(res.get("id", ""))
        order.status = "open"
        self.open_orders.append(order)
        if order.venue_id:
            self._known[order.venue_id] = order
            while len(self._known) > 400:
                self._known.pop(next(iter(self._known)))
        return []                           # fills arrive through sync()

    def on_tick(self, tick: Tick) -> List[Fill]:
        return []

    def cancel_all(self, symbol: Optional[str] = None, strategy: Optional[str] = None) -> int:
        n, keep = 0, []
        for o in self.open_orders:
            if (symbol is None or o.symbol == symbol) and (strategy is None or o.strategy == strategy):
                try:
                    self.ex.cancel_order(o.venue_id, o.symbol)
                except Exception:
                    pass                    # already filled or gone; sync() settles it
                o.status = "cancelled"
                n += 1
            else:
                keep.append(o)
        self.open_orders = keep
        return n

    def cancel(self, order_id: int) -> bool:
        for o in self.open_orders:
            if o.id == order_id:
                try:
                    self.ex.cancel_order(o.venue_id, o.symbol)
                except Exception:
                    pass
                o.status = "cancelled"
                self.open_orders.remove(o)
                return True
        return False

    def flatten(self, ticks: Dict[str, Tick], strategy: str = "risk") -> List[Fill]:
        self.cancel_all()
        self.sync()
        for sym, p in list(self.positions.items()):
            if abs(p.qty) > 1e-12 and sym in ticks:
                side = "sell" if p.qty > 0 else "buy"
                self.submit(Order(sym, side, abs(p.qty), strategy=strategy, tag="flatten"), ticks[sym])
        return self.sync()

    def apply_funding(self, symbol: str, rate: float, mark: float) -> float:
        return 0.0                          # the exchange settles funding itself

    # ---- reconcile with the exchange (the exchange is always right) ---------
    def sync(self) -> List[Fill]:
        fills: List[Fill] = []
        by_venue = self._known               # includes orders that just left the book
        symbols = {o.symbol for o in self.open_orders} | set(self.positions) | {o.symbol for o in list(by_venue.values())[-20:]}
        for sym in symbols:
            try:
                trades = self.ex.fetch_my_trades(sym, limit=50)
            except Exception:
                continue
            for t in trades:
                tid = str(t.get("id"))
                if tid in self._seen_trades:
                    continue
                self._seen_trades.add(tid)
                o = by_venue.get(str(t.get("order")))
                if o is None:
                    continue                # not one of ours
                fee = float((t.get("fee") or {}).get("cost") or 0.0)
                f = Fill(o.id, t["timestamp"] / 1000.0, sym, t["side"], float(t["amount"]),
                         float(t["price"]), fee, o.strategy, o.tag)
                self.realized += apply_fill(self.position(sym), f.signed_qty, f.price)
                self.fees += fee
                fills.append(f)
        try:
            still = {str(x["id"]) for s in symbols for x in self.ex.fetch_open_orders(s)}
            for o in list(self.open_orders):
                if o.venue_id and o.venue_id not in still:
                    o.status = "filled" if any(f.order_id == o.id for f in fills) else o.status
                    self.open_orders.remove(o)
            bal = self.ex.fetch_balance()
            self.cash = float((bal.get("total") or {}).get(self.quote) or 0.0)
            self.free_cash = float((bal.get("free") or {}).get(self.quote) or 0.0)
            # The whole account is the bot's: the coin balance the exchange reports IS the position, however it got there.
            # The balance is the single truth for how much is held, so a trade that shows up late can never be counted twice.
            for sym in self.own_symbols:
                if is_perp(sym):
                    continue
                have, p = float((bal.get("total") or {}).get(sym.split("/")[0]) or 0.0), self.position(sym)
                mark = self.marks.get(sym)
                if not mark:                                    # no price yet (start-up): still never hold a number the exchange does not show
                    p.qty = min(p.qty, have) if have > 0 else 0.0
                    continue
                if have * mark < 1.0:
                    have = 0.0                                  # dust
                if have > p.qty and (have - p.qty) * mark >= 1.0 and sym not in self._took_on and not any(f.symbol == sym for f in fills):
                    self._took_on.add(sym)
                    self.adopted.append(f"took on {have - p.qty:.6f} {sym.split('/')[0]} (${(have - p.qty) * mark:,.2f}) that was already in the account")
                    p.avg = (p.qty * p.avg + (have - p.qty) * mark) / have
                elif have > 0 and p.qty <= 0:
                    p.avg = mark
                p.qty = have
        except Exception:
            pass                            # keep last known state; stale-feed logic covers outages
        return fills

    # ---- remember everything across restarts --------------------------------
    def to_state(self) -> dict:
        def od(o):
            return {"id": o.id, "venue_id": o.venue_id, "symbol": o.symbol, "side": o.side, "qty": o.qty,
                    "type": o.type, "price": o.price, "strategy": o.strategy, "tag": o.tag, "ts": o.ts}
        open_ids = {o.id for o in self.open_orders}
        return {"positions": {k: {"qty": p.qty, "avg": p.avg} for k, p in self.positions.items() if abs(p.qty) > 1e-12},
                "realized": self.realized, "fees": self.fees, "seen": sorted(self._seen_trades)[-2000:],
                "known": [dict(od(o), open=o.id in open_ids) for o in self._known.values()]}

    def load_state(self, s: dict) -> None:
        self.positions = {k: Position(**v) for k, v in s.get("positions", {}).items()}
        self.realized, self.fees = s.get("realized", 0.0), s.get("fees", 0.0)
        self._seen_trades = set(s.get("seen", []))
        for d in s.get("known", []):
            is_open = d.pop("open", False)
            o = Order(d["symbol"], d["side"], d["qty"], d["type"], d["price"], d["strategy"], d["tag"],
                      id=d["id"], ts=d["ts"], status="open" if is_open else "filled", venue_id=d["venue_id"])
            self._known[o.venue_id] = o
            if is_open:
                self.open_orders.append(o)

    # ---- money movement between your own wallets ----------------------------
    def transfer(self, amount_usd: float, from_wallet: str, to_wallet: str) -> dict:
        if amount_usd <= 0 or amount_usd > self.max_transfer_usd:
            raise ValueError(f"transfer ${amount_usd:,.0f} is outside the ${self.max_transfer_usd:,.0f} cap")
        return self.ex.transfer(self.quote, amount_usd, from_wallet, to_wallet)
