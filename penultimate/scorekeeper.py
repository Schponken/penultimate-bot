"""Scorekeeper: logs dated, numeric price calls from analysts and influencers, grades each one
at its deadline, and turns the track record into a small, capped tilt on position size.

Kinds of call:
  touch_above / touch_below  - price reaches the level at any point before the deadline
Only graded on real prices (never on the practice market).
"""
from __future__ import annotations

import calendar
import time
from typing import Dict, List

MAX_TILT = 0.10        # this whole layer can change size by 10% at most


def _ts(date: str) -> float:
    return float(calendar.timegm(time.strptime(date, "%Y-%m-%d")))


# Seed list from the November 2026 review of public calls. (source, kind, level, made, deadline, status, note)
# "made" dates are approximate where the reporting gave none. Three months of results is a small
# sample, which is why weights start near 0.5 and the whole layer is capped.
SEED = [
    ("Ryan Lee (Bitget)", "touch_above", 87000, "2026-08-24", "2026-10-31", "hit", "82-87K zone after $80K break; high was $87,249"),
    ("Ali Martinez", "touch_below", 82500, "2026-10-03", "2026-10-31", "hit", "called the $87K rally compromised"),
    ("Ali Martinez", "touch_below", 60000, "2026-06-10", "2026-10-31", "miss", "Sept-Oct cycle bottom at $38-60K"),
    ("Markus Thielen (10x)", "touch_above", 90000, "2026-10-29", "2026-12-31", "open", "$90K by year-end"),
    ("Markus Thielen (10x)", "touch_below", 55000, "2026-06-23", "2026-10-31", "miss", "cycle low near $55K by October"),
    ("Peter Schiff", "touch_below", 75000, "2026-10-24", "2026-12-31", "hit", "permanent bear; same call since 2025"),
    ("Peter Brandt", "touch_below", 50000, "2026-07-20", "2026-10-04", "miss", "Oct 4 bottom at $40-50K; Oct 4 was the month's high close"),
    ("Benjamin Cowen", "touch_below", 52000, "2026-08-14", "2027-01-31", "open", "Q4 bottom, illustrative floor near $44K"),
    ("Arthur Hayes", "touch_above", 126000, "2026-09-01", "2026-12-31", "open", "'foregone conclusion' by year-end"),
    ("Arthur Hayes", "touch_below", 50000, "2026-08-14", "2026-12-31", "open", "warned of $40-50K revisit, no date given"),
    ("Geoff Kendrick (StanChart)", "touch_above", 100000, "2026-09-30", "2026-12-31", "open", "said $100K may be too low"),
    ("Bernstein", "touch_above", 125000, "2026-08-26", "2026-12-31", "open", "cut from $150K"),
    ("Tom Lee", "touch_above", 150000, "2026-09-01", "2026-12-31", "open", "down from $250K"),
    ("Citi", "touch_above", 113000, "2026-10-01", "2027-10-01", "open", "raised one day before the October high"),
    ("Bitwise (Rasmussen)", "touch_above", 100000, "2026-10-18", "2026-12-31", "open", "above $100K by year-end"),
    ("Mike Alfred", "touch_above", 100000, "2026-11-05", "2026-11-26", "open", "$100K by Thanksgiving"),
    ("Robert Kiyosaki", "touch_above", 250000, "2025-11-09", "2026-12-31", "open", "$250K in 2026"),
    ("Samson Mow", "touch_above", 1330000, "2026-01-10", "2026-12-31", "open", "$1.33M in 2026"),
]


class Scorekeeper:
    def __init__(self, store, symbol: str):
        self.store, self.symbol = store, symbol
        if not store.rows("SELECT 1 FROM calls LIMIT 1"):
            for src, kind, level, made, dl, status, note in SEED:
                self.add(src, kind, level, made, dl, status, note)

    def add(self, source: str, kind: str, level: float, made: str, deadline: str,
            status: str = "open", note: str = "") -> None:
        assert kind in ("touch_above", "touch_below")
        self.store.db.execute(
            "INSERT INTO calls(source,kind,level,symbol,made,deadline,status,resolved,note) VALUES(?,?,?,?,?,?,?,?,?)",
            (source, kind, float(level), self.symbol, _ts(made), _ts(deadline), status, None, note))
        self.store.db.commit()

    def on_price(self, price: float, ts: float) -> List[str]:
        """Grade open calls against a real price. Returns human-readable results."""
        out = []
        for cid, src, kind, level, deadline in self.store.rows(
                "SELECT id,source,kind,level,deadline FROM calls WHERE status='open'"):
            reached = price >= level if kind == "touch_above" else price <= level
            status = "hit" if reached and ts <= deadline + 86400 else ("miss" if ts > deadline + 86400 else None)
            if status:
                self.store.db.execute("UPDATE calls SET status=?, resolved=? WHERE id=?", (status, ts, cid))
                self.store.db.commit()
                out.append(f"{src}: {kind.replace('_', ' ')} ${level:,.0f} -> {status.upper()}")
        return out

    def stats(self) -> Dict[str, dict]:
        s: Dict[str, dict] = {}
        for src, status in self.store.rows("SELECT source,status FROM calls"):
            d = s.setdefault(src, {"hit": 0, "miss": 0, "open": 0})
            d[status] += 1
        for d in s.values():
            n = d["hit"] + d["miss"]
            d["weight"] = (d["hit"] + 1) / (n + 2)      # pulled toward 0.5 until there is real evidence
        return s

    def tilt(self, price: float) -> float:
        """Between -0.10 and +0.10. Positive = the better-scored sources lean upward."""
        stats, total, n = self.stats(), 0.0, 0
        for src, kind, level in self.store.rows("SELECT source,kind,level FROM calls WHERE status='open'"):
            direction = 1 if kind == "touch_above" and level > price else (-1 if kind == "touch_below" and level < price else 0)
            total += (stats[src]["weight"] - 0.5) * 2 * direction
            n += 1
        return max(-MAX_TILT, min(MAX_TILT, (total / n) * MAX_TILT * 4)) if n else 0.0

    def table(self) -> List[dict]:
        st = self.stats()
        rows = [{"source": k, **v} for k, v in st.items()]
        return sorted(rows, key=lambda r: (-r["weight"], r["source"]))

    def open_calls(self) -> List[dict]:
        return [{"source": a, "kind": b, "level": c, "deadline": time.strftime("%b %d", time.gmtime(d)), "note": e}
                for a, b, c, d, e in self.store.rows(
                    "SELECT source,kind,level,deadline,note FROM calls WHERE status='open' ORDER BY deadline")]
