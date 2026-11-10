"""Bot-side plumbing for the forecast model (the slow path).

- keeps hourly candles on disk for the model to read
- installs and supervises the separate model process, restarting it if it stops
- reads the model's sampled price paths and grades its calls against what the price then did

The model runs in shadow mode: it is watched and scored, and it never places or sizes an order.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional

TF = "1h"
TF_S = 3600
FRESH_S = 3 * 3600          # a forecast older than this is not shown as current


class ForecastHub:
    def __init__(self, cfg: dict, data_dir: Path, root: Path):
        f = cfg.get("forecast", {}) or {}
        self.enabled = bool(f.get("enabled", True)) and not os.environ.get("PENULTIMATE_NO_FORECAST")
        self.model = f.get("model", "NeoQuasar/Kronos-small")
        self.tokenizer = f.get("tokenizer", "NeoQuasar/Kronos-Tokenizer-base")
        self.samples = int(f.get("samples", 48))
        self.horizon = int(f.get("horizon_hours", 24))
        self.venue, self.symbol = cfg["venue"], cfg["spot_symbol"]
        self.live_prices = cfg["feed"] == "live"
        self.data_dir, self.root = Path(data_dir), Path(root)
        self.py = self.root / ".venv-forecast" / "bin" / "python"
        self.ready_marker = self.root / ".venv-forecast" / ".ready"
        self.state, self.detail = ("off", "") if not self.enabled else ("starting", "")
        self.forecast: Optional[dict] = None
        self.observed: List[float] = []         # recent real hourly moves, shown until the model is up
        self.candles: List[list] = []           # recent hourly candles for the price chart
        self.history: List[list] = []           # all hourly candles held, for the strategy lab
        self.candle_error = ""
        self._mtime = 0.0
        self._proc: Optional[subprocess.Popen] = None
        self._setup: Optional[subprocess.Popen] = None
        self._setup_tried = False
        self._fails, self._next_start = 0, 0.0
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._stats = {"graded": 0, "hits": 0, "brier": None}

    # ---- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if not self.enabled:
            return
        for fn in (self._candle_loop, self._supervise_loop):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for p in (self._proc, self._setup):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()

    # ---- hourly candles for the model ------------------------------------------
    def _candle_loop(self) -> None:
        ex = None
        while not self._stop.is_set():
            if self.live_prices:
                try:
                    if ex is None:
                        from .broker_live import make_exchange
                        ex = make_exchange(self.venue)
                    rows = ex.fetch_ohlcv(self.symbol, TF, limit=300)
                    candles = [[int(r[0] // 1000)] + [float(x) for x in r[1:6]] for r in rows if None not in r[:6]]
                    if len(candles) > 10:
                        self._write("candles.json", {"symbol": self.symbol, "timeframe_s": TF_S,
                                                     "candles": candles, "written": time.time()})
                        self.candles = [c[:5] for c in candles[-96:]]
                        self.history = candles
                        closes = [c[4] for c in candles[-49:]]
                        self.observed = [round(b / a - 1, 5) for a, b in zip(closes, closes[1:])]
                except Exception as e:
                    self.candle_error = f"{type(e).__name__}: {e}"
                    ex = None
            self._stop.wait(300)

    def _write(self, name: str, obj: dict) -> None:
        tmp = self.data_dir / (name + ".tmp")
        tmp.write_text(json.dumps(obj))
        tmp.replace(self.data_dir / name)

    # ---- install and keep the model process running ----------------------------
    def _supervise_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._supervise()
            except Exception as e:
                self.state, self.detail = "error", f"{type(e).__name__}: {e}"
            self._stop.wait(5)

    def _supervise(self) -> None:
        if not self.live_prices:
            self.state, self.detail = "idle", "the forecast model needs real prices"
            return
        if not self.ready_marker.exists():
            script = self.root / "deploy" / "forecast-setup.sh"
            if self._setup is None and not self._setup_tried and script.exists() and os.name == "posix":
                self._setup_tried = True
                log = open(self.data_dir / "forecast-setup.log", "ab")
                self._setup = subprocess.Popen(["/bin/bash", str(script)], cwd=str(self.root),
                                               stdout=log, stderr=subprocess.STDOUT)
            if self._setup is not None and self._setup.poll() is None:
                self.state, self.detail = "installing", "installing the model runtime (a few minutes, one time)"
            else:
                self.state, self.detail = "unavailable", self._tail("forecast-setup.log") or "model runtime is not installed"
            return
        if self._proc is None or self._proc.poll() is not None:
            if self._proc is not None:
                self._fails += 1
                self._next_start = time.time() + min(900, 20 * 2 ** min(self._fails, 6))
                self._proc = None
            if time.time() < self._next_start:
                self.state, self.detail = "error", "model process stopped; retrying. " + self._tail("forecaster.log")
                return
            log = open(self.data_dir / "forecaster.log", "ab")
            self._proc = subprocess.Popen(
                [str(self.py), "-m", "penultimate.forecaster", "--data", str(self.data_dir), "--model", self.model,
                 "--tokenizer", self.tokenizer, "--samples", str(self.samples), "--horizon", str(self.horizon),
                 "--every", "3600"], cwd=str(self.root), stdout=log, stderr=subprocess.STDOUT)
        try:
            st = json.loads((self.data_dir / "forecaster-status.json").read_text())
            self.state, self.detail = st.get("state", "starting"), st.get("detail", "")
            if st.get("state") == "ready":
                self._fails = 0
        except (OSError, ValueError):
            self.state, self.detail = "loading", "starting the model"

    def _tail(self, name: str) -> str:
        try:
            lines = [l for l in (self.data_dir / name).read_text(errors="replace").splitlines() if l.strip()]
            return lines[-1][-200:] if lines else ""
        except OSError:
            return ""

    # ---- read forecasts, log the model's calls, grade them ----------------------
    def tick(self, price: float, now: float, store) -> List[str]:
        """Called from the trading loop every few seconds. Returns lines for the event log."""
        out: List[str] = []
        path = self.data_dir / "forecast.json"
        try:
            m = path.stat().st_mtime
            if m != self._mtime:
                self._mtime = m
                f = json.loads(path.read_text())
                if f.get("symbol") == self.symbol and f.get("closes"):
                    self.forecast = f
                    if self.is_fresh(price, now):
                        for hours in (1, f["horizon"]):
                            p_up = sum(p[hours - 1] > f["last_close"] for p in f["closes"]) / len(f["closes"])
                            cur = store.db.execute(
                                "INSERT OR IGNORE INTO model_calls(model,base_ts,horizon_s,due,base,p_up,status) "
                                "VALUES(?,?,?,?,?,?,'open')",
                                (f["model"], f["base_ts"], hours * TF_S, f["base_ts"] + TF_S + hours * TF_S,
                                 f["last_close"], p_up))
                            if cur.rowcount and hours == f["horizon"]:
                                out.append(f"new forecast: {p_up:.0%} of {len(f['closes'])} sampled paths end "
                                           f"higher in {hours}h (shadow mode, not traded)")
                        store.db.commit()
        except (OSError, ValueError, KeyError, IndexError):
            pass
        for cid, hs, due, base, p_up in store.rows(
                "SELECT id,horizon_s,due,base,p_up FROM model_calls WHERE status='open' AND due<=?", (now,)):
            if now - due > 300:
                status = "skipped"                  # the bot was off at the deadline; no fair price to grade with
            elif p_up == 0.5 or price == base:
                status = "flat"
            else:
                status = "hit" if (p_up > 0.5) == (price > base) else "miss"
            store.db.execute("UPDATE model_calls SET status=?, outcome=? WHERE id=?", (status, price, cid))
            store.db.commit()
            if status in ("hit", "miss"):
                out.append(f"forecast graded ({hs // 3600}h): called {p_up:.0%} up, price went "
                           f"{'up' if price > base else 'down'} -> {status.upper()}")
        rows = store.rows("SELECT p_up, outcome>base FROM model_calls WHERE status IN ('hit','miss')")
        hits = store.rows("SELECT count(*) FROM model_calls WHERE status='hit'")[0][0]
        self._stats = {"graded": len(rows), "hits": hits,
                       "brier": round(sum((p - y) ** 2 for p, y in rows) / len(rows), 4) if rows else None}
        return out

    def is_fresh(self, price: Optional[float], now: float) -> bool:
        f = self.forecast
        return bool(f and price and now - f["made"] < FRESH_S and abs(f["last_close"] / price - 1) < 0.1)

    def view(self, price: Optional[float], now: float) -> dict:
        f, fresh = self.forecast, self.is_fresh(price, now)
        v = {"enabled": self.enabled, "state": self.state, "detail": self.detail, "model": self.model,
             "fresh": fresh, "made": f["made"] if f else None, **self._stats,
             "hit_rate": self._stats["hits"] / self._stats["graded"] if self._stats["graded"] else None}
        if fresh:
            n = len(f["closes"])
            v["p_up"] = sum(p[-1] > f["last_close"] for p in f["closes"]) / n
            v["p_up_1h"] = sum(p[0] > f["last_close"] for p in f["closes"]) / n
            v["n"], v["horizon"], v["took_s"] = n, f["horizon"], f.get("took_s")
        return v

    def payload(self, price: Optional[float], now: float) -> dict:
        return {"forecast": self.forecast if self.is_fresh(price, now) else None, "observed": self.observed,
                "candles": self.candles, "state": self.state, "detail": self.detail}
