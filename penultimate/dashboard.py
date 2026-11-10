"""Local dashboard: one page that shows what the bot is doing and holds the few manual controls.
Binds to this computer only unless a password is set."""
from __future__ import annotations

import base64
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import ROOT

PAGE = (Path(__file__).parent / "dashboard.html").read_text()
LOCAL = ("127.0.0.1", "::1", "localhost")


def set_mode(mode: str, feed: str, path: Path = ROOT / "config.yaml") -> None:
    text = path.read_text()
    text = re.sub(r"(?m)^mode:\s*\w+", f"mode: {mode}", text, count=1)
    text = re.sub(r"(?m)^feed:\s*\w+", f"feed: {feed}", text, count=1)
    path.write_text(text)


class Dash:
    def __init__(self, port: int = 8787, host: str = "127.0.0.1", password: str = ""):
        if host not in LOCAL and not password:
            host = "127.0.0.1"          # never expose controls to a network without a password
        self.host, self.port, self.password = host, port, password
        self.token = secrets.token_urlsafe(16)
        self.engine = None
        self.updater = None
        self.lab = None
        self.crash = ""
        self.httpd = None

    def start(self) -> None:
        dash = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):   # keep the console quiet
                pass

            def _auth(self) -> bool:
                if not dash.password:
                    return True
                want = "Basic " + base64.b64encode(f"owner:{dash.password}".encode()).decode()
                if secrets.compare_digest(self.headers.get("Authorization", ""), want):
                    return True
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Penultimate"')
                self.end_headers()
                return False

            def _send(self, code: int, body, ctype: str = "application/json") -> None:
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if not self._auth():
                    return
                if self.path == "/":
                    return self._send(200, PAGE.replace("__TOKEN__", dash.token), "text/html")
                if self.path == "/api/state":
                    return self._send(200, dash.state())
                if self.path == "/api/forecast":
                    return self._send(200, dash.forecast())
                if self.path == "/api/lab":
                    return self._send(200, dash.lab.view() if dash.lab else {"state": "off", "gens": []})
                if self.path.startswith("/api/lab?v="):
                    d = dash.lab.variant(self.path.split("=", 1)[1][:12]) if dash.lab else None
                    return self._send(200 if d else 404, d or {"error": "that variant is no longer kept"})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._auth():
                    return
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                except Exception:
                    return self._send(400, {"error": "bad request"})
                if not secrets.compare_digest(str(body.get("token", "")), dash.token):
                    return self._send(403, {"error": "reload the page and try again"})
                ok, msg = dash.command(body, self.client_address[0])
                self._send(200 if ok else 400, {"ok": ok, "message": msg})

        self.httpd = ThreadingHTTPServer((self.host, self.port), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    # ---- data ---------------------------------------------------------------
    def state(self) -> dict:
        from .broker_live import KEYS_FILE
        eng = self.engine
        if eng is None:
            return {"ready": False, "crash": self.crash}
        with eng.lock:
            snap = dict(eng.snap)
        snap["keys_saved"] = KEYS_FILE.exists()
        snap["crash"] = self.crash
        snap["update"] = self.updater.view() if self.updater else {"enabled": False}
        return snap

    def forecast(self) -> dict:
        eng = self.engine
        if eng is None or eng.hub is None:
            return {"forecast": None, "observed": [], "candles": [], "state": "off", "detail": ""}
        return eng.hub.payload(eng.marks.get(eng.spot), eng.feed.now())

    # ---- controls -----------------------------------------------------------
    def command(self, body: dict, client: str):
        eng, cmd = self.engine, body.get("cmd")
        if eng is None:
            return False, "the engine is restarting, try again in a moment"
        if cmd in ("stop_all", "resume", "reset_paper"):
            eng.commands.put((cmd, None))
            return True, cmd
        if cmd in ("update", "update_check"):
            if not (self.updater and self.updater.enabled):
                return False, "updates from a repository are not set up on this machine"
            self.updater.request(install=cmd == "update")
            return True, "installing the new version" if cmd == "update" else "checking for a new version"
        if cmd == "risk_off":
            eng.commands.put(("risk_off", bool(body.get("on"))))
            return True, "risk-off updated"
        if cmd == "keys":
            if client not in LOCAL:
                return False, "keys can only be entered on the machine the bot runs on"
            from .broker_live import save_keys
            try:
                save_keys(str(body.get("apiKey", "")), str(body.get("secret", "")), str(body.get("password", "")).strip())
            except ValueError as e:
                return False, str(e)
            if eng.mode == "live":
                eng.commands.put(("restart", None))
            return True, "keys saved on this machine"
        if cmd == "forget_keys":
            from .broker_live import KEYS_FILE
            KEYS_FILE.unlink(missing_ok=True)
            return True, "keys removed"
        if cmd == "mode":
            target = body.get("target")
            if target == "practice":
                set_mode("paper", "sim")
            elif target == "paper_live":
                set_mode("paper", "live")
            elif target == "live":
                from .broker_live import KEYS_FILE
                if not KEYS_FILE.exists():
                    return False, "save your exchange keys first"
                if body.get("confirm") != "GO LIVE":
                    return False, "type GO LIVE to confirm real-money trading"
                set_mode("live", "live")
            else:
                return False, "unknown mode"
            eng.commands.put(("restart", None))
            return True, f"switching to {target}"
        return False, "unknown command"
