"""Start the bot:  python -m penultimate
Keeps itself running: if the engine hits an error it cancels resting orders, waits, and starts again."""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
import webbrowser

from .config import ROOT, load_config, load_limits
from .dashboard import Dash
from .engine import Engine
from .forecast import ForecastHub
from .lab import Lab
from .updater import Updater


def main() -> None:
    ap = argparse.ArgumentParser(prog="penultimate")
    ap.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    ap.add_argument("--ticks", type=int, default=0, help="stop after this many price updates (testing)")
    ap.add_argument("--no-dashboard", action="store_true")
    args = ap.parse_args()

    dash = None
    if not args.no_dashboard:
        d = load_config().get("dashboard", {})
        dash = Dash(int(d.get("port", 8787)), d.get("host", "127.0.0.1"), str(d.get("password", "") or ""), d.get("hosts") or ())
        dash.start()
        url = f"http://127.0.0.1:{dash.port}/"
        print(f"Penultimate dashboard: {url}")
        if args.open:
            webbrowser.open(url)

    backoff = 1.0
    hub, hub_key, lab = None, None, None
    (ROOT / "data").mkdir(exist_ok=True)
    upd = Updater(load_config(), ROOT, ROOT / "data")      # off unless config.yaml has an update section
    upd.start()
    if dash:
        dash.updater = upd
    while True:
        eng = None
        try:
            cfg = load_config()
            upd.mode = cfg["mode"]
            key = (cfg["feed"], cfg["venue"], cfg["spot_symbol"], str(cfg.get("forecast")))
            if key != hub_key:                  # forecast plumbing follows the price source
                if hub:
                    hub.stop()
                if lab:
                    lab.stop()
                hub, hub_key = ForecastHub(cfg, ROOT / "data", ROOT), key
                (ROOT / "data").mkdir(exist_ok=True)
                hub.start()
                lab = Lab(cfg, hub, load_limits())              # shadow-only strategy lab; never trades
                lab.start()
                if dash:
                    dash.lab = lab
            eng = Engine(cfg, load_limits(), hub=hub)
            if dash:
                dash.engine, dash.crash = eng, ""
            eng.run(max_ticks=args.ticks or None)
            if eng.reexec:                      # updated files on disk: replace this process with a fresh one
                eng.store.close()
                hub.stop()
                lab.stop()
                upd.stop()
                if dash and dash.httpd:
                    dash.httpd.server_close()
                os.execv(sys.executable, [sys.executable, "-m", "penultimate"] + sys.argv[1:])
            if eng.exit or args.ticks:
                break
            backoff = 1.0                       # a clean restart (mode switch)
        except KeyboardInterrupt:
            if eng:
                eng.save_state()
            break
        except Exception as e:                  # self-heal: log, pull orders, retry with backoff
            (ROOT / "data").mkdir(exist_ok=True)
            with open(ROOT / "data" / "crash.log", "a") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc() + "\n")
            if eng:
                try:
                    eng.broker.cancel_all()
                    eng.save_state()
                except Exception:
                    pass
            if dash:
                dash.engine, dash.crash = None, f"{type(e).__name__}: {e}"
            if args.ticks:
                raise
            time.sleep(backoff)
            backoff = min(60.0, backoff * 2)
        finally:
            if eng:
                try:
                    eng.store.close()
                except Exception:
                    pass
    if hub:
        hub.stop()


if __name__ == "__main__":
    main()
