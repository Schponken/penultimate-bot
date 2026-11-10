"""Keeps a server copy of the bot up to date from its GitHub repository.

Off unless config.yaml has an `update:` section (the server installer adds one; a copy on your own
computer has none). It checks the repository every few minutes. A new version is downloaded, checked
(it must unpack cleanly, compile and import) and only then copied over the running files, after which
the bot restarts itself. config.yaml, limits.yaml and the data folder are never touched by an update.

auto: practice (default) installs by itself while the bot is on fake money and waits for a click on the
dashboard once real money is on; `always` never waits; `never` always waits.
"""
from __future__ import annotations

import io
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

TAKE = ("penultimate", "deploy", "requirements.txt", "README.md")     # what an update replaces
NEED = ("penultimate/__init__.py", "penultimate/__main__.py", "penultimate/engine.py", "penultimate/dashboard.html")


class Updater:
    def __init__(self, cfg: dict, root: Path, data_dir: Path):
        u = cfg.get("update") or {}
        self.repo = str(u.get("repo") or "")
        self.branch = str(u.get("branch") or "main")
        self.auto = str(u.get("auto") or "practice")
        self.every = max(120, int(u.get("check_seconds") or 600))
        self.enabled = bool(self.repo) and not os.environ.get("PENULTIMATE_NO_UPDATE")
        self.root, self.data_dir = Path(root), Path(data_dir)
        self.mode = cfg.get("mode", "paper")
        self.current = self._read("installed.sha")
        self.latest, self.message, self.error, self.state = "", "", "", "idle"
        self.checked = 0.0
        self._bad = ""                      # a version that failed its checks; not retried until a newer one appears
        self._want = False
        self._wake, self._stop = threading.Event(), threading.Event()

    # ---- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self.enabled:
            threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def request(self, install: bool) -> None:
        """From the dashboard: check now, and install now if asked."""
        self._want = self._want or install
        if install:
            self._bad = ""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.one_pass()
            except Exception as e:
                self.state, self.error = "error", f"{type(e).__name__}: {e}"[:200]
            self._want = False
            self._wake.wait(self.every)
            self._wake.clear()

    def one_pass(self) -> None:
        self.check()
        go = self._want or self.auto == "always" or (self.auto == "practice" and self.mode == "paper")
        if self.available and go and self.latest != self._bad:
            self.apply(self.latest)

    # ---- talking to GitHub -----------------------------------------------------
    def _read(self, name: str) -> str:
        try:
            return (self.data_dir / name).read_text().strip()
        except OSError:
            return ""

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "penultimate-bot"})
        token = self._read("github-token")          # only needed while the repository is private
        if token:
            req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    def fetch_latest(self):
        d = json.loads(self._get(f"https://api.github.com/repos/{self.repo}/commits/{self.branch}"))
        return d["sha"], (d.get("commit", {}).get("message") or "").strip().splitlines()[0][:120]

    def fetch_tar(self, sha: str) -> bytes:
        return self._get(f"https://api.github.com/repos/{self.repo}/tarball/{sha}")

    # ---- check and install -----------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(self.latest and self.current and self.latest != self.current)

    def check(self) -> None:
        self.latest, self.message = self.fetch_latest()
        self.checked = time.time()
        if not self.current:                # a fresh install that did not record its version: assume it is current
            self.current = self.latest
            (self.data_dir / "installed.sha").write_text(self.current)
        if self.state != "installing" and not (self.state == "error" and self.latest == self._bad):
            self.state, self.error = "idle", ""

    def apply(self, sha: str) -> None:
        self.state, self.error = "installing", ""
        tmp = Path(tempfile.mkdtemp(prefix="update-", dir=self.data_dir))
        try:
            src = self._unpack_and_check(self.fetch_tar(sha), tmp)
            old_req = (self.root / "requirements.txt").read_bytes() if (self.root / "requirements.txt").exists() else b""
            for name in TAKE:
                a = src / name
                if a.is_file():
                    self._put(a, self.root / name)
                elif a.is_dir():
                    for f in a.rglob("*"):
                        if f.is_file() and "__pycache__" not in f.parts:
                            self._put(f, self.root / f.relative_to(src))
            if (self.root / "requirements.txt").read_bytes() != old_req:
                subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "-r",
                                str(self.root / "requirements.txt")], timeout=900, check=True, capture_output=True)
            (self.data_dir / "installed.sha").write_text(sha)
            self.current = sha
            (self.data_dir / "restart.flag").write_text(sha)       # the trading loop restarts on the new files
        except Exception as e:
            self._bad = sha
            self.state, self.error = "error", f"version {sha[:7]} was not installed: {type(e).__name__}: {e}"[:240]
            return
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _put(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        part = dst.with_name(dst.name + ".new")
        shutil.copyfile(src, part)
        shutil.copymode(src, part)
        part.replace(dst)

    @staticmethod
    def _unpack_and_check(raw: bytes, tmp: Path) -> Path:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            for m in tar.getmembers():
                parts = Path(m.name).parts
                if m.name.startswith("/") or ".." in parts or not (m.isfile() or m.isdir()):
                    raise ValueError(f"unsafe entry in the download: {m.name}")
            tar.extractall(tmp, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(tmp)
        kids = [p for p in tmp.iterdir()]
        src = kids[0] if len(kids) == 1 and kids[0].is_dir() and not (tmp / "penultimate").exists() else tmp
        for need in NEED:
            if not (src / need).is_file():
                raise ValueError(f"the download is missing {need}")
        for py in (src / "penultimate").rglob("*.py"):
            py_compile.compile(str(py), doraise=True)
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PENULTIMATE_NO_UPDATE": "1"}
        r = subprocess.run([sys.executable, "-c", "import penultimate.engine, penultimate.dashboard, penultimate.__main__"],
                           cwd=str(src), env=env, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise ValueError("the new version does not start: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1])
        return src

    def view(self) -> dict:
        return {"enabled": self.enabled, "repo": self.repo, "current": self.current[:7], "latest": self.latest[:7],
                "available": self.available, "message": self.message, "state": self.state, "error": self.error,
                "auto": self.auto, "checked": self.checked}
