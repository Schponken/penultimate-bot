"""All the parts running together: the trading loop, the forecast plumbing, the lab and the dashboard's
data endpoints. Checks that each part hands the next what it expects, and that everything the dashboard
page reads is really there."""
import json
import random

from penultimate.config import load_config, load_limits
from penultimate.dashboard import Dash
from penultimate.engine import Engine
from penultimate.feeds import SimFeed
from penultimate.forecast import ForecastHub
from penultimate.lab import LAND_N, Lab, land_cell
from penultimate.store import Store


def history(n=300, seed=5):
    rng, px, out = random.Random(seed), 75000.0, []
    for i in range(n):
        o = px
        px *= 1 + rng.gauss(0, 0.006)
        out.append([1_700_000_000 + i * 3600, o, max(o, px) * 1.003, min(o, px) * 0.997, px, 1.0])
    return out


def build(tmp_path):
    cfg = load_config()
    cfg["feed"] = "sim"
    cfg["sim"]["real_delay"] = 0
    hub = ForecastHub({**cfg, "feed": "live"}, tmp_path, tmp_path)      # the plumbing only: no threads started
    hub.history = history()
    hub.candles = [c[:5] for c in hub.history[-96:]]
    lab = Lab(cfg, hub, load_limits())
    eng = Engine(cfg, load_limits(), feed=SimFeed(cfg, cfg["spot_symbol"], cfg["perp_symbol"]),
                 store=Store(tmp_path / "t.db"), data_dir=tmp_path, hub=hub)
    dash = Dash(0)
    dash.engine, dash.lab = eng, lab
    return cfg, hub, lab, eng, dash


def test_every_part_feeds_the_next_and_the_dashboard_gets_what_it_reads(tmp_path):
    cfg, hub, lab, eng, dash = build(tmp_path)
    eng.run(max_ticks=400)
    for _ in range(3):
        lab.step()
    s = json.loads(json.dumps(dash.state()))                           # must survive the trip to the browser
    for key in ("ready", "mode", "feed", "price", "equity", "cash", "free_cash", "day_pnl", "risk", "limits", "regime",
                "strategies", "orders", "positions", "events", "fills", "curve", "regime_hist", "flow", "inner",
                "fc", "version", "health", "update", "keys_saved", "budget", "score", "open_calls"):
        assert key in s, key
    f = s["flow"]
    assert f["ticks"] == 400 and f["evals"] >= f["ticks"] and f["lab"] == 3
    assert f["checks"] == f["passed"] + s["risk"]["rejections"]         # every order that asked was answered
    assert f["placed"] + f["refused"] <= f["checks"]                    # only real orders are counted as placed or refused
    assert f["placed"] >= len(s["orders"])                              # each resting order went through the gate
    assert set(s["inner"]) >= {"feed", "regime", "risk", "tilt"} and s["inner"]["risk"]["checks"] == f["checks"]
    assert abs(sum(s["regime"]["mix"].values()) - 1) < 0.01
    assert all("inner" in x for x in s["strategies"])
    assert len(s["regime_hist"]) == len(s["curve"]) and s["equity"] > 0

    v = json.loads(json.dumps(lab.view()))
    assert v["gen"] == 3 and len(v["gens"]) == 3 and v["best"] in {r[0] for r in v["gens"][-1]["v"]}
    assert len(v["race"]["v"]) == len(v["gens"][-1]["v"]) and len(v["race"]["live"]) == v["episodes"]
    land = v["land"]
    assert len(land["n"]) == LAND_N * LAND_N and sum(land["n"]) == land["tested"] >= 48
    assert all((t is None) == (n == 0) for t, n in zip(land["te"], land["n"]))
    pr = v["progress"]
    assert [r[0] for r in pr["hist"]] == [1, 2, 3] and pr["need"] == 30 and 0 <= pr["pos"] <= 3 and pr["top_live"] >= pr["live"]
    assert all(len(r) == 6 and r[5] == sum(x[4] > 0 for x in g["v"]) for r, g in zip(pr["hist"], v["gens"]))     # "variants ahead" matches the round it describes
    assert lab.view(since=3) == {"state": "running", "detail": lab.detail_text, "gen": 3, "unchanged": True}
    d = json.loads(json.dumps(lab.variant(v["best"])))
    assert d["id"] == v["best"] and len(d["paths"]) == v["episodes"] and d["futures"] == []
    assert json.loads(json.dumps(dash.forecast()))["candles"] == hub.candles


def test_the_learned_map_puts_settings_in_the_right_cell():
    assert land_cell(0.002, 0.005) == 0 and land_cell(0.08, 0.05) == LAND_N * LAND_N - 1
    assert land_cell(1.0, 1.0) == LAND_N * LAND_N - 1 and land_cell(0.0, 0.0) == 0     # out of range clamps to an edge
    assert land_cell(0.08, 0.005) == LAND_N - 1
