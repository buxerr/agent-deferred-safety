import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_deferred_safety import audit_events, read_jsonl


def event(kind, **values):
    return {"type": kind, **values}


class DeferredResumeAuditTests(unittest.TestCase):
    def test_allows_a_fresh_allow_after_resume(self):
        trace = [
            event("hook_decision", tool_use_id="t1", attempt=1, decision="defer"),
            event("session_resumed"),
            event("hook_decision", tool_use_id="t1", attempt=2, decision="allow"),
            event("tool_start", tool_use_id="t1", attempt=2),
        ]
        self.assertEqual([], audit_events(trace))

    def test_catches_execution_without_rerunning_hook_after_resume(self):
        trace = [
            event("hook_decision", tool_use_id="t1", attempt=1, decision="allow"),
            event("session_resumed"),
            event("tool_start", tool_use_id="t1", attempt=2),
        ]
        findings = audit_events(trace)
        self.assertEqual(["missing-hook-check"], [item.code for item in findings])

    def test_catches_tool_start_after_deny_or_defer(self):
        trace = [
            event("hook_decision", tool_use_id="denied", attempt=1, decision="deny"),
            event("tool_start", tool_use_id="denied", attempt=1),
            event("hook_decision", tool_use_id="paused", attempt=1, decision="defer"),
            event("tool_start", tool_use_id="paused", attempt=1),
        ]
        self.assertEqual(["non-allow-executed", "non-allow-executed"], [item.code for item in audit_events(trace)])

    def test_session_epochs_are_isolated(self):
        trace = [
            event("hook_decision", session_id="a", tool_use_id="t1", attempt=1, decision="allow"),
            event("session_resumed", session_id="b"),
            event("tool_start", session_id="a", tool_use_id="t1", attempt=1),
        ]
        self.assertEqual([], audit_events(trace))

    def test_jsonl_reader_and_cli_report_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "trace.jsonl"
            path.write_text(json.dumps(event("tool_start", tool_use_id="t1", attempt=1)) + "\n", encoding="utf-8")
            self.assertEqual(1, len(read_jsonl(path)))
            result = subprocess.run(
                [sys.executable, "-m", "agent_deferred_safety.cli", str(path), "--json"],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(1, result.returncode)
        self.assertIn("missing-hook-check", result.stdout)


if __name__ == "__main__":
    unittest.main()
