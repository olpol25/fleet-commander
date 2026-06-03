#!/usr/bin/env python3
"""
Fleet Commander — executor.py
Turns an automation candidate from a brief into a structured schematic, then
scaffolds dry-run automation code from that schematic.

Schematic-first: the schematic is the deliverable — a spec covering inputs,
steps, outputs, and everything the automation would touch (apps/files/APIs).
The scaffold is generated from it.

Hard deploy gate: nothing here runs or deploys live. No cron, no launchd, no
action-taking API calls, no installed agents. Scaffolds are dry-run only.
Deployment waits until there is a hosted app to run automations under
supervision. The gate is enforced in code, not left to the model.

Usage:
  python executor.py list [--fleet PATH]
      List the automation candidates in a fleet.json.

  python executor.py schematic <index|title> [--fleet PATH] [--offline] [--out PATH]
      Generate a schematic for one candidate. Calls Claude to plan it, unless
      --offline is given (then a deterministic skeleton is produced — no API
      call, useful with no API key or for a quick stub).

  python executor.py scaffold <schematic.json> [--out DIR]
      Scaffold dry-run automation code from a schematic file.

Environment variables:
  ANTHROPIC_API_KEY   required for `schematic` (not needed with --offline)

No pip dependencies — standard library only.
"""

import argparse
import copy
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OUTPUT_DIR = Path.home() / "Documents"
SCHEMATIC_VERSION = "1"
MODEL = "claude-opus-4-7"

# Hard deploy gate. This stays closed until there is a hosted Fleet Commander
# app to run automations under supervision. It is not a flag to be flipped:
# every schematic gets this canonical BLOCKED block injected by code, and every
# scaffold is dry-run only by construction.
DEPLOY_GATE = {
    "status": "BLOCKED",
    "mode": "dry-run-only",
    "reason": (
        "No hosted app exists yet. Live deployment, scheduling, and "
        "action-taking API calls are disabled by the executor."
    ),
    "blocked": [
        "cron / launchd / scheduled-task registration",
        "action-taking API calls",
        "installed or background agents",
        "live execution of generated code",
    ],
    "unblock_condition": (
        "A hosted Fleet Commander app exists to run automations under "
        "supervision."
    ),
}

# Map an evidence_key (free text from the brief) to a pattern block in fleet.json.
EVIDENCE_KEYS = {
    "repeated_sequence": "repeated_sequences",
    "transition": "transition_cost_events",
    "focus": "focus_fragmentation",
}


class ExecutorError(Exception):
    """A user-facing failure — caught in main(), printed without a traceback."""


# ---------------------------------------------------------------------------
# fleet.json loading and candidate selection
# ---------------------------------------------------------------------------

def find_latest_fleet_json():
    matches = sorted(OUTPUT_DIR.glob("fleet-*.json"))
    if not matches:
        raise ExecutorError(
            f"No fleet-*.json found in {OUTPUT_DIR}. Run brief.py first, "
            f"or pass --fleet PATH."
        )
    return matches[-1]


def load_fleet(path):
    path = Path(path)
    try:
        fleet = json.loads(path.read_text())
    except FileNotFoundError:
        raise ExecutorError(f"fleet file not found: {path}")
    except json.JSONDecodeError as e:
        raise ExecutorError(f"{path} is not valid JSON: {e}")
    if not isinstance(fleet, dict):
        raise ExecutorError(f"{path} does not contain a fleet object.")
    return fleet


def select_candidate(fleet, selector):
    """Return (index, candidate) for a 0-based index or a title substring."""
    candidates = fleet.get("automation_candidates") or []
    if not candidates:
        raise ExecutorError("This fleet.json has no automation_candidates.")

    try:
        index = int(selector)
    except ValueError:
        index = None
    if index is not None:
        if not 0 <= index < len(candidates):
            raise ExecutorError(
                f"Candidate index {index} out of range (0..{len(candidates) - 1})."
            )
        return index, candidates[index]

    needle = selector.lower()
    hits = [
        (i, c) for i, c in enumerate(candidates)
        if needle in str(c.get("title", "")).lower()
    ]
    if not hits:
        raise ExecutorError(f"No candidate title matches {selector!r}.")
    if len(hits) > 1:
        titles = ", ".join(repr(c.get("title", "")) for _, c in hits)
        raise ExecutorError(f"{selector!r} matches multiple candidates: {titles}")
    return hits[0]


def gather_evidence(fleet, candidate):
    """Pull the pattern block this candidate was derived from, by evidence_key."""
    key = str(candidate.get("evidence_key", "")).lower()
    for marker, block_name in EVIDENCE_KEYS.items():
        if marker in key:
            return {block_name: fleet.get(block_name, [])}
    return {
        "note": f"evidence_key {key!r} did not match a known pattern block",
        "days_of_data": fleet.get("days_of_data"),
        "confidence": fleet.get("confidence"),
    }


# ---------------------------------------------------------------------------
# Schematic shape — normalization and the code-enforced deploy gate
# ---------------------------------------------------------------------------

def enforce_deploy_gate(schematic):
    """Overwrite the deploy gate with the canonical BLOCKED block — always.

    The model does not get a say in this, and neither does a hand-edited
    schematic. Whatever was there before, the gate is closed.
    """
    schematic["deploy_gate"] = copy.deepcopy(DEPLOY_GATE)
    return schematic


def _clean_str_list(value):
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _normalize_steps(value):
    if not isinstance(value, list):
        return []
    steps = []
    for i, raw in enumerate(value, start=1):
        raw = raw if isinstance(raw, dict) else {}
        steps.append({
            "n": i,
            "action": str(raw.get("action", "step")).strip() or "step",
            "detail": str(raw.get("detail", "")).strip(),
            "touches": _clean_str_list(raw.get("touches")),
        })
    return steps


def _normalize_records(value, fields):
    if not isinstance(value, list):
        return []
    records = []
    for raw in value:
        raw = raw if isinstance(raw, dict) else {}
        records.append({f: raw.get(f, default) for f, default in fields.items()})
    return records


def normalize_schematic(raw, candidate, generator):
    """Build a complete, well-formed schematic from a (possibly partial) dict.

    Every key is filled, the structure is fixed, and the deploy gate is
    code-enforced. Used for both the Claude path and the offline skeleton.
    """
    raw = raw if isinstance(raw, dict) else {}
    trigger = raw.get("trigger") if isinstance(raw.get("trigger"), dict) else {}
    touches = raw.get("touches") if isinstance(raw.get("touches"), dict) else {}

    schematic = {
        "schematic_version": SCHEMATIC_VERSION,
        "candidate_title": str(candidate.get("title", "untitled")).strip() or "untitled",
        "candidate_evidence_key": str(candidate.get("evidence_key", "")).strip(),
        "generated_at": datetime.now().strftime("%Y-%m-%d"),
        "generator": generator,
        "summary": (
            str(raw.get("summary", "")).strip()
            or str(candidate.get("what_agent_does", "")).strip()
        ),
        "trigger": {
            "type": str(trigger.get("type", "manual")).strip() or "manual",
            "detail": str(trigger.get("detail", "Run by hand.")).strip(),
        },
        "inputs": _normalize_records(raw.get("inputs"), {
            "name": "input", "type": "unspecified", "source": "unspecified",
            "required": True,
        }),
        "steps": _normalize_steps(raw.get("steps")),
        "outputs": _normalize_records(raw.get("outputs"), {
            "name": "output", "type": "unspecified", "destination": "unspecified",
        }),
        "touches": {
            "apps": _clean_str_list(touches.get("apps")),
            "files": _clean_str_list(touches.get("files")),
            "apis": _clean_str_list(touches.get("apis")),
        },
        "risks": _clean_str_list(raw.get("risks")),
        "open_questions": _clean_str_list(raw.get("open_questions")),
    }
    return enforce_deploy_gate(schematic)


def offline_schematic(candidate):
    """A deterministic skeleton schematic — no API call, no model analysis."""
    title = str(candidate.get("title", "untitled")).strip() or "untitled"
    what = str(candidate.get("what_agent_does", "")).strip()
    raw = {
        "summary": what or f"Automation skeleton for: {title}",
        "trigger": {
            "type": "manual",
            "detail": "Run by hand. No scheduling — the deploy gate is closed.",
        },
        "inputs": [{
            "name": "evidence_context",
            "type": "json",
            "source": f"fleet.json -> {candidate.get('evidence_key', '')}",
            "required": True,
        }],
        "steps": [
            {"n": 1, "action": "gather inputs",
             "detail": "Collect the inputs listed in this schematic.",
             "touches": []},
            {"n": 2, "action": "perform automation work",
             "detail": what or "Carry out the work described by the candidate.",
             "touches": []},
            {"n": 3, "action": "write outputs",
             "detail": "Write the outputs listed in this schematic.",
             "touches": []},
        ],
        "outputs": [{
            "name": "result", "type": "unspecified", "destination": "dry-run log",
        }],
        "touches": {"apps": [], "files": [], "apis": []},
        "risks": [
            "Offline skeleton — generated without model analysis. Steps are "
            "placeholders, not a real plan.",
        ],
        "open_questions": [
            "Which apps, files, and APIs would this automation actually touch?",
            "What are the concrete steps? Re-run without --offline to plan them.",
        ],
    }
    return normalize_schematic(raw, candidate, generator="offline-skeleton")


# ---------------------------------------------------------------------------
# Schematic generation via Claude — structured output, forced tool use
# ---------------------------------------------------------------------------

SCHEMATIC_SYSTEM_PROMPT = """\
You are the schematic planner for Fleet Commander, a tool that turns observed \
work patterns into automation specifications.

You are given ONE automation candidate identified from a person's \
screen-activity brief. Produce a structured schematic: a precise spec of the \
inputs it needs, the steps it would take, the outputs it would produce, and \
everything it would touch — apps, files, and APIs.

Hard rules:
- This is a SPEC, not a deployment. Nothing you describe will run live.
- Do not assume credentials, hosted infrastructure, or schedulers exist.
- For 'trigger', describe how it WOULD be triggered. Any schedule is a \
proposal only — nothing gets scheduled.
- Be concrete about apps, files, and APIs. Name them. If you have to guess, \
say so in open_questions instead of inventing certainty.
- Steps should be small and individually implementable. Each step lists what \
it would touch.
- Be honest. If the candidate is vague or the evidence is thin, say so in \
open_questions and keep the schematic minimal rather than padding it."""

SCHEMATIC_TOOL = {
    "name": "emit_schematic",
    "description": "Emit the structured automation schematic for the candidate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-3 plain sentences: what this automation does.",
            },
            "trigger": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["manual", "schedule-proposed", "event-proposed"],
                        "description": (
                            "How it WOULD be triggered. 'manual' = a person "
                            "runs it. The '-proposed' values are proposals "
                            "only; nothing is scheduled or wired up."
                        ),
                    },
                    "detail": {"type": "string"},
                },
                "required": ["type", "detail"],
            },
            "inputs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {
                            "type": "string",
                            "description": "e.g. text, list, file, api-response, credential",
                        },
                        "source": {
                            "type": "string",
                            "description": "where the input comes from",
                        },
                        "required": {"type": "boolean"},
                    },
                    "required": ["name", "type", "source", "required"],
                },
            },
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "n": {"type": "integer"},
                        "action": {
                            "type": "string",
                            "description": "short imperative label",
                        },
                        "detail": {"type": "string"},
                        "touches": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "what this step would touch, e.g. 'app:Linear', "
                                "'file:~/Documents/x.md', 'api:api.notion.com'"
                            ),
                        },
                    },
                    "required": ["n", "action", "detail", "touches"],
                },
            },
            "outputs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string"},
                        "destination": {"type": "string"},
                    },
                    "required": ["name", "type", "destination"],
                },
            },
            "touches": {
                "type": "object",
                "description": "Everything the automation would touch, deduplicated.",
                "properties": {
                    "apps": {"type": "array", "items": {"type": "string"}},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "apis": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["apps", "files", "apis"],
            },
            "risks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What could go wrong if this were ever deployed live.",
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "summary", "trigger", "inputs", "steps", "outputs", "touches",
            "risks", "open_questions",
        ],
    },
}


def _build_schematic_payload(candidate, evidence):
    user_text = (
        "Automation candidate:\n"
        f"  title: {candidate.get('title', '')}\n"
        f"  what the agent would do: {candidate.get('what_agent_does', '')}\n"
        f"  evidence_key: {candidate.get('evidence_key', '')}\n\n"
        "Evidence from the brief (the pattern data this candidate came from):\n"
        f"{json.dumps(evidence, indent=2, default=str)}\n\n"
        "Produce the schematic by calling emit_schematic."
    )
    # The stable prefix (system prompt + tool schema) is the same for every
    # candidate, so it is cache-friendly; only the user turn varies per call.
    return {
        "model": MODEL,
        "max_tokens": 4096,
        "system": [{
            "type": "text",
            "text": SCHEMATIC_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        "tools": [SCHEMATIC_TOOL],
        "tool_choice": {"type": "tool", "name": "emit_schematic"},
        "messages": [{"role": "user", "content": user_text}],
    }


def call_claude_schematic(candidate, evidence):
    """Ask Claude to plan the schematic. Returns the raw tool input dict."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ExecutorError(
            "ANTHROPIC_API_KEY is not set. Set it, or use --offline for a "
            "deterministic skeleton schematic."
        )

    payload = json.dumps(_build_schematic_payload(candidate, evidence)).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code == 401:
            raise ExecutorError("API key rejected (401). Check ANTHROPIC_API_KEY.")
        if e.code == 429:
            raise ExecutorError("Rate limited (429). Wait a minute and retry.")
        if e.code == 529:
            raise ExecutorError("Anthropic API overloaded (529). Retry shortly.")
        raise ExecutorError(f"API error {e.code}: {body[:200]}")
    except urllib.error.URLError as e:
        raise ExecutorError(f"Network error reaching the Anthropic API: {e.reason}")

    for block in result.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "emit_schematic":
            return block.get("input", {})
    raise ExecutorError(
        f"Unexpected API response (no emit_schematic tool call): "
        f"{json.dumps(result)[:300]}"
    )


def generate_schematic(fleet, candidate, offline=False):
    if offline:
        return offline_schematic(candidate)
    evidence = gather_evidence(fleet, candidate)
    raw = call_claude_schematic(candidate, evidence)
    return normalize_schematic(raw, candidate, generator=f"claude:{MODEL}")


# ---------------------------------------------------------------------------
# Scaffold — dry-run automation code generated from a schematic
# ---------------------------------------------------------------------------

def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug or "automation"


def _escape_triple(text):
    """Neutralize text for safe embedding inside a triple-quoted string literal.

    Schematic fields are semi-trusted (a schematic.json can be hand-edited or
    shared). Without this, a `\"\"\"` in a title or summary would break out of
    the generated docstring and run as module-level code at import time —
    before the deploy gate is ever reached. Escaping the backslash first, then
    every double-quote, makes a `\"\"\"` breakout impossible and stops a
    trailing backslash from escaping the closing delimiter.
    """
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _step_func_name(step):
    return f"step_{step['n']}_{slugify(step['action']).replace('-', '_')}"


def render_automation_py(schematic):
    """Render the dry-run automation scaffold as Python source.

    Every step is a stub that routes through dry_run() — it prints what it
    WOULD do and touches nothing. There are no imports for scheduling,
    subprocesses, or networking, and main() refuses to proceed if the deploy
    gate is ever forced open.
    """
    raw_summary = str(schematic.get("summary", "")).strip()
    title = _escape_triple(schematic.get("candidate_title", "automation"))
    summary_doc = _escape_triple(raw_summary or "(no summary)")
    # _normalize_steps coerces any hand-edited / partial steps into a known
    # shape, so the renderer is safe even when called straight from a loaded
    # schematic.json (cmd_scaffold does not re-run full normalization).
    steps = _normalize_steps(schematic.get("steps")) or [{
        "n": 1, "action": "perform automation work",
        "detail": raw_summary or "No steps were specified.",
        "touches": [],
    }]

    lines = [
        '"""',
        f"Automation scaffold — {title}",
        "",
        summary_doc,
        "",
        "GENERATED by the Fleet Commander executor. This is a DRY-RUN scaffold.",
        "Nothing here runs live: no scheduling, no action-taking API calls, no",
        "installed agents. Every step only prints what it would do. The deploy",
        "gate stays closed until there is a hosted app to run this under",
        "supervision — see schematic.json -> deploy_gate.",
        '"""',
        "",
        "import json",
        "import pathlib",
        "",
        "# Hard deploy gate. Do not flip this. Deployment waits for a hosted app.",
        "DEPLOY_ENABLED = False",
        "",
        "SCHEMATIC = json.loads(",
        '    (pathlib.Path(__file__).parent / "schematic.json").read_text()',
        ")",
        "",
        "",
        "def dry_run(action, detail, touches):",
        '    """Print what a step would do. Takes no real action — ever."""',
        '    line = f"[DRY-RUN] {action}: {detail}"',
        "    if touches:",
        "        line += f\"  (would touch: {', '.join(touches)})\"",
        "    print(line)",
        "",
    ]

    for step in steps:
        touches = ", ".join(repr(t) for t in step["touches"])
        lines += [
            "",
            f"def {_step_func_name(step)}(ctx):",
            # repr() keeps the action a safe one-line literal; as the function's
            # first statement it still serves as the docstring.
            f"    {step['action']!r}",
            "    dry_run(",
            f"        action={step['action']!r},",
            f"        detail={step['detail']!r},",
            f"        touches=[{touches}],",
            "    )",
            "    # TODO: implement this step. Until the deploy gate opens it must",
            "    # stay dry-run only — no live actions, no scheduling, no network.",
            "    return ctx",
        ]

    step_names = ", ".join(_step_func_name(s) for s in steps)
    lines += [
        "",
        "",
        f"STEPS = [{step_names}]",
        "",
        "",
        "def main():",
        "    if DEPLOY_ENABLED:",
        "        raise SystemExit(",
        '            "Deploy gate is closed. This scaffold is dry-run only and "',
        '            "must not run with DEPLOY_ENABLED=True."',
        "        )",
        '    print(f"DRY-RUN plan for: {SCHEMATIC[\'candidate_title\']}")',
        '    print(SCHEMATIC["summary"])',
        '    print()',
        "    ctx = {}",
        "    for step in STEPS:",
        "        ctx = step(ctx)",
        '    print()',
        '    print("Dry-run complete. No live actions were taken. Deploy gate: BLOCKED.")',
        "",
        "",
        'if __name__ == "__main__":',
        "    main()",
        "",
    ]
    return "\n".join(lines)


def scaffold_from_schematic(schematic, out_dir):
    """Write the scaffold (automation.py + schematic.json) into out_dir."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    schematic_path = out_dir / "schematic.json"
    automation_path = out_dir / "automation.py"
    schematic_path.write_text(json.dumps(schematic, indent=2, default=str))
    automation_path.write_text(render_automation_py(schematic))
    return [schematic_path, automation_path]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_deploy_gate(gate):
    print(f"  deploy gate: {gate['status']} ({gate['mode']})")
    print(f"  {gate['reason']}")


def cmd_list(args):
    fleet_path = args.fleet or find_latest_fleet_json()
    fleet = load_fleet(fleet_path)
    candidates = fleet.get("automation_candidates") or []
    print(f"{fleet_path}  —  {len(candidates)} automation candidate(s)")
    for i, c in enumerate(candidates):
        print(f"\n  [{i}] {c.get('title', '(untitled)')}")
        print(f"      {c.get('what_agent_does', '').strip()}")
        print(f"      evidence_key: {c.get('evidence_key', '')}")


def cmd_schematic(args):
    fleet_path = args.fleet or find_latest_fleet_json()
    fleet = load_fleet(fleet_path)
    index, candidate = select_candidate(fleet, args.candidate)

    print(f"Candidate [{index}]: {candidate.get('title', '')}")
    if args.offline:
        print("Generating offline skeleton schematic (no API call)...")
    else:
        print(f"Planning schematic with {MODEL}...")
    schematic = generate_schematic(fleet, candidate, offline=args.offline)

    out_path = Path(args.out) if args.out else Path.cwd() / (
        f"schematic-{slugify(schematic['candidate_title'])}.json"
    )
    out_path.write_text(json.dumps(schematic, indent=2, default=str))

    print(f"\nschematic → {out_path}")
    print(f"  summary: {schematic['summary']}")
    print(f"  {len(schematic['steps'])} step(s), "
          f"{len(schematic['inputs'])} input(s), "
          f"{len(schematic['outputs'])} output(s)")
    t = schematic["touches"]
    print(f"  touches: {len(t['apps'])} app(s), "
          f"{len(t['files'])} file(s), {len(t['apis'])} api(s)")
    _print_deploy_gate(schematic["deploy_gate"])
    print(f"\nNext: python executor.py scaffold {out_path}")


def cmd_scaffold(args):
    schematic_path = Path(args.schematic)
    try:
        schematic = json.loads(schematic_path.read_text())
    except FileNotFoundError:
        raise ExecutorError(f"schematic file not found: {schematic_path}")
    except json.JSONDecodeError as e:
        raise ExecutorError(f"{schematic_path} is not valid JSON: {e}")

    # Re-enforce the deploy gate — a schematic file could have been hand-edited.
    enforce_deploy_gate(schematic)
    schematic.setdefault("candidate_title", schematic_path.stem)
    schematic.setdefault("summary", "")
    schematic.setdefault("steps", [])

    out_dir = Path(args.out) if args.out else Path.cwd() / (
        f"automation-{slugify(schematic['candidate_title'])}"
    )
    written = scaffold_from_schematic(schematic, out_dir)

    print(f"scaffold → {out_dir}/")
    for path in written:
        print(f"  {path.name}")
    _print_deploy_gate(schematic["deploy_gate"])
    print(f"\nIt is dry-run only. Run it with: python {out_dir / 'automation.py'}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Fleet Commander executor — schematics and dry-run scaffolds.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list automation candidates in a fleet.json")
    p_list.add_argument("--fleet", help="path to fleet.json (default: latest in ~/Documents)")
    p_list.set_defaults(func=cmd_list)

    p_schematic = sub.add_parser("schematic", help="generate a schematic for one candidate")
    p_schematic.add_argument("candidate", help="candidate index or title substring")
    p_schematic.add_argument("--fleet", help="path to fleet.json (default: latest in ~/Documents)")
    p_schematic.add_argument("--offline", action="store_true",
                             help="deterministic skeleton, no API call")
    p_schematic.add_argument("--out", help="output path for the schematic JSON")
    p_schematic.set_defaults(func=cmd_schematic)

    p_scaffold = sub.add_parser("scaffold", help="scaffold dry-run code from a schematic")
    p_scaffold.add_argument("schematic", help="path to a schematic JSON file")
    p_scaffold.add_argument("--out", help="output directory for the scaffold")
    p_scaffold.set_defaults(func=cmd_scaffold)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except ExecutorError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
