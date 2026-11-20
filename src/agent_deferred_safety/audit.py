"""Check whether tool execution is preceded by a fresh allow decision."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class AuditFinding:
    line: int
    code: str
    message: str
    tool_use_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError(f"line {line_number}: each JSONL record must be an object")
        event["_line"] = line_number
        events.append(event)
    return events


def audit_events(events: Iterable[dict[str, Any]]) -> list[AuditFinding]:
    """Audit normalized hook and execution events.

    Event types are ``session_resumed``, ``hook_decision`` and ``tool_start``.
    A resume advances the session epoch, so a prior permission decision cannot
    authorize execution after a restart. Unknown event types are ignored.
    """
    findings: list[AuditFinding] = []
    epoch: dict[str, int] = {}
    decisions: dict[tuple[str, str, str, int], str] = {}
    starts: set[tuple[str, str, str, int]] = set()

    for fallback_line, event in enumerate(events, start=1):
        line = int(event.get("_line", fallback_line))
        kind = event.get("type")
        session_id = str(event.get("session_id", "default"))
        if kind == "session_resumed":
            epoch[session_id] = epoch.get(session_id, 0) + 1
            continue
        if kind not in {"hook_decision", "tool_start"}:
            continue

        tool_use_id = event.get("tool_use_id")
        attempt = event.get("attempt")
        if not isinstance(tool_use_id, str) or not tool_use_id or attempt is None:
            findings.append(AuditFinding(line, "invalid-event", f"{kind} requires tool_use_id and attempt", None))
            continue
        key = (session_id, tool_use_id, str(attempt), epoch.get(session_id, 0))

        if kind == "hook_decision":
            decision = event.get("decision")
            if decision not in {"allow", "deny", "defer"}:
                findings.append(AuditFinding(line, "invalid-decision", "decision must be allow, deny, or defer", tool_use_id))
                continue
            decisions[key] = decision
            continue

        if key in starts:
            findings.append(AuditFinding(line, "duplicate-execution", "tool started more than once in the same attempt", tool_use_id))
            continue
        starts.add(key)
        decision = decisions.get(key)
        if decision is None:
            findings.append(AuditFinding(line, "missing-hook-check", "tool started without a hook decision in this session attempt", tool_use_id))
        elif decision != "allow":
            findings.append(AuditFinding(line, "non-allow-executed", f"tool started after a {decision!r} decision", tool_use_id))

    return findings
