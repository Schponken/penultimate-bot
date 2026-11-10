"""The forecast plumbing: reading sampled paths, logging the model's calls, grading them honestly."""
import json
import time

import pytest

from penultimate.config import load_config
from penultimate.forecast import ForecastHub, TF_S
from penultimate.forecaster import closed_candles
from penultimate.store import Store


def hub(tmp_path):
    cfg = load_config()
    cfg["feed"] = "live"
    return ForecastHub(cfg, tmp_path, tmp_path)


def write_forecast(tmp_path, made, base_ts, last_close, finals_up, n=10, horizon=24):
    closes = [[last_close * (1.01 if i < finals_up else 0.99)] * horizon for i in range(n)]
    (tmp_path / "forecast.json").write_text(json.dumps({
        "model": "NeoQuasar/Kronos-small", "symbol": "BTC/USD", "timeframe_s": TF_S, "made": made,
        "base_ts": base_ts, "last_close": last_close, "horizon": horizon, "n": n,
        "closes": closes, "highs": closes, "lows": closes, "took_s": 1.0}))


def test_calls_are_logged_once_and_graded_at_the_deadline(tmp_path):
    h, store = hub(tmp_path), Store()
    base_ts, now = 1_800_000_000, 1_800_003_700.0
    write_forecast(tmp_path, made=now, base_ts=base_ts, last_close=100.0, finals_up=7)
    lines = h.tick(100.2, now, store)
    assert any("70%" in l and "shadow mode" in l for l in lines)
    assert h.tick(100.2, now + 5, store) == []                       # same forecast is not logged twice
    assert store.rows("SELECT count(*) FROM model_calls")[0][0] == 2  # 1h and 24h horizons
    v = h.view(100.2, now + 5)
    assert v["fresh"] and v["p_up"] == pytest.approx(0.7) and v["graded"] == 0 and v["hit_rate"] is None

    due_1h = base_ts + TF_S + TF_S
    lines = h.tick(101.0, due_1h + 3, store)                          # price went up, model said 70% up
    assert any("HIT" in l for l in lines)
    v = h.view(101.0, due_1h + 3)
    assert v["graded"] == 1 and v["hits"] == 1 and v["hit_rate"] == 1.0
    assert v["brier"] == pytest.approx((0.7 - 1) ** 2)

    due_24h = base_ts + TF_S + 24 * TF_S
    lines = h.tick(95.0, due_24h + 3, store)                          # price ended lower: a miss
    assert any("MISS" in l for l in lines)
    assert h.view(95.0, due_24h + 3)["hit_rate"] == 0.5


def test_a_call_is_not_graded_with_a_late_price(tmp_path):
    h, store = hub(tmp_path), Store()
    base_ts, now = 1_800_000_000, 1_800_003_700.0
    write_forecast(tmp_path, made=now, base_ts=base_ts, last_close=100.0, finals_up=9)
    h.tick(100.0, now, store)
    h.tick(120.0, base_ts + 2 * TF_S + 4000, store)                   # the bot was off at the deadline
    assert store.rows("SELECT status FROM model_calls WHERE horizon_s=3600")[0][0] == "skipped"
    assert h.view(120.0, now)["graded"] == 0


def test_stale_or_mismatched_forecasts_are_not_shown(tmp_path):
    h, store = hub(tmp_path), Store()
    now = 1_800_003_700.0
    write_forecast(tmp_path, made=now - 4 * 3600, base_ts=1_799_990_000, last_close=100.0, finals_up=5)
    h.tick(100.0, now, store)
    assert not h.view(100.0, now)["fresh"] and h.payload(100.0, now)["forecast"] is None
    assert store.rows("SELECT count(*) FROM model_calls")[0][0] == 0   # an old forecast is not logged as a call
    write_forecast(tmp_path, made=now, base_ts=1_800_000_000, last_close=100.0, finals_up=5)
    time.sleep(0.01)
    import os
    os.utime(tmp_path / "forecast.json", (now, now + 1))
    h.tick(150.0, now, store)                                         # forecast is for a very different price
    assert not h.view(150.0, now)["fresh"]


def test_the_candle_still_forming_is_left_out():
    raw = {"timeframe_s": 3600, "candles": [[7200, 1, 2, 0.5, 1.5, 10], [0, 1, 2, 0.5, 1.5, 10],
                                            [3600, 1, 2, 0.5, 1.5, 10], [10800, 1, None, 0.5, 1.5, 10]]}
    rows = closed_candles(raw, now=9000)
    assert [r[0] for r in rows] == [0, 3600]
