"""Demonstrate recovery after an effect is applied but its result is not saved.

This exercises the local journal only. It does not start an SDK or contact a
model provider. The first child process exits abruptly after writing a marker
and before calling ``complete``; the second process must report ``unknown``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_deferred_safety import ToolExecutionJournal  # noqa: E402


TOOL_NAME = "write_marker"
ARGUMENTS = {"path": "demo-marker"}
OPERATION_KEY = "demo-payment-1"


def append_effect(path: Path, run_id: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"operation_key": OPERATION_KEY, "run_id": run_id}) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def run_crash_phase(database: Path, effects: Path) -> int:
    journal = ToolExecutionJournal(database)
    approval_id = journal.approve(
        run_id="run-before-crash",
        call_id="call-before-crash",
        operation_key=OPERATION_KEY,
        tool_name=TOOL_NAME,
        arguments=ARGUMENTS,
    )
    claim = journal.begin_effect(
        approval_id=approval_id,
        run_id="run-before-crash",
        call_id="call-before-crash",
        tool_name=TOOL_NAME,
        arguments=ARGUMENTS,
        operation_key=OPERATION_KEY,
    )
    if claim.status != "execute":
        journal.close()
        print(f"Unexpected first claim: {claim.status}", flush=True)
        return 2

    append_effect(effects, "run-before-crash")
    print("Effect marker written; exiting before journal.complete().", flush=True)
    # Exit without running context-manager cleanup to model abrupt process loss.
    os._exit(23)


def run_resume_phase(database: Path, effects: Path) -> int:
    journal = ToolExecutionJournal(database)
    try:
        approval_id = journal.approve(
            run_id="run-after-resume",
            call_id="call-after-resume",
            operation_key=OPERATION_KEY,
            tool_name=TOOL_NAME,
            arguments=ARGUMENTS,
        )
        claim = journal.begin_effect(
            approval_id=approval_id,
            run_id="run-after-resume",
            call_id="call-after-resume",
            tool_name=TOOL_NAME,
            arguments=ARGUMENTS,
            operation_key=OPERATION_KEY,
        )
        if claim.status == "execute":
            append_effect(effects, "run-after-resume")
            journal.complete(claim.operation_key, {"written": True})
            print("The journal allowed a second effect attempt.")
            return 1
        if claim.status == "completed":
            print(f"Prior result returned: {claim.result!r}")
        else:
            print("Resume result: unknown; no second effect was performed.")
        return 0
    finally:
        journal.close()


def run_demo() -> int:
    with tempfile.TemporaryDirectory(prefix="agent-safety-crash-demo-") as folder:
        root = Path(folder)
        database = root / "journal.sqlite3"
        effects = root / "effects.jsonl"

        crashed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--phase", "crash", "--database", str(database), "--effects", str(effects)],
            capture_output=True,
            text=True,
            check=False,
        )
        print(crashed.stdout, end="")
        print(f"Crash child exit code: {crashed.returncode}")
        if crashed.returncode != 23:
            print(crashed.stderr, end="", file=sys.stderr)
            return 2

        resumed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--phase", "resume", "--database", str(database), "--effects", str(effects)],
            capture_output=True,
            text=True,
            check=False,
        )
        print(resumed.stdout, end="")
        if resumed.returncode != 0:
            print(resumed.stderr, end="", file=sys.stderr)
            return resumed.returncode

        effect_count = len(effects.read_text(encoding="utf-8").splitlines())
        print(f"Recorded side effects: {effect_count}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("all", "crash", "resume"), default="all")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--effects", type=Path)
    arguments = parser.parse_args()

    if arguments.phase == "all":
        return run_demo()
    if arguments.database is None or arguments.effects is None:
        parser.error("--database and --effects are required for child phases")
    if arguments.phase == "crash":
        return run_crash_phase(arguments.database, arguments.effects)
    return run_resume_phase(arguments.database, arguments.effects)


if __name__ == "__main__":
    raise SystemExit(main())
