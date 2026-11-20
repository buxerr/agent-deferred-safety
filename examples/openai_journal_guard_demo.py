"""Show the journal blocking a stale approval at an OpenAI tool boundary.

The scripted model requires no provider credentials. If the SDK rejects the
stale approval first, the demo reports that. If the SDK accepts it, the tool
callback checks the host journal and refuses the effect before it runs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import tempfile

from agents import Agent, RunConfig, Runner
from agents.decorators import tool
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext

from agent_deferred_safety import InvalidApprovalError, ToolExecutionJournal


TOOL_NAME = "write_marker"
CALL_ID = "call_fixed"
OPERATION_KEY = "demo-operation-1"
ARGUMENTS = {"path": "A"}
executed: list[str] = []
guard_blocks: list[str] = []


@dataclass
class DemoContext:
    run_id: str
    journal: ToolExecutionJournal
    approval_ids: dict[str, str] = field(default_factory=dict)
    operation_key: str = OPERATION_KEY


@tool(needs_approval=True)
async def write_marker(ctx: ToolContext[DemoContext], path: str) -> str:
    """Write a harmless in-memory marker after host-side authorization."""
    approval_id = ctx.context.approval_ids.get(ctx.tool_call_id)
    if approval_id is None:
        raise InvalidApprovalError("no host approval is bound to this tool call")

    try:
        claim = ctx.context.journal.begin_effect(
            approval_id=approval_id,
            run_id=ctx.context.run_id,
            call_id=ctx.tool_call_id,
            tool_name=ctx.tool_name,
            arguments={"path": path},
            operation_key=ctx.context.operation_key,
        )
    except InvalidApprovalError as exc:
        guard_blocks.append(str(exc))
        raise

    if claim.status == "unknown":
        guard_blocks.append("operation outcome is unknown")
        raise RuntimeError("operation outcome is unknown; reconcile before retrying")
    if claim.status == "completed":
        return str(claim.result)

    executed.append(path)
    result = {"written": path}
    ctx.context.journal.complete(claim.operation_key, result)
    return "written"


def model_script() -> ScriptedModel:
    return ScriptedModel(
        [
            [function_call(TOOL_NAME, '{"path":"A"}', call_id=CALL_ID)],
            [assistant_message("done")],
        ]
    )


async def demonstrate() -> int:
    executed.clear()
    guard_blocks.clear()
    with tempfile.TemporaryDirectory(prefix="agent-safety-openai-guard-") as folder:
        with ToolExecutionJournal(Path(folder) / "journal.sqlite3") as journal:
            first_context = DemoContext(run_id="run-1", journal=journal)
            agent = Agent(
                name="guard-demo",
                instructions="Use write_marker when asked.",
                tools=[write_marker],
                model=model_script(),
            )
            run_config = RunConfig(tracing_disabled=True)

            first = await Runner.run(
                agent,
                "Write marker A",
                context=first_context,
                run_config=run_config,
            )
            if not first.interruptions:
                print("First run did not interrupt for approval.")
                return 2

            original_item = first.interruptions[0]
            approval_id = journal.approve(
                run_id=first_context.run_id,
                call_id=CALL_ID,
                operation_key=OPERATION_KEY,
                tool_name=TOOL_NAME,
                arguments=ARGUMENTS,
            )
            first_context.approval_ids[CALL_ID] = approval_id
            first_state = first.to_state()
            first_state.approve(original_item)
            await Runner.run(
                agent,
                first_state,
                context=first_context,
                run_config=run_config,
            )
            print(f"Effects after the correctly approved run: {executed!r}")

            # The second run deliberately carries the old host grant. The
            # SDK issue reports that its matching tool call can accept the
            # old ToolApprovalItem when the call ID and arguments are reused.
            agent.model = model_script()
            second_context = DemoContext(
                run_id="run-2",
                journal=journal,
                approval_ids={CALL_ID: approval_id},
            )
            second = await Runner.run(
                agent,
                "Write marker A again",
                context=second_context,
                run_config=run_config,
            )
            if not second.interruptions:
                print("Second run did not interrupt; no stale approval was attempted.")
                return 2

            second_state = second.to_state()
            try:
                second_state.approve(original_item)
            except Exception as exc:
                print(f"SDK rejected stale approval: {type(exc).__name__}: {exc}")
            else:
                print("SDK accepted stale approval; resuming into host-side journal guard.")
                try:
                    await Runner.run(
                        agent,
                        second_state,
                        context=second_context,
                        run_config=run_config,
                    )
                except Exception as exc:
                    print(f"Resume stopped: {type(exc).__name__}: {exc}")

            print(f"Journal guard decisions: {guard_blocks!r}")
            print(f"Final side effects: {executed!r}")
            return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(demonstrate()))
