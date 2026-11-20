# Agent Deferred Tool Safety Audit and Journal

Audit agent SDK traces for unsafe tool behavior across run and process boundaries. Reported cases include [Claude deferred tools executing twice after a crash](https://github.com/anthropics/claude-agent-sdk-python/issues/1549), [a Python callback not re-running on resume](https://github.com/anthropics/claude-agent-sdk-python/issues/993), [a later defer being ignored during resume](https://github.com/anthropics/claude-code/issues/97196), and [an OpenAI approval transferring across runs](https://github.com/openai/openai-agents-python/issues/5413). These are reports against specific SDK versions, not proof that the behavior persists in every current release.

This package provides a normalized trace auditor and an experimental host-side execution journal. It does not patch either SDK or prove that every tool path is safe.

## Install

```sh
python -m pip install agent-deferred-safety
```

## Audit a trace

Instrument your SDK adapter to emit normalized JSON Lines events. Record the tool-use identifier, attempt number, session ID, and hook decision, but do not include tool arguments or secrets.

```jsonl
{"type":"hook_decision","session_id":"run-1","tool_use_id":"tool-7","attempt":1,"decision":"defer"}
{"type":"session_resumed","session_id":"run-1"}
{"type":"hook_decision","session_id":"run-1","tool_use_id":"tool-7","attempt":2,"decision":"allow"}
{"type":"tool_start","session_id":"run-1","tool_use_id":"tool-7","attempt":2}
```

Run the audit locally or in CI:

```sh
agent-resume-audit trace.jsonl
agent-resume-audit trace.jsonl --json
```

Exit codes: `0` passed, `1` unsafe or incomplete trace, `2` input error. A resume starts a new audit epoch, so an old allow decision cannot authorize a later start.

## Checks

- Every tool start has a matching hook decision from the same session epoch and attempt.
- A `deny` or `defer` decision is not followed by execution in that attempt.
- Duplicate start records for one attempt are reported.

The adapter must record events faithfully. Missing logs can produce false positives; a trace cannot prove that unrecorded execution did not happen. Add a side-effect marker test at the SDK boundary for end-to-end regression coverage.

## Experimental execution journal

`ToolExecutionJournal` adds a host-side primitive for two resume hazards: an old approval being reused in a new run, and an external effect being repeated after a crash before its result is saved. Approval records are bound to the run, call, stable operation key, tool name, and a digest of the arguments. Before the host performs an effect, the journal durably marks the operation as started and consumes that approval.

```python
from agent_deferred_safety import ToolExecutionJournal
from pathlib import Path

def write_marker(path: str) -> str:
    Path(path).write_text("written\n", encoding="utf-8")
    return "written"

with ToolExecutionJournal("agent-safety.sqlite3") as journal:
    approval_id = journal.approve(
        run_id="run-17",
        call_id="call-4",
        operation_key="business-operation-884",
        tool_name="write_marker",
        arguments={"path": "marker.txt"},
    )
    claim = journal.begin_effect(
        approval_id=approval_id,
        run_id="run-17",
        call_id="call-4",
        tool_name="write_marker",
        arguments={"path": "marker.txt"},
        operation_key="business-operation-884",
    )

    if claim.status == "execute":
        result = write_marker("marker.txt")
        journal.complete(claim.operation_key, {"written": result})
    elif claim.status == "completed":
        result = claim.result
    else:
        # A previous attempt started but has no recorded outcome. Reconcile
        # with the destination before deciding whether another attempt is safe.
        raise RuntimeError("operation outcome is unknown; do not execute again")
```

The `operation_key` must be namespaced and stable for the same real-world effect across retries. A `started` operation becomes `unknown` after a crash; the host must reconcile it with the destination before retrying. If the destination supports idempotency keys, use the same stable key there as well. This journal cannot provide exactly-once effects when the destination has no idempotency or reconciliation mechanism.

The journal stores argument digests and JSON results in SQLite. It does not store raw arguments, but digests are not encryption; avoid sensitive result fields and protect the database with the host application's normal access controls. This feature is experimental and does not yet include SDK adapters or a validated crash reproducer.

## Run the local scenarios

These no-API-key examples exercise the journal contract. The crash scenario launches a child process that exits after writing a marker but before saving the tool result; the resumed process should report an unknown outcome without repeating the marker. The second scenario passes an old approval into a different run and should reject it.

```sh
python examples/crash_window_demo.py
python examples/stale_approval_demo.py
```

These two scenarios demonstrate the local journal only. The OpenAI reproduction below exercises the actual Agents SDK with its scripted model; the Anthropic crash issue still needs an SDK adapter and current-version evidence.

### OpenAI Agents SDK reproduction

The reported stale-approval case can be exercised with the SDK's scripted model, without provider credentials:

```sh
python -m pip install -e ".[openai-repro]"
python examples/openai_stale_approval_repro.py
python examples/openai_journal_guard_demo.py
```

The script prints the installed `openai-agents` version, interruption behavior, and the in-memory side-effect count. It does not modify the SDK or assert that the report still reproduces on every version. See [issue #5413](https://github.com/openai/openai-agents-python/issues/5413) for the reported behavior and version-specific details.

The journal-guard demo passes the current call ID through the SDK's `ToolContext`. It shows the host-side check rejecting an old run's approval before the tool effect, even if `RunState.approve()` accepts that stale item.

## Development

```sh
python -m unittest discover -s tests -v
```

Keywords: agent SDK resume bug, deferred tool permission check, AI agent security regression test, tool-call authorization audit, Claude Agent SDK hooks.

## License

MIT
