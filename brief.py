#!/usr/bin/env python3
"""
Fleet Commander — brief.py
Watches your screen activity and produces a weekly advisor-style brief.

Usage:
  python brief.py               # generate this week's brief
  python brief.py --dry-run     # print extracted patterns, no API call
  python brief.py --explain     # show raw evidence for each automation candidate
  python brief.py --days 3      # use only last 3 days of data

Environment variables (required):
  ANTHROPIC_API_KEY             your Anthropic API key

Environment variables (optional — enable email delivery):
  FLEET_EMAIL_TO                recipient address, e.g. you@gmail.com
  FLEET_EMAIL_FROM              sender address (your Gmail or iCloud)
  FLEET_SMTP_PASSWORD           app password (not your login password)
  FLEET_SMTP_HOST               default: smtp.gmail.com
  FLEET_SMTP_PORT               default: 587

Output:
  ~/Documents/weekly-brief-YYYY-MM-DD.md
  ~/Documents/fleet-YYYY-MM-DD.json
  ~/.fleet-commander/fleet-commander.log  (when run by launchd)
"""

import argparse
import json
import logging
import os
import re
import smtplib
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = Path.home() / ".screenpipe" / "db.sqlite"
OUTPUT_DIR = Path.home() / "Documents"
LOG_PATH = Path.home() / ".fleet-commander" / "fleet-commander.log"

SCREENPIPE_VERSION = "0.2.13"  # bump if you upgrade ScreenPipe

REQUIRED_COLUMNS = {
    "frames": {"id", "timestamp"},
    "ocr_text": {"frame_id", "app_name", "window_name"},
}

GENERIC_TITLES = {
    "", "new tab", "untitled", "new window",
    "extension: newtab", "start page", "home",
}


# ---------------------------------------------------------------------------
# Logging — writes to file when run by launchd, stdout when run manually
# ---------------------------------------------------------------------------

def setup_logging():
    is_launchd = not sys.stderr.isatty()
    handlers = []
    if is_launchd:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(LOG_PATH))
    else:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 0: Schema validation
# ---------------------------------------------------------------------------

def validate_schema(conn):
    cur = conn.cursor()
    for table, required_cols in REQUIRED_COLUMNS.items():
        cur.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cur.fetchall()}
        missing = required_cols - existing
        if missing:
            log.error(
                "ScreenPipe schema mismatch — table '%s' missing columns: %s\n"
                "You may have upgraded ScreenPipe (pinned to %s).\n"
                "Run: sqlite3 ~/.screenpipe/db.sqlite .schema\n"
                "Then update REQUIRED_COLUMNS in brief.py.",
                table, missing, SCREENPIPE_VERSION,
            )
            sys.exit(1)


# ---------------------------------------------------------------------------
# Step 1: Load rows
# ---------------------------------------------------------------------------

def load_rows(conn, days=7):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.cursor()
    cur.execute(
        """
        SELECT f.timestamp, o.app_name, o.window_name
        FROM ocr_text o
        JOIN frames f ON o.frame_id = f.id
        WHERE f.timestamp >= ?
          AND o.app_name != ''
          AND o.window_name IS NOT NULL
          AND trim(lower(o.window_name)) NOT IN (
              '', 'new tab', 'untitled', 'new window',
              'extension: newtab', 'start page', 'home'
          )
        ORDER BY f.timestamp
        """,
        (since,),
    )
    rows_raw = cur.fetchall()

    rows, skipped = [], 0
    for ts_str, app, window in rows_raw:
        if not app or not window:
            skipped += 1
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            skipped += 1
            continue
        rows.append((ts, app.strip(), window.strip()))

    log.info("loaded %d rows (%d filtered)", len(rows), skipped)
    return rows


# ---------------------------------------------------------------------------
# Step 2: Confidence
# ---------------------------------------------------------------------------

def compute_confidence(rows):
    if not rows:
        return 0, 0.0
    days_of_data = len({r[0].date() for r in rows})
    return days_of_data, min(days_of_data / 7.0, 1.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_title(title):
    return re.sub(r"\s+[—\-]\s+.+$", "", title).strip()


def compute_app_segments(rows):
    """Collapse consecutive same-app rows into (app, window, start, end) segments."""
    if not rows:
        return []
    segments = []
    curr_app, curr_window, curr_start = rows[0][1], rows[0][2], rows[0][0]
    for i in range(1, len(rows)):
        ts, app, window = rows[i]
        gap = (ts - rows[i - 1][0]).total_seconds()
        if app != curr_app or gap > 60:
            segments.append((curr_app, curr_window, curr_start, rows[i - 1][0]))
            curr_app, curr_window, curr_start = app, window, ts
    segments.append((curr_app, curr_window, curr_start, rows[-1][0]))
    return segments


# ---------------------------------------------------------------------------
# Step 3a: Transition cost events
# ---------------------------------------------------------------------------

def find_transition_cost_events(rows, threshold=5, window_minutes=2):
    """2-minute windows with 5+ app switches. Non-overlapping."""
    if not rows:
        return []

    events_by_day = defaultdict(list)
    i = 0
    while i < len(rows):
        ts_start = rows[i][0]
        ts_end = ts_start + timedelta(minutes=window_minutes)
        j = i
        while j < len(rows) and rows[j][0] <= ts_end:
            j += 1
        window = rows[i:j]
        if len(window) >= 2:
            switches = sum(1 for a, b in zip(window, window[1:]) if a[1] != b[1])
            if switches >= threshold:
                day = ts_start.date().isoformat()
                events_by_day[day].append({
                    "time": ts_start.strftime("%H:%M"),
                    "switches": switches,
                    "apps": list({r[1] for r in window})[:5],
                    "_evidence": [(r[0].isoformat(), r[1], r[2]) for r in window],
                })
                i = j
                continue
        i += 1

    result = []
    for day, events in sorted(events_by_day.items()):
        result.append({
            "date": day,
            "day": datetime.fromisoformat(day).strftime("%a"),
            "count": len(events),
            "top_apps": list({app for e in events for app in e["apps"]})[:5],
            "_evidence": events,
        })
    return sorted(result, key=lambda x: x["count"], reverse=True)[:10]


# ---------------------------------------------------------------------------
# Step 3b: Repeated manual sequences
# ---------------------------------------------------------------------------

def find_repeated_sequences(rows, min_weeks=3, lookback_weeks=4):
    """Same (day-of-week, app, title) in 3+ of the past 4 weeks."""
    if not rows:
        return []

    now = rows[-1][0]
    pattern_weeks, pattern_evidence = defaultdict(set), defaultdict(list)

    for ts, app, window in rows:
        weeks_ago = (now - ts).days // 7
        if weeks_ago >= lookback_weeks:
            continue
        norm = normalize_title(window)
        if not norm or norm.lower() in GENERIC_TITLES:
            continue
        key = (ts.weekday(), app, norm)
        pattern_weeks[key].add(weeks_ago)
        pattern_evidence[key].append((ts.isoformat(), app, window))

    result = []
    for (dow, app, norm_title), weeks_set in pattern_weeks.items():
        if len(weeks_set) >= min_weeks:
            result.append({
                "app": app,
                "title_pattern": norm_title,
                "day": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dow],
                "weeks_observed": len(weeks_set),
                "_evidence": pattern_evidence[(dow, app, norm_title)][-5:],
            })

    result.sort(key=lambda x: x["weeks_observed"], reverse=True)
    return result[:10]


# ---------------------------------------------------------------------------
# Step 3c: Deep work fragmentation
# ---------------------------------------------------------------------------

def find_focus_fragmentation(rows, min_break_seconds=30):
    """Longest uninterrupted focus block per day. Glances < 30s don't break a block."""
    if not rows:
        return []

    rows_by_day = defaultdict(list)
    for r in rows:
        rows_by_day[r[0].date()].append(r)

    result = []
    for day, day_rows in sorted(rows_by_day.items()):
        segments = compute_app_segments(day_rows)
        if not segments:
            continue

        focus_blocks, curr_app, block_start = [], segments[0][0], segments[0][2]
        for seg_app, _, seg_start, seg_end in segments[1:]:
            if seg_app != curr_app and (seg_end - seg_start).total_seconds() >= min_break_seconds:
                focus_blocks.append((curr_app, block_start, seg_start))
                curr_app, block_start = seg_app, seg_start
        focus_blocks.append((curr_app, block_start, segments[-1][3]))

        durations = [(end - start).total_seconds() / 60 for _, start, end in focus_blocks]
        result.append({
            "date": day.isoformat(),
            "day": day.strftime("%a"),
            "longest_block_min": round(max(durations), 1) if durations else 0,
            "blocks_over_30min": sum(1 for d in durations if d >= 30),
            "_evidence": [(b[0], b[1].isoformat(), b[2].isoformat()) for b in focus_blocks[:5]],
        })

    return result


# ---------------------------------------------------------------------------
# Step 6: Claude API — structured output
# ---------------------------------------------------------------------------

def call_claude(patterns, days_of_data, confidence):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.error("ANTHROPIC_API_KEY environment variable not set")
        sys.exit(1)

    import urllib.request

    prompt = f"""You are a technically fluent EA who watched this person's entire week.

Here are behavioral patterns extracted from their screen activity \
({days_of_data} days of data, confidence {round(confidence * 10)}/10):

{json.dumps(patterns, indent=2, default=str)}

Write a frank debrief. Name 2-3 things they're doing that probably cost them \
time without realizing it. Name 1-2 workflows that could be handed to an AI \
agent — be specific about what the agent would do.

If the data is sparse or patterns are weak, say so directly rather than \
fabricating patterns. Honest signal assessment beats a confident brief built on noise.

Do not hedge. Do not use: leverage, utilize, optimize, robust, comprehensive, \
nuanced, deep dive, actionable, streamline."""

    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "tools": [{
            "name": "write_brief",
            "description": "Write the weekly advisor brief",
            "input_schema": {
                "type": "object",
                "properties": {
                    "brief_text": {
                        "type": "string",
                        "description": "300-400 word frank debrief. Prose, no bullet points.",
                    },
                    "automation_candidates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "what_agent_does": {"type": "string"},
                                "evidence_key": {"type": "string"},
                            },
                            "required": ["title", "what_agent_does", "evidence_key"],
                        },
                    },
                },
                "required": ["brief_text", "automation_candidates"],
            },
        }],
        "tool_choice": {"type": "tool", "name": "write_brief"},
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())

    for block in result.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "write_brief":
            return block["input"]

    raise ValueError(f"Unexpected API response: {json.dumps(result)[:300]}")


# ---------------------------------------------------------------------------
# Delivery: macOS notification
# ---------------------------------------------------------------------------

def send_notification(title, message):
    try:
        script = f'display notification "{message}" with title "{title}"'
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
    except Exception as e:
        log.warning("notification failed: %s", e)


# ---------------------------------------------------------------------------
# Delivery: email
# ---------------------------------------------------------------------------

def send_email(brief_md, candidates):
    to_addr = os.environ.get("FLEET_EMAIL_TO", "")
    from_addr = os.environ.get("FLEET_EMAIL_FROM", "")
    password = os.environ.get("FLEET_SMTP_PASSWORD", "")

    if not all([to_addr, from_addr, password]):
        return  # email not configured — skip silently

    host = os.environ.get("FLEET_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("FLEET_SMTP_PORT", "587"))
    today = datetime.now().strftime("%b %d, %Y")
    candidate_count = len(candidates)
    subject = f"Fleet Commander Brief — {today} ({candidate_count} automation candidate{'s' if candidate_count != 1 else ''})"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(brief_md, "plain"))

    try:
        with smtplib.SMTP(host, port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(from_addr, password)
            smtp.sendmail(from_addr, to_addr, msg.as_string())
        log.info("brief emailed to %s", to_addr)
    except Exception as e:
        log.warning("email delivery failed: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    setup_logging()

    parser = argparse.ArgumentParser(description="Fleet Commander — Weekly Advisor Brief")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print extracted patterns without calling Claude")
    parser.add_argument("--explain", action="store_true",
                        help="Include raw evidence for each automation candidate")
    parser.add_argument("--days", type=int, default=7,
                        help="Days of data to query (default: 7)")
    args = parser.parse_args()

    log.info("Fleet Commander starting")

    if not DB_PATH.exists():
        log.error("ScreenPipe database not found at %s — is ScreenPipe running?", DB_PATH)
        sys.exit(1)

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    validate_schema(conn)

    rows = load_rows(conn, days=args.days)
    if len(rows) < 10:
        log.error("only %d rows — not enough data yet (need at least 2 hours)", len(rows))
        sys.exit(1)

    days_of_data, confidence = compute_confidence(rows)
    log.info("%d days of data, confidence %d/10", days_of_data, round(confidence * 10))

    log.info("extracting patterns")
    transition_events = find_transition_cost_events(rows)
    repeated_sequences = find_repeated_sequences(rows)
    focus_fragmentation = find_focus_fragmentation(rows)

    def strip_evidence(items):
        return [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]

    patterns = {
        "days_of_data": days_of_data,
        "confidence": round(confidence, 2),
        "transition_cost_events": strip_evidence(transition_events),
        "repeated_sequences": strip_evidence(repeated_sequences),
        "focus_fragmentation": strip_evidence(focus_fragmentation),
    }

    if args.dry_run:
        print(json.dumps(patterns, indent=2, default=str))
        return

    onboarding_caveat = days_of_data < 5

    log.info("calling Claude API")
    brief_result = call_claude(patterns, days_of_data, confidence)
    candidates = brief_result.get("automation_candidates", [])

    # Build fleet.json
    today = datetime.now().strftime("%Y-%m-%d")
    fleet = {
        "generated_at": today,
        "confidence": round(confidence, 2),
        "days_of_data": days_of_data,
        **{k: v for k, v in patterns.items() if k not in ("confidence", "days_of_data")},
        "automation_candidates": candidates,
    }

    # Build markdown
    conf_display = f"{round(confidence * 10)}/10"
    lines = [
        f"# Fleet Commander Brief — Week of {today}",
        f"**Confidence: {conf_display}** — {days_of_data} days of data",
        "",
    ]

    if onboarding_caveat:
        lines += [
            "> **Week 1 note:** Repeated-sequence patterns need 3+ weeks to surface.",
            "> This brief shows transition costs and focus fragmentation only.",
            "> Come back next week — it gets sharper every week.",
            "",
        ]

    lines += [brief_result.get("brief_text", ""), ""]

    if candidates:
        lines += ["---", "## Automation candidates", ""]
        for c in candidates:
            lines += [f"**{c['title']}** — {c['what_agent_does']}", ""]

    if args.explain and candidates:
        lines += ["---", "## Raw evidence", ""]
        for c in candidates:
            lines.append(f"### {c['title']}")
            key = c.get("evidence_key", "").lower()
            if "repeated_sequence" in key:
                for seq in repeated_sequences:
                    for ts_str, app, window in seq.get("_evidence", [])[:5]:
                        lines.append(f"    {ts_str[:16]}  {app}: {window}")
                    break
            elif "transition" in key:
                for event in transition_events:
                    for ev in event.get("_evidence", [])[:1]:
                        for ts_str, app, window in ev.get("_evidence", [])[:6]:
                            lines.append(f"    {ts_str[:16]}  {app}: {window}")
                    break
            lines.append("")

    brief_md = "\n".join(lines)

    # Write outputs
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    brief_path = OUTPUT_DIR / f"weekly-brief-{today}.md"
    fleet_path = OUTPUT_DIR / f"fleet-{today}.json"
    brief_path.write_text(brief_md)
    fleet_path.write_text(json.dumps(fleet, indent=2, default=str))
    log.info("brief → %s", brief_path)
    log.info("fleet → %s", fleet_path)

    # Deliver
    candidate_summary = f"{len(candidates)} automation candidate{'s' if len(candidates) != 1 else ''} found"
    send_notification("Fleet Commander", f"Weekly brief ready — {candidate_summary}")
    send_email(brief_md, candidates)

    print(brief_md)


if __name__ == "__main__":
    main()
