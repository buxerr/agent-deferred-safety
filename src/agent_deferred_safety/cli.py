"""CLI for auditing agent deferred-tool JSONL traces."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .audit import audit_events, read_jsonl


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-resume-audit",
        description="Check agent tool traces for missing permission checks after session resume.",
    )
    parser.add_argument("trace", type=Path, help="JSON Lines file with normalized hook and tool events")
    parser.add_argument("--json", action="store_true", help="print machine-readable audit output")
    args = parser.parse_args(argv)
    try:
        findings = audit_events(read_jsonl(args.trace))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"Cannot audit trace: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"status": "failed" if findings else "passed", "findings": [item.as_dict() for item in findings]}, indent=2))
    elif findings:
        for finding in findings:
            print(f"line {finding.line}: {finding.code}: {finding.message}")
    else:
        print("Trace passed: every tool start had a fresh allow decision.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
