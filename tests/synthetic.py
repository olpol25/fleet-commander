"""
Minimal synthetic ScreenPipe data builders for tests/test_brief.py.

Detectors in brief.py take a list of (ts, app, window) tuples. These helpers
build such lists for specific scenarios, plus a build_db() that writes them
to a ScreenPipe-shaped SQLite file for integration tests.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


FRAME_S = 5.0  # ScreenPipe samples at 0.2 fps


def session(start: datetime, duration_s: float, app: str, window: str,
            sample_every_s: float = FRAME_S):
    """Continuous activity in one (app, window) sampled every FRAME_S seconds."""
    rows = []
    t = start
    end = start + timedelta(seconds=duration_s)
    while t < end:
        rows.append((t, app, window))
        t = t + timedelta(seconds=sample_every_s)
    return rows


def switch_storm(start: datetime, apps: list[str], titles: list[str] | None,
                 switches: int, total_seconds: float = 120.0):
    """`switches` app changes spread evenly across `total_seconds`."""
    if switches < 1:
        return []
    step = total_seconds / switches
    titles = titles or [f"{a} window" for a in apps]
    rows, t = [], start
    for i in range(switches + 1):
        rows.append((t, apps[i % len(apps)], titles[i % len(titles)]))
        t = t + timedelta(seconds=step)
    return rows


def weekly_recurring_meeting(anchor_monday: datetime, weeks: int,
                             dow: int, hour: int, duration_min: int,
                             app: str, window: str):
    """Same meeting on the same weekday for N consecutive weeks."""
    rows = []
    for w in range(weeks):
        day = anchor_monday + timedelta(weeks=w, days=dow)
        start = day.replace(hour=hour, minute=0, second=0, microsecond=0)
        rows.extend(session(start, duration_min * 60, app, window))
    return rows


def build_db(path: Path, rows: Iterable[tuple[datetime, str, str]]):
    """Write rows to a fresh ScreenPipe-shaped SQLite db at `path`."""
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE frames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL
        );
        CREATE TABLE ocr_text (
            frame_id INTEGER NOT NULL,
            app_name TEXT,
            window_name TEXT,
            FOREIGN KEY(frame_id) REFERENCES frames(id)
        );
        """
    )
    for ts, app, window in rows:
        cur.execute("INSERT INTO frames (timestamp) VALUES (?)",
                    (ts.strftime("%Y-%m-%d %H:%M:%S"),))
        frame_id = cur.lastrowid
        cur.execute(
            "INSERT INTO ocr_text (frame_id, app_name, window_name) VALUES (?, ?, ?)",
            (frame_id, app, window),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Convenience anchors
# ---------------------------------------------------------------------------

def monday_at(year: int, month: int, day: int, hour: int = 9) -> datetime:
    """Build a tz-aware datetime; assert the date is a Monday."""
    d = datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)
    assert d.weekday() == 0, f"{d.date()} is not a Monday"
    return d
