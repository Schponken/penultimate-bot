"""The strategy lab: its simulator is honest about fees and pessimistic about ordering, and it never trades."""
import random

from penultimate.lab import EP_HOURS, ELITE, POP, Lab, episodes, simulate, score
from penultimate.config import load_limits

FEES = dict(usd=100.0, maker=0.005, taker=0.009, max_pos=300.0, recenter=0.012)
GENE = {"levels": [0.03], "tp": 0.02, "stop": 0.10, "hold": 48}


def bar(o, h, l, c):
    return (o, h, l, c)


def test_a_dip_then_a_bounce_makes_the_take_profit_minus_fees():
    bars = [bar(100, 100, 100, 100), bar(100, 100, 96, 97), bar(97, 100, 97, 99.5)] + [bar(99.5, 99.5, 99.5, 99.5)] * 3
    pnl, curve, trades = simulate(bars, GENE, **FEES)
    assert [t[1] for t in trades] == ["buy", "profit"]
    assert abs(trades[0][2] - 97.0) < 1e-9 and abs(trades[1][2] - 97.0 * 1.02) < 1e-9
    assert abs(pnl - (100 * 1.02 * (1 - 0.005) - 100 * 1.005)) < 1e-6        # about +$0.99 on $100 after both fees
    assert len(curve) == len(bars)


def test_no_take_profit_in_the_same_hour_as_the_buy():
    bars = [bar(100, 100, 100, 100), bar(100, 101, 96, 100)]                  # dipped and bounced inside one hour
    pnl, _, trades = simulate(bars, GENE, **FEES)
    assert [t[1] for t in trades] == ["buy"]                                  # still holding: no sale was booked
    assert abs(pnl - (100 / 97 * 100 * (1 - 0.009) - 100 * 1.005)) < 1e-6     # marked at the close, after the exit fee


def test_a_crash_through_the_stop_loses_money_and_the_stop_wins_ties():
    bars = [bar(100, 100, 100, 100), bar(100, 100, 96, 96.5), bar(96.5, 99.5, 89, 99)]   # stop and take-profit both touched
    pnl, _, trades = simulate(bars, GENE, **FEES)
    assert [t[1] for t in trades] == ["buy", "stop"] and pnl < -6


def test_flat_prices_mean_no_trades_and_zero():
    pnl, curve, trades = simulate([bar(100, 100.2, 99.8, 100)] * 20, GENE, **FEES)
    assert trades == [] and pnl == 0 and set(curve) == {0.0}


def test_the_position_cap_is_respected():
    g = {"levels": [0.01, 0.02, 0.03], "tp": 0.5, "stop": 0.5, "hold": 99}
    _, _, trades = simulate([bar(100, 100, 100, 100), bar(100, 100, 90, 90)], g, **{**FEES, "usd": 150.0})
    assert sum(t[1] == "buy" for t in trades) == 2                            # a third $150 lot would pass the $300 cap


def candles(n=300, seed=3):
    rng, px, out = random.Random(seed), 100.0, []
    for i in range(n):
        o = px
        px *= 1 + rng.gauss(0, 0.006)
        out.append([1_700_000_000 + i * 3600, o, max(o, px) * 1.003, min(o, px) * 0.997, px, 1.0])
    return out


def test_the_holdout_never_overlaps_the_training_history():
    eps = episodes(candles())
    train_end = max(e["t0"] for e in eps if e["split"] == "train") + EP_HOURS * 3600
    assert min(e["t0"] for e in eps if e["split"] == "test") >= train_end
    assert sum(e["split"] == "test" for e in eps) >= 5


class FakeHub:
    enabled, live_prices, forecast = True, True, None
    def __init__(self):
        self.history = candles()
        self.candles = [c[:5] for c in self.history[-96:]]
    def is_fresh(self, price, now):
        return False


def test_rounds_keep_the_best_and_report_every_trade_path():
    lab = Lab({"paper": {}, "strategies": {"deep_bids": {"levels": [0.03, 0.05, 0.08]}}}, FakeHub(), load_limits())
    assert not hasattr(lab, "broker") and not hasattr(lab, "place")           # it has no way to trade
    lab.step()
    first = {v["id"]: v["fit"] for v in lab.pop}
    best = sorted(first, key=lambda k: -first[k])[:ELITE]
    lab.step()
    assert len(lab.pop) == POP and lab.gen == 2
    assert set(best) <= {v["id"] for v in lab.pop}                            # the best were kept and tested again
    assert all(v["age"] == 1 for v in lab.pop if v["id"] in best)
    view = lab.view()
    assert len(view["gens"]) == 2 and view["live"] is not None and view["test"] >= 5
    d = lab.variant(view["best"])
    assert len(d["paths"]) == view["episodes"] and all(len(p["eq"]) == EP_HOURS for p in d["paths"])
    assert lab.variant("live")["levels"] == [0.03, 0.05, 0.08] and lab.variant(999999) is None
    assert d["id"] == view["best"] and len(view["race"]["v"]) == POP and len(view["race"]["v"][0][1]) == view["episodes"]
    assert abs(score(lab.live_gene, lab.eps, lab.fees)["train"] - view["live"]["train"]) < 1e-9
