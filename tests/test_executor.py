"""
Tests for executor.py — schematic generation, scaffolding, and the deploy gate.

Thin harness: fixtures are small inline dicts, the assertions lean on
executor.py's own functions. No network — the Claude path is never exercised
here (the offline skeleton covers the schematic shape end to end).

Run from the repo root:
  python -m unittest discover tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import executor  # noqa: E402


SAMPLE_FLEET = {
    "generated_at": "2026-05-14",
    "confidence": 0.9,
    "days_of_data": 7,
    "repeated_sequences": [
        {"app": "Notion", "title_pattern": "Sprint Planning", "day": "Mon",
         "weeks_observed": 4},
    ],
    "transition_cost_events": [
        {"date": "2026-05-12", "day": "Tue", "count": 6,
         "top_apps": ["Slack", "Linear"]},
    ],
    "focus_fragmentation": [
        {"date": "2026-05-12", "day": "Tue", "longest_block_min": 34.0,
         "blocks_over_30min": 1},
    ],
    "automation_candidates": [
        {"title": "Sprint kickoff prep",
         "what_agent_does": "Pulls open Linear issues and builds the Notion sprint doc.",
         "evidence_key": "repeated_sequences"},
        {"title": "Weekly async update",
         "what_agent_does": "Drafts the Friday update from Linear closes.",
         "evidence_key": "transition_cost_events"},
    ],
}


def _candidate(**overrides):
    base = {
        "title": "Test automation",
        "what_agent_does": "Does the test thing.",
        "evidence_key": "repeated_sequences",
    }
    base.update(overrides)
    return base


class DeployGateTests(unittest.TestCase):
    """The deploy gate is code-enforced and always closed."""

    def test_gate_constant_is_blocked(self):
        self.assertEqual(executor.DEPLOY_GATE["status"], "BLOCKED")
        self.assertEqual(executor.DEPLOY_GATE["mode"], "dry-run-only")

    def test_enforce_overrides_an_enabled_gate(self):
        schematic = {"deploy_gate": {"status": "ENABLED", "mode": "live"}}
        executor.enforce_deploy_gate(schematic)
        self.assertEqual(schematic["deploy_gate"]["status"], "BLOCKED")
        self.assertEqual(schematic["deploy_gate"]["mode"], "dry-run-only")

    def test_enforce_adds_a_gate_when_missing(self):
        schematic = {}
        executor.enforce_deploy_gate(schematic)
        self.assertEqual(schematic["deploy_gate"]["status"], "BLOCKED")

    def test_enforce_copies_the_blocked_list(self):
        # Mutating one schematic's gate must not leak into the module constant.
        s1, s2 = {}, {}
        executor.enforce_deploy_gate(s1)
        executor.enforce_deploy_gate(s2)
        s1["deploy_gate"]["blocked"].append("tampered")
        self.assertNotIn("tampered", s2["deploy_gate"]["blocked"])
        self.assertNotIn("tampered", executor.DEPLOY_GATE["blocked"])


class NormalizeSchematicTests(unittest.TestCase):

    REQUIRED_KEYS = {
        "schematic_version", "candidate_title", "candidate_evidence_key",
        "generated_at", "generator", "summary", "trigger", "inputs", "steps",
        "outputs", "touches", "risks", "open_questions", "deploy_gate",
    }

    def test_fills_every_key_from_an_empty_raw(self):
        schematic = executor.normalize_schematic({}, _candidate(), "test")
        self.assertEqual(set(schematic), self.REQUIRED_KEYS)
        self.assertEqual(schematic["deploy_gate"]["status"], "BLOCKED")
        self.assertEqual(schematic["generator"], "test")

    def test_falls_back_to_candidate_text_for_summary(self):
        schematic = executor.normalize_schematic(
            {}, _candidate(what_agent_does="Specific work."), "test")
        self.assertEqual(schematic["summary"], "Specific work.")

    def test_renumbers_steps_sequentially(self):
        raw = {"steps": [
            {"n": 9, "action": "second", "detail": "b", "touches": []},
            {"n": 3, "action": "first", "detail": "a", "touches": []},
        ]}
        schematic = executor.normalize_schematic(raw, _candidate(), "test")
        self.assertEqual([s["n"] for s in schematic["steps"]], [1, 2])

    def test_touches_is_always_three_lists(self):
        schematic = executor.normalize_schematic(
            {"touches": {"apps": ["Linear"]}}, _candidate(), "test")
        self.assertEqual(schematic["touches"]["apps"], ["Linear"])
        self.assertEqual(schematic["touches"]["files"], [])
        self.assertEqual(schematic["touches"]["apis"], [])

    def test_tolerates_garbage_input(self):
        # Non-dict raw, non-list fields — must not raise.
        schematic = executor.normalize_schematic(
            {"steps": "not a list", "risks": None, "trigger": "nope"},
            _candidate(), "test")
        self.assertEqual(schematic["steps"], [])
        self.assertEqual(schematic["risks"], [])
        self.assertEqual(schematic["trigger"]["type"], "manual")


class OfflineSchematicTests(unittest.TestCase):

    def test_offline_schematic_is_well_formed(self):
        schematic = executor.offline_schematic(_candidate())
        self.assertEqual(schematic["generator"], "offline-skeleton")
        self.assertEqual(schematic["deploy_gate"]["status"], "BLOCKED")
        self.assertTrue(schematic["steps"])
        self.assertEqual(schematic["trigger"]["type"], "manual")
        self.assertTrue(schematic["open_questions"])

    def test_offline_schematic_is_deterministic(self):
        a = executor.offline_schematic(_candidate())
        b = executor.offline_schematic(_candidate())
        # generated_at is the only date-dependent field; everything else matches.
        a.pop("generated_at")
        b.pop("generated_at")
        self.assertEqual(a, b)


class CandidateSelectionTests(unittest.TestCase):

    def test_select_by_index(self):
        index, candidate = executor.select_candidate(SAMPLE_FLEET, "1")
        self.assertEqual(index, 1)
        self.assertEqual(candidate["title"], "Weekly async update")

    def test_select_by_title_substring(self):
        index, candidate = executor.select_candidate(SAMPLE_FLEET, "kickoff")
        self.assertEqual(index, 0)

    def test_index_out_of_range_raises(self):
        with self.assertRaises(executor.ExecutorError):
            executor.select_candidate(SAMPLE_FLEET, "9")

    def test_no_title_match_raises(self):
        with self.assertRaises(executor.ExecutorError):
            executor.select_candidate(SAMPLE_FLEET, "nonexistent")

    def test_ambiguous_title_raises(self):
        fleet = {"automation_candidates": [
            {"title": "Report A"}, {"title": "Report B"},
        ]}
        with self.assertRaises(executor.ExecutorError):
            executor.select_candidate(fleet, "report")

    def test_empty_candidates_raises(self):
        with self.assertRaises(executor.ExecutorError):
            executor.select_candidate({"automation_candidates": []}, "0")


class GatherEvidenceTests(unittest.TestCase):

    def test_maps_repeated_sequences_key(self):
        evidence = executor.gather_evidence(
            SAMPLE_FLEET, _candidate(evidence_key="repeated_sequences"))
        self.assertIn("repeated_sequences", evidence)

    def test_maps_transition_key(self):
        evidence = executor.gather_evidence(
            SAMPLE_FLEET, _candidate(evidence_key="transition_cost_events"))
        self.assertIn("transition_cost_events", evidence)

    def test_maps_focus_key(self):
        evidence = executor.gather_evidence(
            SAMPLE_FLEET, _candidate(evidence_key="focus_fragmentation"))
        self.assertIn("focus_fragmentation", evidence)

    def test_unknown_key_falls_back_to_context(self):
        evidence = executor.gather_evidence(
            SAMPLE_FLEET, _candidate(evidence_key="mystery"))
        self.assertIn("note", evidence)
        self.assertEqual(evidence["confidence"], 0.9)


class ClaudeRequestShapeTests(unittest.TestCase):
    """The API request is built correctly — without making a call."""

    def test_payload_shape(self):
        payload = executor._build_schematic_payload(
            _candidate(), {"repeated_sequences": []})
        self.assertEqual(payload["model"], "claude-opus-4-7")
        self.assertEqual(payload["tool_choice"],
                         {"type": "tool", "name": "emit_schematic"})
        # Forced tool use is the structured-output mechanism; no thinking field.
        self.assertNotIn("thinking", payload)
        # Stable prefix carries a cache breakpoint; the candidate is in the user turn.
        self.assertEqual(payload["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertIn("Test automation", payload["messages"][0]["content"])

    def test_missing_api_key_raises(self):
        # call_claude_schematic must fail loudly, not silently, with no key.
        import os
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(executor.ExecutorError):
                executor.call_claude_schematic(_candidate(), {})
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved


class SlugifyTests(unittest.TestCase):

    def test_basic(self):
        self.assertEqual(executor.slugify("Sprint Kickoff Prep"), "sprint-kickoff-prep")

    def test_strips_punctuation_and_edges(self):
        self.assertEqual(executor.slugify("  Weekly: async! update  "),
                         "weekly-async-update")

    def test_empty_falls_back(self):
        self.assertEqual(executor.slugify("!!!"), "automation")


# Tokens that would indicate the scaffold can take a live action. The deploy
# gate means none of these may appear in generated automation code.
FORBIDDEN_TOKENS = [
    "subprocess", "os.system", "crontab", "launchctl", "schtasks",
    "urllib", "requests", "socket", ".popen", "eval(", "exec(",
]


class ScaffoldTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out_dir = Path(self.tmp.name) / "scaffold"
        self.schematic = executor.offline_schematic(_candidate())

    def _scaffold(self):
        return executor.scaffold_from_schematic(self.schematic, self.out_dir)

    def test_writes_both_files(self):
        self._scaffold()
        self.assertTrue((self.out_dir / "automation.py").exists())
        self.assertTrue((self.out_dir / "schematic.json").exists())

    def test_schematic_json_round_trips(self):
        self._scaffold()
        written = json.loads((self.out_dir / "schematic.json").read_text())
        self.assertEqual(written["deploy_gate"]["status"], "BLOCKED")

    def test_automation_py_is_valid_python(self):
        self._scaffold()
        source = (self.out_dir / "automation.py").read_text()
        # compile() raises SyntaxError if the generated code is malformed.
        compile(source, "automation.py", "exec")

    def test_automation_py_has_closed_deploy_gate(self):
        self._scaffold()
        source = (self.out_dir / "automation.py").read_text()
        self.assertIn("DEPLOY_ENABLED = False", source)
        self.assertNotIn("DEPLOY_ENABLED = True", source)

    def test_automation_py_has_no_live_action_tokens(self):
        self._scaffold()
        source = (self.out_dir / "automation.py").read_text()
        for token in FORBIDDEN_TOKENS:
            self.assertNotIn(token, source,
                             f"scaffold must not contain {token!r}")

    def test_automation_py_runs_as_a_dry_run(self):
        self._scaffold()
        result = subprocess.run(
            [sys.executable, str(self.out_dir / "automation.py")],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[DRY-RUN]", result.stdout)
        self.assertIn("Deploy gate: BLOCKED", result.stdout)

    def test_scaffold_neutralizes_docstring_injection(self):
        # A schematic.json is semi-trusted (hand-editable, shareable). A `"""`
        # in a title / summary / step action must not break out of the
        # generated docstrings and run as module-level code at import time.
        marker = Path(self.tmp.name) / "PWNED"
        payload = (
            f'x"""\nimport pathlib; '
            f'pathlib.Path({str(marker)!r}).write_text("pwned")\n_pad = """x'
        )
        self.schematic["candidate_title"] = payload
        self.schematic["summary"] = payload
        self.schematic["steps"] = [
            {"n": 1, "action": payload, "detail": "d", "touches": []},
        ]
        self._scaffold()

        source = (self.out_dir / "automation.py").read_text()
        compile(source, "automation.py", "exec")  # still valid Python
        result = subprocess.run(
            [sys.executable, str(self.out_dir / "automation.py")],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists(), "injected payload executed — gate bypassed")

    def test_scaffold_handles_a_schematic_with_no_steps(self):
        self.schematic["steps"] = []
        self._scaffold()
        source = (self.out_dir / "automation.py").read_text()
        compile(source, "automation.py", "exec")
        result = subprocess.run(
            [sys.executable, str(self.out_dir / "automation.py")],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
