"""Reproduce the reported cross-run stale approval with a scripted model.

This uses OpenAI Agents SDK's local ``ScriptedModel`` and needs no API key.
It prints the tool effects observed on the installed SDK version; it makes no
assertions so it can also show a changed outcome if the SDK is later fixed.
"""

from __future__ import annotations

import asyncio
from importlib.metadata import PackageNotFoundError, version

from agents import Agent, RunConfig, Runner
from agents.decorators import tool
from agents.testing import ScriptedModel, assistant_message, function_call


executed: list[str] = []


@tool(needs_approval=True)
async def write_marker(path: str) -> str:
    """Record one harmless in-memory side effect."""
    executed.append(path)
    return "written"


def model_script() -> ScriptedModel:
    return ScriptedModel(
        [
            [function_call("write_marker", '{"path":"A"}', call_id="call_fixed")],
            [assistant_message("done")],
        ]
    )


async def reproduce() -> int:
    try:
        sdk_version = version("openai-agents")
    except PackageNotFoundError:
        sdk_version = "unknown"
    print(f"openai-agents version: {sdk_version}")

    agent = Agent(
        name="approval-repro",
        instructions="Use the write_marker tool when asked.",
        tools=[write_marker],
        model=model_script(),
    )
    run_config = RunConfig(tracing_disabled=True)

    first = await Runner.run(agent, "write marker A", run_config=run_config)
    if not first.interruptions:
        print("First run did not interrupt for approval.")
        print(f"Tool effects: {executed!r}")
        return 2

    stale_approval = first.interruptions[0]
    first_state = first.to_state()
    first_state.approve(stale_approval)
    await Runner.run(agent, first_state, run_config=run_config)
    print(f"Effects after approved first run: {executed!r}")

    # The second run uses the same Agent and scripted call identity, as in the
    # issue report. Only the approval item is stale across the run boundary.
    agent.model = model_script()
    second = await Runner.run(agent, "write marker A again", run_config=run_config)
    if not second.interruptions:
        print("Second run did not interrupt; no stale-item transfer attempted.")
        print(f"Tool effects after second run: {executed!r}")
        return 2

    second_state = second.to_state()
    try:
        second_state.approve(stale_approval)
    except Exception as exc:
        print(f"Stale approval rejected: {type(exc).__name__}: {exc}")
        print(f"Tool effects after second run: {executed!r}")
        return 0

    print("Stale approval accepted by RunState.approve().")
    resumed = await Runner.run(agent, second_state, run_config=run_config)
    print(f"Remaining interruptions: {len(resumed.interruptions)}")
    print(f"Tool effects after second run: {executed!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(reproduce()))
