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


def test_what_the_lab_learned_survives_a_restart(tmp_path):
    import json
    from penultimate.config import load_config, load_limits
    from penultimate.lab import LAND_N, Lab

    class Hub:
        enabled = live_prices = True
        data_dir = tmp_path
        forecast = None
        candles = []

        def is_fresh(self, *a):
            return False
    hub = Hub()
    rng, px, hist = random.Random(3), 70000.0, []
    for i in range(320):
        o = px
        px *= 1 + rng.gauss(0, 0.006)
        hist.append([1_700_000_000 + i * 3600, o, max(o, px) * 1.003, min(o, px) * 0.997, px, 1.0])
    hub.history = hist
    a = Lab(load_config(), hub, load_limits())
    for _ in range(3):
        a.step()
    b = Lab(load_config(), hub, load_limits())                         # "restart": a new lab reading the same folder
    assert (b.hist, b.runs) == (a.hist, a.runs) and len(a.hist) == 3
    assert (b.gen, b.tested, b.land) == (a.gen, a.tested, a.land) and [v["id"] for v in b.pop] == [v["id"] for v in a.pop]
    assert b.view()["best"] is None and b.view()["land"]["tested"] == a.tested      # readable before its first round
    b.step()
    assert b.gen == 4 and b.tested > a.tested and len(b.gens) == 4 and b.view()["best"] is not None and len(b.hist) == 4
    old = json.loads((tmp_path / "lab.json").read_text())                # a file saved before progress history existed
    old.pop("hist"), old.pop("runs")
    (tmp_path / "lab.json").write_text(json.dumps(old))
    d = Lab(load_config(), hub, load_limits())
    assert [r[0] for r in d.hist] == [1, 2, 3, 4] and abs(d.hist[-1][1] - b.hist[-1][1]) < 0.002 and d.hist[-1][4] is None
    (tmp_path / "lab.json").write_text(json.dumps({"v": 1, "land": {"n": [1]}, "pop": []}))    # a damaged file is ignored
    c = Lab(load_config(), hub, load_limits())
    assert c.gen == 0 and c.land["n"] == [0] * (LAND_N * LAND_N)
    c.step()
    assert c.gen == 1


def test_walk_forward_proof_and_the_real_money_safeguard(tmp_path):
    from penultimate.config import load_config, load_limits
    from penultimate.engine import Engine
    from penultimate.feeds import SimFeed
    from penultimate.lab import walk_forward, tstat, weekly
    from penultimate.store import Store
    rng = random.Random(1)
    good = [[rng.gauss(1.0, 1.0) for _ in range(130)] for _ in range(10)]          # ten variants that really do make money
    bad = [[rng.gauss(-3.0, 3.0) for _ in range(130)] for _ in range(60)]
    live = [rng.gauss(-2.5, 4.0) for _ in range(130)]
    p = walk_forward(good + bad, live)
    assert p["unseen_weeks"] == 78 and p["picks"]["mean"] > 0.5 and p["picks"]["t"] > 3          # picking on the past found them
    assert p["vs_average"]["t"] > 3 and p["vs_live"]["t"] > 3 and p["live_all"]["t"] < -3 and p["ahead_in_hindsight"] == 10
    noise = [[rng.gauss(0, 3.0) for _ in range(130)] for _ in range(70)]
    assert abs(walk_forward(noise, live)["vs_average"]["t"]) < 3                                # nothing to learn: it does not claim to have learned
    assert tstat([1.0, 1.0, 1.0])[1] == 0.0 and tstat([])[0] == 0.0

    hist, px = [], 70000.0
    for i in range(168 * 4):
        o = px
        px *= 1 + rng.gauss(0, 0.006)
        hist.append([1_700_000_000 + i * 3600, o, max(o, px) * 1.003, min(o, px) * 0.997, px, 1.0])
    w, buys = weekly(hist, {"levels": [0.01, 0.02], "tp": 0.01, "stop": 0.05, "hold": 24}, {"usd": 95.0})
    assert len(w) == 4 and buys > 0

    cfg = load_config()
    cfg["feed"] = "sim"
    cfg["sim"]["real_delay"] = 0
    eng = Engine(cfg, load_limits(), feed=SimFeed(cfg, cfg["spot_symbol"], cfg["perp_symbol"]), store=Store(tmp_path / "t.db"), data_dir=tmp_path)

    class L:
        proof = {"done": True, "weeks": 158, "live_all": {"mean": -2.7, "t": -4.75, "won": 57, "lost": 90}}
    eng.lab = L()
    assert eng.gate() == "" and eng.can_add_risk()                      # practice money is never held back
    eng.mode = "live"
    assert "lost $2.70 a week" in eng.gate() and not eng.can_add_risk()  # real money stands aside from a proven loser
    eng._overruled = True
    assert eng.gate() == "" and eng.gate_reason() and eng.can_add_risk()  # unless you overrule it
    eng._overruled, L.proof = False, {"done": True, "weeks": 158, "live_all": {"mean": -0.4, "t": -1.1, "won": 70, "lost": 80}}
    assert eng.gate() == ""                                              # a loss that could be bad luck does not trigger it
