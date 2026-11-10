"""SQLite log of everything the bot does, so any decision can be explained afterwards."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(ts REAL, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS fills(ts REAL, symbol TEXT, side TEXT, qty REAL, price REAL, fee REAL,
                                 strategy TEXT, tag TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS equity(ts REAL, equity REAL, price REAL);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY, source TEXT, kind TEXT, level REAL, symbol TEXT,
                                 made REAL, deadline REAL, status TEXT, resolved REAL, note TEXT);
CREATE TABLE IF NOT EXISTS model_calls(id INTEGER PRIMARY KEY, model TEXT, base_ts REAL, horizon_s REAL, due REAL,
                                       base REAL, p_up REAL, status TEXT, outcome REAL,
                                       UNIQUE(model, base_ts, horizon_s));
"""


class Store:
    def __init__(self, path: Optional[Path] = None):
        self.path = str(path) if path else ":memory:"
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self._n = 0

    def _maybe_commit(self, force: bool = False) -> None:
        self._n += 1
        if force or self._n % 25 == 0:
            self.db.commit()

    def event(self, ts: float, kind: str, detail: str) -> None:
        self.db.execute("INSERT INTO events VALUES(?,?,?)", (ts, kind, detail))
        self._maybe_commit(force=True)

    def fill(self, f, mode: str) -> None:
        self.db.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?)",
                        (f.ts, f.symbol, f.side, f.qty, f.price, f.fee, f.strategy, f.tag, mode))
        self._maybe_commit(force=True)

    def equity(self, ts: float, equity: float, price: float) -> None:
        self.db.execute("INSERT INTO equity VALUES(?,?,?)", (ts, equity, price))
        self._maybe_commit()

    def set(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value)))
        self.db.commit()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def rows(self, sql: str, args: tuple = ()) -> List[tuple]:
        return self.db.execute(sql, args).fetchall()

    def close(self) -> None:
        self.db.commit()
        self.db.close()
