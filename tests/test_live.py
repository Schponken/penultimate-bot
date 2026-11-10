"""Live broker logic against a stand-in exchange (no network, no real account)."""
import pytest

from penultimate.broker_live import LiveBroker
from penultimate.models import Order, Tick

SPOT = "BTC/USD"


class FakeExchange:
    def __init__(self, reject_keys=False):
        self.orders, self.trades, self.cancelled, self.transfers = {}, [], [], []
        self.usd, self.reject_keys, self._n = 5000.0, reject_keys, 0

    def load_markets(self):
        return {SPOT: {}}

    def fetch_balance(self):
        if self.reject_keys:
            raise RuntimeError("invalid api key")
        return {"total": {"USD": self.usd}, "free": {"USD": self.usd - 10}}

    def amount_to_precision(self, sym, q):
        return f"{q:.6f}"

    def price_to_precision(self, sym, p):
        return f"{p:.2f}"

    def create_order(self, sym, typ, side, qty, price=None):
        self._n += 1
        oid = f"X{self._n}"
        self.orders[oid] = dict(id=oid, symbol=sym, side=side, amount=qty, price=price, type=typ)
        return {"id": oid}

    def fill(self, oid, price):
        o = self.orders.pop(oid)
        self.trades.append({"id": f"T{oid}", "order": oid, "timestamp": getattr(self, "now_ms", 1_000_000), "side": o["side"],
                            "amount": o["amount"], "price": price, "fee": {"cost": 0.5}})
        self.usd += (-1 if o["side"] == "buy" else 1) * o["amount"] * price - 0.5

    def fetch_my_trades(self, sym, limit=50):
        return list(self.trades)

    def fetch_open_orders(self, sym):
        return list(self.orders.values())

    def cancel_order(self, oid, sym):
        self.cancelled.append(oid)
        self.orders.pop(oid, None)

    def transfer(self, code, amount, a, b):
        self.transfers.append((code, amount, a, b))
        return {"id": "tr1"}


def t(px=100.0):
    return Tick(1000.0, SPOT, px - 0.01, px + 0.01, px)


def test_self_check_reports_problems_without_trading():
    assert LiveBroker(FakeExchange()).self_check([SPOT]) == []
    probs = LiveBroker(FakeExchange()).self_check([SPOT, "DOGE/USD"])
    assert any("DOGE/USD" in p for p in probs)
    ex = FakeExchange(reject_keys=True)
    assert any("keys" in p for p in LiveBroker(ex).self_check([SPOT]))
    assert ex._n == 0                                     # the check never places an order


def test_order_fill_and_reconcile():
    ex = FakeExchange()
    b = LiveBroker(ex)
    o = Order(SPOT, "buy", 0.5, "limit", 99.0, strategy="deep_bids", tag="ladder-0")
    assert b.submit(o, t()) == [] and o.status == "open" and o.venue_id == "X1"
    assert b.sync() == [] and len(b.open_orders) == 1     # nothing filled yet
    ex.fill("X1", 99.0)
    fills = b.sync()
    assert len(fills) == 1 and fills[0].strategy == "deep_bids" and fills[0].tag == "ladder-0"
    assert b.position(SPOT).qty == pytest.approx(0.5) and b.position(SPOT).avg == 99.0
    assert b.open_orders == [] and b.cash == pytest.approx(5000 - 49.5 - 0.5)
    assert b.sync() == []                                 # the same trade is never counted twice
    assert b.equity({SPOT: 110.0}) == pytest.approx(b.cash + 55.0)


def test_cancel_all_and_flatten():
    ex = FakeExchange()
    b = LiveBroker(ex)
    b.submit(Order(SPOT, "buy", 0.5), t())
    ex.fill("X1", 100.0)
    b.sync()
    b.submit(Order(SPOT, "buy", 0.1, "limit", 90.0), t())
    b.flatten({SPOT: t()})
    assert "X2" in ex.cancelled
    sells = [o for o in ex.orders.values() if o["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["amount"] == pytest.approx(0.5)


def test_exchange_refusal_is_a_rejection_not_a_crash():
    ex = FakeExchange()
    ex.create_order = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("insufficient funds"))
    o = Order(SPOT, "buy", 1.0)
    assert LiveBroker(ex).submit(o, t()) == [] and o.status == "rejected" and "insufficient" in o.reason


def test_transfers_are_capped_and_there_is_no_withdraw():
    ex = FakeExchange()
    b = LiveBroker(ex, max_transfer_usd=1000)
    b.transfer(400, "spot", "future")
    assert ex.transfers == [("USD", 400, "spot", "future")]
    with pytest.raises(ValueError):
        b.transfer(1500, "spot", "future")
    assert not hasattr(b, "withdraw")
    import pathlib
    src = "".join(p.read_text() for p in pathlib.Path(__file__).parent.parent.joinpath("penultimate").glob("*.py"))
    assert ".withdraw(" not in src                        # no withdrawal call anywhere in the bot


def test_market_buy_passes_a_price_and_free_cash_is_tracked():
    ex = FakeExchange()
    b = LiveBroker(ex)
    b.submit(Order(SPOT, "buy", 0.5), t(100.0))
    assert ex.orders["X1"]["price"] == pytest.approx(100.01)      # Coinbase sizes market buys in dollars
    b.submit(Order(SPOT, "sell", 0.5), t(100.0))
    assert ex.orders["X2"]["price"] is None
    b.sync()
    assert b.free_cash == pytest.approx(4990.0)


def test_key_file_paste_is_understood(tmp_path, monkeypatch):
    import json
    from penultimate import broker_live
    monkeypatch.setattr(broker_live, "KEYS_FILE", tmp_path / "keys.json")
    pem = "-----BEGIN EC PRIVATE KEY-----\n" + "A" * 64 + "\n" + "B" * 20 + "\n-----END EC PRIVATE KEY-----\n"
    broker_live.save_keys(json.dumps({"name": "organizations/o/apiKeys/k", "privateKey": pem}), "")
    saved = broker_live.load_keys()
    assert saved["apiKey"] == "organizations/o/apiKeys/k" and saved["secret"] == pem
    broker_live.save_keys("organizations/o/apiKeys/k", pem.replace("\n", " "))      # line breaks lost in a paste
    assert broker_live.load_keys()["secret"] == pem
    with pytest.raises(ValueError):
        broker_live.save_keys("only-a-name", "")


# ---- the whole engine in live mode, against the stand-in exchange -----------------
def _live_engine(ex, batches, store=None, state=None):
    import copy
    from penultimate.config import load_config, load_limits
    from penultimate.engine import Engine
    from penultimate.store import Store
    from tests.test_core import ListFeed
    cfg = copy.deepcopy(load_config())
    cfg["mode"], cfg["feed"] = "live", "live"
    cfg["strategies"]["funding_carry"]["enabled"] = False
    b = LiveBroker(ex, max_transfer_usd=500)
    store = store or Store()
    if state is not None:
        b.load_state(state)
    feed = ListFeed(batches)
    feed.live = True
    return Engine(cfg, load_limits(), feed=feed, broker=b, store=store), store


def _ticks(start, n, px=100.0):
    return [(start + i * 60.0, [Tick(start + i * 60.0, SPOT, px - 0.01, px + 0.01, px)]) for i in range(n)]


def test_live_engine_waits_for_funds_then_starts_by_itself():
    ex = FakeExchange()
    ex.usd = 0.0
    ex.fetch_balance = lambda: {"total": {"USD": ex.usd}, "free": {"USD": ex.usd - sum(
        o["amount"] * o["price"] for o in ex.orders.values() if o["side"] == "buy" and o["price"])}}
    e, store = _live_engine(ex, _ticks(1_800_000_000.0, 5))
    e.run()
    assert ex._n == 0 and "waiting for funds" in e.strategies[0].note       # no orders sent to the exchange
    ex.usd = 500.0                                                          # the deposit lands
    e.feed.batches, e.feed.stopped = _ticks(1_800_000_600.0, 5), False
    e.run()
    buys = [o for o in ex.orders.values() if o["side"] == "buy"]
    assert len(buys) == 3 and all(o["type"] == "limit" and o["amount"] * o["price"] <= 100 for o in buys)
    assert sum(o["amount"] * o["price"] for o in buys) <= 300
    assert e.risk.rejections == 0

    # one rung fills -> a take-profit sell goes out for exactly that amount
    first = min(buys, key=lambda o: -o["price"])
    ex.now_ms = 1_800_001_100_000
    ex.fill(first["id"], first["price"])
    e.feed.batches, e.feed.stopped = _ticks(1_800_001_200.0, 3, px=97.5), False
    e.run()
    sells = [o for o in ex.orders.values() if o["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["amount"] == pytest.approx(first["amount"])
    assert sells[0]["price"] == pytest.approx(first["price"] * 1.02, rel=1e-3)
    held = e.broker.position(SPOT).qty
    assert held == pytest.approx(first["amount"])

    # restart: a brand-new process picks up positions, orders and lots, and re-sends nothing
    sent_before = ex._n
    e.save_state()
    e2, _ = _live_engine(ex, _ticks(1_800_002_000.0, 3, px=97.5), store=store, state=store.get("live_state"))
    assert e2.broker.position(SPOT).qty == pytest.approx(held)
    assert len(e2.broker.open_orders) == 3 and len(e2.strategies[0].lots) == 1
    e2.run()
    assert ex._n == sent_before and e2.broker.position(SPOT).qty == pytest.approx(held)
    assert len(e2.fills) == 0                                               # the old fill is not counted twice

    # the take-profit fills while the bot is running -> flat, one win
    ex.fill(sells[0]["id"], sells[0]["price"])
    e2.feed.batches, e2.feed.stopped = _ticks(1_800_003_000.0, 3, px=99.5), False
    e2.run()
    assert e2.broker.position(SPOT).qty == pytest.approx(0, abs=1e-9) and e2.strategies[0].wins == 1
    assert e2.broker.realized > 0


def test_fill_is_not_lost_when_an_order_leaves_the_book_before_its_trade_shows():
    ex = FakeExchange()
    b = LiveBroker(ex)
    o = Order(SPOT, "buy", 0.5, "limit", 99.0, strategy="deep_bids", tag="ladder-0")
    b.submit(o, t())
    ex.orders.pop("X1")                    # gone from the open-orders list, trade not reported yet
    assert b.sync() == [] and b.open_orders == []
    ex.trades.append({"id": "T1", "order": "X1", "timestamp": 1_000_000, "side": "buy", "amount": 0.5,
                      "price": 99.0, "fee": {"cost": 0.25}})
    fills = b.sync()
    assert len(fills) == 1 and b.position(SPOT).qty == pytest.approx(0.5)


def test_the_whole_account_is_the_bots_when_told_so():
    ex = FakeExchange()
    bal = {"BTC": 0.002}
    ex.fetch_balance = lambda: {"total": {"USD": 100.0, **bal}, "free": {"USD": 100.0}}
    b = LiveBroker(ex)
    b.sync()
    assert b.position(SPOT).qty == 0                                   # off by default: only its own trades count
    b.own_symbols, b.marks = [SPOT], {SPOT: 75000.0}
    b.sync()
    p = b.position(SPOT)
    assert p.qty == 0.002 and p.avg == 75000.0 and round(b.adopted_usd, 2) == 150.0 and len(b.adopted) == 1
    assert round(b.equity(b.marks), 2) == 250.0 and round(b.gross_exposure(b.marks), 2) == 150.0
    b.adopted, b.adopted_usd = [], 0.0
    b.sync()
    assert b.adopted == []                                             # in step: nothing more to take on
    bal["BTC"] = 0.0005                                                # you sold some yourself in the Coinbase app
    b.sync()
    assert b.position(SPOT).qty == 0.0005 and round(b.adopted_usd, 2) == -112.5
    bal["BTC"] = 0.00050001                                            # dust is ignored
    b.sync()
    assert b.position(SPOT).qty == 0.0005
    tick = Tick(1.0, SPOT, 74990, 75010, 75000)
    b.flatten({SPOT: tick})                                            # Stop everything sells all of it
    assert any(o["side"] == "sell" and abs(o["amount"] - 0.0005) < 1e-9 for o in ex.orders.values())
