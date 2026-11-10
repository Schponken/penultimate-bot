"""Checks that the parts that protect money behave as designed."""
import copy
import time

import pytest

from penultimate.broker_paper import PaperBroker
from penultimate.config import Limits, load_config, load_limits
from penultimate.engine import Engine
from penultimate.models import Order, Tick
from penultimate.regime import Regime
from penultimate.risk import RiskEngine
from penultimate.scorekeeper import Scorekeeper
from penultimate.store import Store

SPOT, PERP = "BTC/USD", "BTC/USD:USD"


def tick(sym=SPOT, px=100.0, ts=1000.0, spread=0.02, fr=None):
    return Tick(ts, sym, px - spread / 2, px + spread / 2, px, fr)


def limits(**kw):
    base = dict(max_order_usd=500, max_position_usd=2000, max_gross_exposure_usd=4000, max_leverage=2.0,
                max_daily_loss_usd=200, max_transfer_usd=1000, stale_feed_seconds=15, max_spread_bps=60,
                cooldown_seconds=300)
    base.update(kw)
    return Limits(**base)


def broker(cash=10000, fee=0.001):
    return PaperBroker(cash, fee, fee, 0)


def sim_config():
    """The shipped settings, forced onto the built-in practice market so tests never touch a network."""
    cfg = copy.deepcopy(load_config())
    cfg["mode"], cfg["feed"] = "paper", "sim"
    cfg["paper"]["starting_cash"] = 10000
    return cfg


class ListFeed:
    """Feeds a fixed script of ticks; `clock` lets a test move time without sending prices."""
    live = False

    def __init__(self, batches):
        self.batches, self.stopped, self.errors, self.last_error = batches, False, 0, ""
        self.clock = 0.0

    def now(self):
        return self.clock

    def stream(self):
        for ts, batch in self.batches:
            if self.stopped:
                return
            self.clock = ts
            yield batch


def engine(batches, **over):
    cfg = sim_config()
    cfg["strategies"]["deep_bids"]["enabled"] = over.pop("deep", False)
    cfg["strategies"]["funding_carry"]["enabled"] = over.pop("carry", False)
    cfg["paper"].update(over.pop("paper", {}))
    cfg["strategies"]["funding_carry"].update(over.pop("carry_params", {}))
    return Engine(cfg, over.pop("limits", limits()), feed=ListFeed(batches), store=Store())


# ---- paper broker accounting ------------------------------------------------
def test_spot_round_trip_accounting():
    b = broker(fee=0.001)
    b.submit(Order(SPOT, "buy", 1.0), tick(px=100))
    assert b.cash == pytest.approx(10000 - 100.01 - 0.10001)
    b.submit(Order(SPOT, "sell", 1.0), tick(px=110))
    assert b.position(SPOT).qty == 0
    assert b.realized == pytest.approx(109.99 - 100.01)
    assert b.equity({SPOT: 110}) == pytest.approx(b.cash)
    assert b.cash == pytest.approx(10000 + 9.98 - b.fees)


def test_spot_cannot_sell_what_it_does_not_hold():
    b = broker()
    o = Order(SPOT, "sell", 1.0)
    assert b.submit(o, tick()) == [] and o.status == "rejected"


def test_spot_cannot_spend_more_than_cash():
    b = broker(cash=50)
    o = Order(SPOT, "buy", 1.0)
    b.submit(o, tick(px=100))
    assert o.status == "rejected" and b.cash == 50


def test_perp_short_profit_and_funding():
    b = broker(fee=0.0)
    b.submit(Order(PERP, "sell", 2.0), tick(PERP, 100, spread=0))
    assert b.position(PERP).qty == -2.0 and b.cash == 10000
    assert b.apply_funding(PERP, 0.001, 100) == pytest.approx(0.2)      # shorts are paid when rate > 0
    assert b.equity({PERP: 90}) == pytest.approx(10000.2 + 20)
    b.submit(Order(PERP, "buy", 2.0), tick(PERP, 90, spread=0))
    assert b.cash == pytest.approx(10020.2)


def test_limit_order_rests_then_fills_as_maker():
    b = PaperBroker(10000, 0.01, 0.001, 0)
    o = Order(SPOT, "buy", 1.0, "limit", 95.0)
    assert b.submit(o, tick(px=100)) == [] and o.status == "open"
    assert b.on_tick(tick(px=96)) == []
    f = b.on_tick(tick(px=94))
    assert len(f) == 1 and f[0].price == 95.0 and f[0].fee == pytest.approx(0.095)


def test_flatten_closes_everything():
    b = broker()
    b.submit(Order(SPOT, "buy", 1.0), tick())
    b.submit(Order(PERP, "sell", 1.0), tick(PERP))
    b.submit(Order(SPOT, "buy", 1.0, "limit", 50.0), tick())
    b.flatten({SPOT: tick(), PERP: tick(PERP)})
    assert b.open_orders == [] and all(abs(p.qty) < 1e-12 for p in b.positions.values())


# ---- risk engine ------------------------------------------------------------
def test_order_cap():
    r, b = RiskEngine(limits()), broker()
    ok, why = r.check(Order(SPOT, "buy", 6.0), tick(), b, {SPOT: 100})
    assert not ok and "order cap" in why


def test_position_cap_counts_resting_orders():
    r, b = RiskEngine(limits()), broker()
    t = tick()
    for _ in range(4):
        o = Order(SPOT, "buy", 4.9, "limit", 99.0)
        assert r.check(o, t, b, {SPOT: 100})[0]
        b.submit(o, t)
    ok, why = r.check(Order(SPOT, "buy", 4.9, "limit", 99.0), t, b, {SPOT: 100})
    assert not ok and "position" in why


def test_leverage_and_exposure_caps():
    r, b = RiskEngine(limits(max_position_usd=1e9, max_order_usd=1e9, max_gross_exposure_usd=1e9)), broker(cash=1000)
    assert not r.check(Order(PERP, "sell", 25.0), tick(PERP), b, {PERP: 100})[0]     # 2.5x
    assert r.check(Order(PERP, "sell", 15.0), tick(PERP), b, {PERP: 100})[0]         # 1.5x
    r2 = RiskEngine(limits(max_position_usd=1e9, max_order_usd=1e9, max_leverage=100))
    ok, why = r2.check(Order(PERP, "sell", 50.0), tick(PERP), broker(), {PERP: 100})
    assert not ok and "exposure" in why


def test_halt_blocks_new_risk_but_never_blocks_exits():
    r, b = RiskEngine(limits()), broker()
    b.submit(Order(SPOT, "buy", 2.0), tick())
    r.halt("test")
    assert not r.check(Order(SPOT, "buy", 1.0), tick(), b, {SPOT: 100})[0]
    assert r.check(Order(SPOT, "sell", 2.0), tick(), b, {SPOT: 100})[0]
    r.resume()
    r.pause("cooldown", 2000.0)
    assert not r.check(Order(SPOT, "buy", 1.0), tick(ts=1500), b, {SPOT: 100})[0]
    assert r.check(Order(SPOT, "sell", 1.0), tick(ts=1500), b, {SPOT: 100})[0]
    assert r.check(Order(SPOT, "buy", 1.0), tick(ts=2001), b, {SPOT: 100})[0]


def test_daily_loss_trigger_and_new_day_reset():
    r = RiskEngine(limits())
    day = 86400 * 20000
    assert not r.on_equity(10000, day)
    assert not r.on_equity(9801, day + 60)
    assert r.on_equity(9800, day + 120)
    assert not r.on_equity(9800, day + 86400)        # next day starts a fresh budget


def test_limits_file_loads_and_rejects_nonsense():
    assert load_limits().max_daily_loss_usd > 0
    with pytest.raises(ValueError):
        limits(max_order_usd=0)
    with pytest.raises(Exception):
        load_limits().max_order_usd = 1e9             # frozen: code cannot raise its own limits


# ---- engine reactions -------------------------------------------------------
def test_daily_loss_cap_sells_everything_and_halts():
    day = 86400 * 20000.0
    e = engine([(day, [tick(px=100, ts=day)]), (day + 60, [tick(px=100, ts=day + 60)]),
                (day + 120, [tick(px=79, ts=day + 120)]), (day + 180, [tick(px=79, ts=day + 180)])])
    hold = {"done": False}

    class Buyer:
        name = "buyer"
        def on_tick(self, t):
            if not hold["done"]:
                hold["done"] = True
                for _ in range(3):
                    e.place(Order(SPOT, "buy", 4.0, strategy="buyer"))
        def on_fill(self, f): ...
        def reset(self): hold["reset"] = True
        def state(self): return {}
        def view(self): return {"name": "buyer", "note": ""}

    e.strategies = [Buyer()]
    e.run()
    assert e.risk.halted and "daily loss" in e.risk.halt_reason
    assert e.broker.position(SPOT).qty == 0 and hold.get("reset")
    assert e.place(Order(SPOT, "buy", 1.0)).status == "rejected"
    assert e.store.get("risk")["halted"] is True      # survives a restart


def test_stale_feed_cancels_orders_and_pauses():
    e = engine([(1000.0, [tick(ts=1000)]), (1005.0, []), (1030.0, []), (1031.0, [tick(ts=1031)])])
    placed = {}

    class Rester:
        name = "rester"
        def on_tick(self, t):
            if not placed:
                placed["o"] = e.place(Order(SPOT, "buy", 1.0, "limit", 90.0, strategy="rester"))
        def on_fill(self, f): ...
        def reset(self): ...
        def state(self): return {}
        def view(self): return {"name": "rester", "note": ""}

    e.strategies = [Rester()]
    e.run()
    assert placed["o"].status == "cancelled" and e.broker.open_orders == []
    assert e.risk.paused_until >= 1031 + 300
    assert any("no fresh prices" in ev["detail"] for ev in e.events)
    assert e.place(Order(SPOT, "buy", 1.0)).status == "rejected"


def test_wide_spread_pauses_entries():
    e = engine([(1000.0, [tick(ts=1000)]), (1001.0, [tick(ts=1001, spread=2.0)])])
    e.run()
    assert e.risk.pause_reason == "spread too wide"


def test_stop_button_and_resume():
    e = engine([(1000.0 + i, [tick(ts=1000.0 + i)]) for i in range(4)])
    e.commands.put(("stop_all", None))
    e.run(max_ticks=2)
    assert e.risk.halted and e.risk.halt_reason == "stop button pressed"
    e.commands.put(("resume", None))
    e.feed.batches = [(2000.0, [tick(ts=2000)])]
    e.run()
    assert not e.risk.halted


def test_risk_off_signal_file_cuts_size(tmp_path):
    e = engine([(1000.0 + i, [tick(ts=1000.0 + i)]) for i in range(45)])
    e.data_dir = tmp_path
    (tmp_path / "signals.json").write_text('{"risk_off": true, "note": "bond volatility spike"}')
    e.run()
    assert e.regime.risk_off and e.size_mult(use_regime=False) == 0.25


# ---- strategies -------------------------------------------------------------
def test_deep_bids_catch_a_wick_and_take_profit():
    ts, batches, px = 1000.0, [], 100.0
    path = [100.0] * 5 + [98, 95, 91, 91.5, 93, 95, 97, 99, 100, 100.5]
    for p in path:
        ts += 60
        batches.append((ts, [tick(px=p, ts=ts)]))
    e = engine(batches, deep=True, paper={"taker_fee": 0.0, "maker_fee": 0.0})
    e.run()
    s = e.strategies[0]
    assert s.wins == 3 and s.losses == 0
    assert e.broker.realized > 0 and e.broker.position(SPOT).qty == pytest.approx(0, abs=1e-9)
    # the ladder did not chase the price down while it was falling
    buys = [f for f in e.fills if f["tag"].startswith("ladder")]
    assert max(f["price"] for f in buys) == pytest.approx(97.0)


def test_carry_skips_when_fees_too_high_and_opens_when_they_are_not():
    def run(fee):
        ts, batches = 1000.0, []
        for i in range(60):
            ts += 60
            batches.append((ts, [tick(px=100 + (i % 3) * 0.01, ts=ts), tick(PERP, 100.1, ts, fr=0.0006)]))
        e = engine(batches, carry=True, paper={"taker_fee": fee, "perp_taker_fee": fee})
        e.regime.window = 40
        e.run()
        return e
    high = run(0.006)
    assert high.strategies[0].opened == 0 and "skipped" in high.strategies[0].note
    assert high.broker.fees == 0
    low = run(0.0002)
    assert low.strategies[0].opened == 1
    assert low.broker.position(SPOT).qty == pytest.approx(-low.broker.position(PERP).qty)   # hedged


def test_carry_never_leaves_half_a_hedge():
    ts, batches = 1000.0, []
    for i in range(60):
        ts += 60
        batches.append((ts, [tick(px=100 + (i % 3) * 0.01, ts=ts), tick(PERP, 100.1, ts, fr=0.0006)]))
    # gross cap allows the first leg but not the second
    e = engine(batches, carry=True, paper={"taker_fee": 0.0002, "perp_taker_fee": 0.0002},
               carry_params={"notional_usd": 500}, limits=limits(max_gross_exposure_usd=700))
    e.run()
    assert e.broker.position(SPOT).qty == pytest.approx(0, abs=1e-9)
    assert e.broker.position(PERP).qty == 0
    assert len(e.fills) <= 2                          # one attempt, unwound; no churn


# ---- regime and scorekeeper -------------------------------------------------
def test_regime_goes_to_panic_on_a_crash():
    g, px = Regime(window=40), 100.0
    for i in range(60):
        px *= 1.0002 if i % 2 else 0.9998
        g.update(px)
    assert g.state in ("choppy", "trending")
    for _ in range(4):
        px *= 0.985
        g.update(px)
    assert g.state == "panic" and g.size_mult == 0.25


def test_scorekeeper_grades_and_caps_tilt():
    sk = Scorekeeper(Store(), SPOT)
    now = time.mktime(time.strptime("2026-11-10", "%Y-%m-%d"))
    before = {r["source"]: r for r in sk.table()}
    assert before["Ryan Lee (Bitget)"]["weight"] > before["Peter Brandt"]["weight"]
    assert -0.10 <= sk.tilt(74500) <= 0.10
    res = sk.on_price(91000, now)
    assert any("Thielen" in r and "HIT" in r for r in res)
    late = time.mktime(time.strptime("2026-12-05", "%Y-%m-%d"))
    res = sk.on_price(80000, late)
    assert any("Mike Alfred" in r and "MISS" in r for r in res)


# ---- whole system -----------------------------------------------------------
@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_two_simulated_weeks_never_break_a_limit(seed):
    cfg = sim_config()
    cfg["sim"].update(seed=seed, real_delay=0)
    cfg["paper"].update(taker_fee=0.0002, maker_fee=0.0, perp_taker_fee=0.0002, perp_maker_fee=0.0, starting_cash=500)
    L = load_limits()
    e = Engine(cfg, L, store=Store())
    worst = {"gross": 0.0, "lev": 0.0, "order": 0.0, "pos": 0.0}
    orig = e._snapshot

    def spy(now):
        orig(now)
        s = e.snap
        worst["gross"] = max(worst["gross"], s["risk"]["gross"])
        worst["lev"] = max(worst["lev"], s["risk"]["leverage"])
        worst["pos"] = max([worst["pos"]] + [abs(p["value"]) for p in s["positions"]])
    e._snapshot = spy
    e.run(max_ticks=20000)
    # the order cap governs orders that add risk; exits are always allowed through
    worst["order"] = max([0.0] + [q * p for (q, p) in e.store.rows(
        "select qty,price from fills where tag like 'ladder%' or tag in ('carry-spot','carry-perp')")])
    assert worst["order"] <= L.max_order_usd * 1.001
    assert worst["gross"] <= L.max_gross_exposure_usd * 1.10      # room for price moves after entry
    assert worst["pos"] <= L.max_position_usd * 1.10
    assert worst["lev"] <= L.max_leverage * 1.10
    assert e.risk.rejections < 50                                 # strategies are not hammering the limits


def test_state_survives_a_restart(tmp_path):
    cfg = sim_config()
    cfg["sim"].update(seed=3, real_delay=0)
    e1 = Engine(cfg, load_limits(), data_dir=tmp_path)
    e1.run(max_ticks=6000)
    cash, pos = e1.broker.cash, {k: v.qty for k, v in e1.broker.positions.items() if abs(v.qty) > 1e-12}
    lots = len(e1.strategies[0].lots)
    e1.store.close()
    e2 = Engine(cfg, load_limits(), data_dir=tmp_path)
    assert e2.broker.cash == pytest.approx(cash)
    assert {k: v.qty for k, v in e2.broker.positions.items() if abs(v.qty) > 1e-12} == pytest.approx(pos)
    assert len(e2.strategies[0].lots) == lots
