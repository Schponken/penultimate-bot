"""The forecast model, run as its own slow process so it can never delay or crash trading.

Reads hourly candles the bot saved (data/candles.json), samples many possible price paths
for the next 24 hours from the Kronos model, and writes them to data/forecast.json.
It has no exchange access and places no orders.

Run:  <forecast-python> -m penultimate.forecaster --data data
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


def write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(path)


def closed_candles(raw: dict, now: float) -> list:
    """Drop the candle that is still forming; keep [ts_seconds, o, h, l, c, v] rows in time order."""
    tf = raw["timeframe_s"]
    rows = sorted(r for r in raw["candles"] if all(v is not None for v in r[:5]))
    return [r for r in rows if r[0] + tf <= now]


def sample_paths(predictor, rows: list, tf: int, horizon: int, n: int, T: float, top_p: float):
    """n sampled futures. Returns (closes, highs, lows), each n x horizon."""
    import numpy as np
    import pandas as pd
    from .vendor.kronos.kronos import calc_time_stamps

    a = np.array(rows, dtype=np.float64)
    ts, o, h, l, c, v = a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5]
    amount = v * (o + h + l + c) / 4.0
    x = np.stack([o, h, l, c, v, amount], axis=1).astype(np.float32)
    x_ts = pd.Series(pd.to_datetime(ts, unit="s"))
    y_ts = pd.Series(pd.to_datetime(ts[-1] + tf * np.arange(1, horizon + 1), unit="s"))
    x_stamp = calc_time_stamps(x_ts).values.astype(np.float32)
    y_stamp = calc_time_stamps(y_ts).values.astype(np.float32)
    mean, std = x.mean(axis=0), x.std(axis=0)
    xn = np.clip((x - mean) / (std + 1e-5), -predictor.clip, predictor.clip)
    rep = lambda m: np.repeat(m[None], n, axis=0)             # one copy of the history per sampled path
    preds = predictor.generate(rep(xn), rep(x_stamp), rep(y_stamp), horizon, T, 0, top_p, 1, False)
    preds = preds * (std + 1e-5) + mean                        # n x horizon x 6
    po, ph, pl, pc = preds[:, :, 0], preds[:, :, 1], preds[:, :, 2], preds[:, :, 3]
    highs = np.maximum.reduce([ph, po, pc])                    # a candle's high can't sit below its close
    lows = np.minimum.reduce([pl, po, pc])
    return pc, highs, lows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--model", default="NeoQuasar/Kronos-small")
    ap.add_argument("--tokenizer", default="NeoQuasar/Kronos-Tokenizer-base")
    ap.add_argument("--samples", type=int, default=48)
    ap.add_argument("--horizon", type=int, default=24)
    ap.add_argument("--lookback", type=int, default=400)
    ap.add_argument("--every", type=float, default=900)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)

    def status(state: str, detail: str = "") -> None:
        write_json(data / "forecaster-status.json", {"state": state, "detail": detail, "ts": time.time(),
                                                     "model": args.model})

    status("loading", "loading the model (the first run downloads it)")
    try:
        import torch
        from .vendor.kronos import Kronos, KronosPredictor, KronosTokenizer
        torch.set_num_threads(max(1, min(4, (os.cpu_count() or 2) // 2)))
        tokenizer = KronosTokenizer.from_pretrained(args.tokenizer)
        model = Kronos.from_pretrained(args.model)
        tokenizer.eval()
        model.eval()
        predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=512)
    except Exception as e:
        status("error", f"could not load the model: {type(e).__name__}: {e}")
        raise

    last_base, last_made = None, 0.0
    while True:
        try:
            raw = json.loads((data / "candles.json").read_text())
            rows = closed_candles(raw, time.time())[-args.lookback:]
        except (OSError, ValueError, KeyError):
            rows, raw = [], {}
        if len(rows) < 64:
            status("waiting", "waiting for price history from the bot")
        elif rows[-1][0] != last_base or time.time() - last_made >= args.every:
            t0 = time.time()
            try:
                closes, highs, lows = sample_paths(predictor, rows, raw["timeframe_s"], args.horizon,
                                                   args.samples, 1.0, 0.9)
                r = lambda m: [[round(float(v), 2) for v in row] for row in m]
                last_base, last_made = rows[-1][0], time.time()
                write_json(data / "forecast.json", {
                    "model": args.model, "symbol": raw.get("symbol"), "timeframe_s": raw["timeframe_s"],
                    "made": last_made, "base_ts": last_base, "last_close": rows[-1][4],
                    "horizon": args.horizon, "n": args.samples, "context": len(rows),
                    "closes": r(closes), "highs": r(highs), "lows": r(lows),
                    "took_s": round(time.time() - t0, 1)})
                status("ready", f"{args.samples} sampled paths in {time.time() - t0:.0f}s")
            except Exception as e:
                status("error", f"forecast failed: {type(e).__name__}: {e}")
                time.sleep(60)
        if args.once:
            break
        time.sleep(20)


if __name__ == "__main__":
    main()
