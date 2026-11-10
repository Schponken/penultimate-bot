"""Menu bar item for the Mac: the account value at the top of the screen, and the whole dashboard one click below it.

The bot builds the small native app itself (from menubar.swift, with the compiler that ships with Apple's
command line tools) and keeps it running. Nothing to install and nothing to type. On anything that is not
a Mac, or without the compiler, this does nothing at all. It never trades; it only shows the dashboard.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import threading
import time
from pathlib import Path

NAME = "PenultimateBar"
PLIST = {"CFBundleIdentifier": "com.penultimate.menubar", "CFBundleName": "Penultimate", "CFBundleExecutable": NAME,
         "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1", "CFBundleVersion": "1", "LSUIElement": True,
         "LSMinimumSystemVersion": "11.0",
         "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True}}     # it only ever loads the bot on this computer


class MenuBar:
    def __init__(self, root: Path, data_dir: Path, port: int):
        self.src = Path(root) / "penultimate" / "menubar.swift"
        self.data, self.port = Path(data_dir), int(port)
        self.app = self.data / f"{NAME}.app"
        self.bin = self.app / "Contents" / "MacOS" / NAME
        self.off = self.data / "menubar.off"
        self.log = self.data / "menubar.log"
        self.enabled = sys.platform == "darwin" and self.src.exists() and not os.environ.get("PENULTIMATE_NO_MENUBAR")
        self._failed = 0.0                  # source time-stamp of a build that did not compile; not retried until the file changes
        self._stop = threading.Event()

    def start(self) -> None:
        if not self.enabled:
            return
        try:
            self.off.unlink()               # "hide until the bot restarts": the bot has restarted
        except OSError:
            pass
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _say(self, text: str) -> None:
        try:
            with open(self.log, "a") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text.rstrip() + "\n")
        except OSError:
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.ensure()
            except Exception as e:          # the menu bar must never disturb the trading loop
                self._say(f"error: {type(e).__name__}: {e}")
            self._stop.wait(15)

    def write_bundle(self) -> None:
        (self.app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
        with open(self.app / "Contents" / "Info.plist", "wb") as f:
            plistlib.dump(PLIST, f)

    def running(self) -> bool:
        return subprocess.run(["/usr/bin/pgrep", "-f", f"{NAME}.app/Contents/MacOS/{NAME}"], capture_output=True).returncode == 0

    def ensure(self) -> None:
        if self.off.exists():
            return
        stamp = self.src.stat().st_mtime
        if not self.bin.exists() or self.bin.stat().st_mtime < stamp:
            if stamp == self._failed:
                return
            if subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True).returncode != 0:
                self._failed = stamp
                self._say("Apple's command line tools are not installed, so the menu bar item cannot be built")
                return
            self.write_bundle()
            tmp = self.bin.with_name(NAME + ".new")
            r = subprocess.run(["/usr/bin/swiftc", "-O", str(self.src), "-o", str(tmp)], capture_output=True, text=True, timeout=600)
            if r.returncode != 0 or not tmp.exists():
                self._failed = stamp
                self._say("build failed:\n" + (r.stderr or r.stdout)[-3000:])
                return
            subprocess.run(["/usr/bin/pkill", "-f", f"{NAME}.app/Contents/MacOS/{NAME}"], capture_output=True)
            time.sleep(0.5)
            tmp.replace(self.bin)
            self._say("built")
        if not self.running():
            with open(self.log, "a") as log:
                subprocess.Popen([str(self.bin), str(self.port), str(self.data)], stdout=log, stderr=log,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
            self._say("started")
