"""
Test harness for brief.py pattern detectors.

Run:  python -m unittest tests.test_brief -v
"""

from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make the project root importable so we can `import brief`.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import brief  # noqa: E402
from tests import synthetic as syn  # noqa: E402


# ---------------------------------------------------------------------------
# find_transition_cost_events
# ---------------------------------------------------------------------------

class TransitionEventTests(unittest.TestCase):

    def test_no_events_when_switches_evenly_spaced(self):
        """5 switches across an hour is one every 12min — must not flag a 2-min window."""
        start = syn.monday_at(2026, 5, 4)
        apps = ["Slack", "Notion", "Linear", "Chrome", "Slack", "Notion"]
        rows = []
        for i, a in enumerate(apps):
            rows.append((start + timedelta(minutes=12 * i), a, f"{a} win"))
        events = brief.find_transition_cost_events(rows)
        self.assertEqual(events, [], "evenly-spaced switches should not flag a storm")

    def test_flags_dense_storm(self):
        """6 switches in 90 seconds is unambiguously a storm."""
        start = syn.monday_at(2026, 5, 4)
        rows = syn.switch_storm(start, ["Slack", "Notion", "Linear"], None,
                                switches=6, total_seconds=90)
        events = brief.find_transition_cost_events(rows)
        self.assertEqual(len(events), 1)
        self.assertGreaterEqual(events[0]["count"], 1)

    def test_below_threshold_not_flagged(self):
        """4 switches in 90s is below the 5-switch threshold."""
        start = syn.monday_at(2026, 5, 4)
        rows = syn.switch_storm(start, ["Slack", "Notion"], None,
                                switches=4, total_seconds=90)
        events = brief.find_transition_cost_events(rows)
        self.assertEqual(events, [])

    def test_single_storm_counted_once(self):
        """A single 90s storm should produce exactly one event, not many overlapping ones."""
        start = syn.monday_at(2026, 5, 4)
        rows = syn.switch_storm(start, ["Slack", "Notion", "Linear"], None,
                                switches=10, total_seconds=90)
        events = brief.find_transition_cost_events(rows)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["count"], 1,
                         "one storm should be counted once, not duplicated by overlap")


# ---------------------------------------------------------------------------
# find_repeated_sequences
# ---------------------------------------------------------------------------

class RepeatedSequenceTests(unittest.TestCase):

    def test_four_week_monday_meeting_flagged(self):
        anchor = syn.monday_at(2026, 4, 13)  # 4 Mondays: 4/13, 4/20, 4/27, 5/4
        rows = syn.weekly_recurring_meeting(
            anchor_monday=anchor, weeks=4, dow=0, hour=9,
            duration_min=30, app="Notion", window="Sprint Planning — Q2",
        )
        seqs = brief.find_repeated_sequences(rows)
        self.assertTrue(any(s["app"] == "Notion" and "Sprint Planning" in s["title_pattern"]
                            for s in seqs),
                        f"expected weekly Monday meeting to surface; got {seqs}")

    def test_two_week_pattern_not_flagged(self):
        anchor = syn.monday_at(2026, 4, 27)
        rows = syn.weekly_recurring_meeting(
            anchor_monday=anchor, weeks=2, dow=0, hour=9,
            duration_min=30, app="Notion", window="Sprint Planning",
        )
        seqs = brief.find_repeated_sequences(rows)
        self.assertEqual(seqs, [], "2 weeks should not pass the 3-week threshold")

    def test_calendar_week_boundary(self):
        """
        REGRESSION: weeks_ago uses (now-ts).days // 7, a rolling bucket.
        A pattern that lands on Mon morning each of 4 weeks should still be
        bucketed into 4 distinct weeks even when 'now' is set to a Friday.
        """
        # Anchor: meeting every Monday 9am for 4 weeks
        first_monday = syn.monday_at(2026, 4, 13)
        rows = []
        for w in range(4):
            day = first_monday + timedelta(weeks=w)
            rows.extend(syn.session(day.replace(hour=9), 1800, "Notion", "Sprint Planning"))
        # "Now" is Friday of week 4 (May 8, 2026)
        rows.append((datetime(2026, 5, 8, 17, 0, tzinfo=timezone.utc),
                     "Chrome", "totally unrelated"))
        seqs = brief.find_repeated_sequences(rows)
        sprint = [s for s in seqs if "Sprint Planning" in s["title_pattern"]]
        self.assertTrue(sprint, "Monday meeting should still be detected when 'now' is Friday")
        self.assertGreaterEqual(sprint[0]["weeks_observed"], 3)

    def test_different_dow_not_collapsed(self):
        """Same title on Mon and Tue is not the same pattern."""
        first_monday = syn.monday_at(2026, 4, 13)
        rows = []
        for w in range(4):
            mon = first_monday + timedelta(weeks=w)
            tue = mon + timedelta(days=1)
            rows.extend(syn.session(mon.replace(hour=9), 600, "Notion", "Planning"))
            rows.extend(syn.session(tue.replace(hour=9), 600, "Notion", "Planning"))
        seqs = brief.find_repeated_sequences(rows)
        days = {s["day"] for s in seqs if s["title_pattern"] == "Planning"}
        self.assertIn("Mon", days)
        self.assertIn("Tue", days)


# ---------------------------------------------------------------------------
# find_focus_fragmentation
# ---------------------------------------------------------------------------

class FocusFragmentationTests(unittest.TestCase):

    def test_pure_focus_block(self):
        start = syn.monday_at(2026, 5, 4)
        rows = syn.session(start, 3600, "VSCode", "main.py")
        frag = brief.find_focus_fragmentation(rows)
        self.assertEqual(len(frag), 1)
        self.assertGreaterEqual(frag[0]["longest_block_min"], 55)
        self.assertLessEqual(frag[0]["longest_block_min"], 61)

    def test_brief_glance_does_not_break_block(self):
        """One Slack frame spliced into a continuous VSCode hour — a real 5s glance at 0.2 fps."""
        start = syn.monday_at(2026, 5, 4)
        rows = syn.session(start, 3600, "VSCode", "main.py")
        glance_offset = timedelta(minutes=30)
        for i, (ts, _, _) in enumerate(rows):
            if ts - start == glance_offset:
                rows[i] = (ts, "Slack", "DMs")
                break
        frag = brief.find_focus_fragmentation(rows)
        self.assertEqual(len(frag), 1)
        self.assertGreaterEqual(frag[0]["longest_block_min"], 55,
                                "5s glance should not split a 60min focus block")

    def test_long_interruption_breaks_block(self):
        """5 minutes in another app should break the focus block."""
        start = syn.monday_at(2026, 5, 4)
        rows = []
        rows.extend(syn.session(start, 1800, "VSCode", "main.py"))
        rows.extend(syn.session(start + timedelta(minutes=30), 300, "Slack", "channel"))
        rows.extend(syn.session(start + timedelta(minutes=35), 1800, "VSCode", "main.py"))
        frag = brief.find_focus_fragmentation(rows)
        # Each VSCode block is 30 min; longest should be ~30, not ~60
        self.assertLess(frag[0]["longest_block_min"], 35,
                        "5min interruption should split block; longest should be ~30")

    def test_initial_blip_does_not_anchor_focus(self):
        """
        REGRESSION: a 5s App A appearance followed by 2 hours of App B
        should attribute those 2 hours to App B, not still be 'in an A block'
        waiting for A to disappear.
        """
        start = syn.monday_at(2026, 5, 4)
        rows = [(start, "Mail", "Inbox")]  # single 5s frame
        rows.extend(syn.session(start + timedelta(seconds=10), 7200, "VSCode", "main.py"))
        frag = brief.find_focus_fragmentation(rows)
        # Longest block should be the 2h VSCode session, not contaminated by Mail
        self.assertGreaterEqual(frag[0]["longest_block_min"], 115,
                                "2h focus block should not be split by an initial 5s Mail blip")


# ---------------------------------------------------------------------------
# Edge cases: sleep gaps, weekends, midnight spans, sparse days
# ---------------------------------------------------------------------------

class EdgeCaseTests(unittest.TestCase):

    def test_sleep_gap_breaks_focus_block(self):
        """30min coding, 2h laptop sleep, 30min coding — two 30min blocks, not one 60min block."""
        start = syn.monday_at(2026, 5, 4)
        rows = syn.session(start, 1800, "VSCode", "main.py")
        resume = start + timedelta(seconds=1800) + timedelta(hours=2)
        rows += syn.session(resume, 1800, "VSCode", "main.py")
        frag = brief.find_focus_fragmentation(rows)
        self.assertEqual(len(frag), 1)
        self.assertLess(frag[0]["longest_block_min"], 35,
                        "a 2h idle gap must break the block even though the app is unchanged")
        self.assertGreaterEqual(frag[0]["longest_block_min"], 25)

    def test_weekend_recurring_pattern_detected(self):
        """A Saturday-morning routine repeated 4 weeks should surface like any weekday one."""
        first_saturday = syn.monday_at(2026, 4, 13) + timedelta(days=5)
        rows = []
        for w in range(4):
            day = first_saturday + timedelta(weeks=w)
            rows.extend(syn.session(day.replace(hour=10), 1200, "Notion", "Weekly Review"))
        seqs = brief.find_repeated_sequences(rows)
        sat = [s for s in seqs if s["day"] == "Sat" and "Weekly Review" in s["title_pattern"]]
        self.assertTrue(sat, f"Saturday routine should be detected; got {seqs}")

    def test_midnight_spanning_block_split_by_day(self):
        """A focus block crossing midnight is reported per calendar day, not as one block."""
        start = syn.monday_at(2026, 5, 4).replace(hour=23)
        rows = syn.session(start, 7200, "VSCode", "main.py")  # 23:00 -> 01:00
        frag = brief.find_focus_fragmentation(rows)
        self.assertEqual(len(frag), 2, "block crossing midnight should appear on two days")
        for day in frag:
            self.assertGreater(day["longest_block_min"], 0)

    def test_sparse_day_does_not_crash(self):
        """A day with only a handful of frames should produce sane output, not an error."""
        start = syn.monday_at(2026, 5, 4)
        rows = [
            (start, "Mail", "Inbox"),
            (start + timedelta(seconds=5), "Mail", "Inbox"),
            (start + timedelta(seconds=10), "Chrome", "github.com"),
        ]
        frag = brief.find_focus_fragmentation(rows)
        events = brief.find_transition_cost_events(rows)
        seqs = brief.find_repeated_sequences(rows)
        self.assertEqual(len(frag), 1)
        self.assertEqual(events, [])
        self.assertEqual(seqs, [])


# ---------------------------------------------------------------------------
# Integration: full pipeline through load_rows
# ---------------------------------------------------------------------------

class LoadRowsIntegrationTests(unittest.TestCase):

    def test_round_trip_through_sqlite(self):
        """Build a DB, load rows back, and verify pattern detection still works."""
        start = syn.monday_at(2026, 4, 13)
        rows = syn.weekly_recurring_meeting(
            anchor_monday=start, weeks=4, dow=0, hour=9,
            duration_min=30, app="Notion", window="Sprint Planning — Q2",
        )
        # Add a recent ping so 'days=7' query window includes data
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        rows.append((recent, "Chrome", "github.com"))

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "db.sqlite"
            syn.build_db(db_path, rows)
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            # load_rows uses `days` to filter — pick a window big enough to
            # include the synthetic April data
            loaded = brief.load_rows(conn, days=40)
            conn.close()

        self.assertGreater(len(loaded), 100, "round trip should preserve session rows")
        apps = {r[1] for r in loaded}
        self.assertIn("Notion", apps)

    def test_overlay_apps_filtered(self):
        start = syn.monday_at(2026, 5, 4)
        rows = syn.session(start, 600, "VSCode", "main.py")
        rows.append((start + timedelta(seconds=30), "Dock", "Dock"))
        rows.append((start + timedelta(seconds=60), "Control Centre", "glance"))
        rows.sort(key=lambda r: r[0])

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "db.sqlite"
            syn.build_db(db_path, rows)
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            loaded = brief.load_rows(conn, days=400)  # wide window to bypass time filter
            conn.close()

        apps = {r[1] for r in loaded}
        self.assertNotIn("Dock", apps)
        self.assertNotIn("Control Centre", apps)
        self.assertIn("VSCode", apps)


if __name__ == "__main__":
    unittest.main(verbosity=2)
