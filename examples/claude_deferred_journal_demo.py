"""Exercise Claude deferred-tool recovery with a local scripted API.

The demo defers one in-process MCP tool, resumes it in a fresh process, commits
an idempotent destination effect, and exits before returning the tool result.
A second resume sees the same tool-use ID, finds the committed effect, and
reconciles the journal instead of applying it again. No API key or live model
is used.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from agent_deferred_safety import ToolExecutionJournal


TOOL = "mcp__payments__pay"
TOOL_USE_ID = "toolu_payment_1"


def _crash_process_tree() -> None:
    """Stop the host and its Claude CLI child after the destination commits."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(os.getpid())],
            capture_output=True,
            check=False,
        )
    else:
        os.killpg(os.getpid(), signal.SIGKILL)
    os._exit(17)


class ScriptedMessagesAPI(BaseHTTPRequestHandler):
    """Small Anthropic Messages API stub that returns one fixed tool call."""

    def log_message(self, *_args: Any) -> None:
        pass

    def _json(self, value: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._json({"data": []})

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.end_headers()

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        if "count_tokens" in self.path:
            self._json({"input_tokens": 1})
            return
        if "/v1/messages" not in self.path:
            self._json({})
            return

        request = json.loads(body or b"{}")
        tools = request.get("tools", []) or []
        messages = request.get("messages", []) or []
        has_tool_result = any(
            isinstance(message.get("content"), list)
            and any(
                block.get("type") == "tool_result"
                for block in message["content"]
                if isinstance(block, dict)
            )
            for message in messages
            if isinstance(message, dict)
        )
        use_tool = any(tool.get("name") == TOOL for tool in tools) and not has_tool_result
        blocks: list[dict[str, Any]]
        stop_reason: str
        if use_tool:
            blocks = [{
                "type": "tool_use",
                "id": TOOL_USE_ID,
                "name": TOOL,
                "input": {"to": "alice", "amount": 10},
            }]
            stop_reason = "tool_use"
        else:
            blocks = [{"type": "text", "text": "Done."}]
            stop_reason = "end_turn"

        message = {
            "id": "msg_" + uuid.uuid4().hex,
            "type": "message",
            "role": "assistant",
            "model": request.get("model", "claude-sonnet-5"),
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 0},
        }
        if not request.get("stream"):
            message.update(content=blocks, stop_reason=stop_reason)
            self._json(message)
            return

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def sse(event: str, data: dict[str, Any]) -> None:
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        sse("message_start", {"type": "message_start", "message": message})
        for index, block in enumerate(blocks):
            if block["type"] == "tool_use":
                sse("content_block_start", {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {**block, "input": {}},
                })
                sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(block["input"]),
                    },
                })
            else:
                sse("content_block_start", {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                })
                sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": block["text"]},
                })
            sse("content_block_stop", {"type": "content_block_stop", "index": index})
        sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 1},
        })
        sse("message_stop", {"type": "message_stop"})


class IdempotentPaymentDestination:
    """SQLite stand-in for a destination with atomic idempotency support."""

    def __init__(self, path: str) -> None:
        self.path = path
        with sqlite3.connect(path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS effects (
                    idempotency_key TEXT PRIMARY KEY,
                    recipient TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    result_json TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS callback_invocations (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    count INTEGER NOT NULL
                )"""
            )

    def record_callback_invocation(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """INSERT INTO callback_invocations(id, count) VALUES (1, 1)
                   ON CONFLICT(id) DO UPDATE SET count = count + 1"""
            )

    def get(self, idempotency_key: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT result_json FROM effects WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def apply(self, idempotency_key: str, to: str, amount: int) -> dict[str, Any]:
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT result_json FROM effects WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row:
                connection.execute("COMMIT")
                return json.loads(row[0])

            result = {"status": "paid", "to": to, "amount": amount}
            connection.execute(
                "INSERT INTO effects(idempotency_key, recipient, amount, result_json) VALUES (?, ?, ?, ?)",
                (idempotency_key, to, amount, json.dumps(result, sort_keys=True)),
            )
            connection.execute("COMMIT")
            return result
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def counts(self) -> tuple[int, int]:
        with sqlite3.connect(self.path) as connection:
            effects = connection.execute("SELECT COUNT(*) FROM effects").fetchone()[0]
            row = connection.execute(
                "SELECT count FROM callback_invocations WHERE id = 1"
            ).fetchone()
        return int(row[0]) if row else 0, int(effects)


async def run_child(phase: str, state_path: Path) -> None:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        HookMatcher,
        ResultMessage,
        ToolUseBlock,
        create_sdk_mcp_server,
        query,
        tool,
    )

    root = Path(os.environ["DEMO_ROOT"])
    journal_path = str(root / "journal.sqlite3")
    destination = IdempotentPaymentDestination(str(root / "destination.sqlite3"))
    state = json.loads(state_path.read_text("utf-8")) if phase != "defer" else {}
    session_id = state.get("session_id")
    if phase != "defer" and not session_id:
        raise RuntimeError("defer phase did not save a Claude session ID")

    run_id = uuid.uuid4().hex
    active_approvals: dict[str, tuple[str, str]] = {}
    with ToolExecutionJournal(journal_path) as journal:

        @tool("pay", "Make a demo payment", {
            "to": str,
            "amount": int,
            "idempotency_key": str,
        })
        async def pay(args: dict[str, Any]) -> dict[str, Any]:
            destination.record_callback_invocation()
            operation_key = args["idempotency_key"]
            authorization = active_approvals.pop(operation_key, None)
            if authorization is None:
                raise RuntimeError("PreToolUse did not create a journal approval")
            approval_id, call_id = authorization
            arguments = {"to": args["to"], "amount": args["amount"]}
            claim = journal.begin_effect(
                approval_id=approval_id,
                run_id=run_id,
                call_id=call_id,
                tool_name="pay",
                arguments=arguments,
                operation_key=operation_key,
            )

            if claim.status == "completed":
                result = claim.result
            elif claim.status == "unknown":
                result = destination.get(operation_key)
                if result is None:
                    journal.reconcile(
                        operation_key,
                        applied=False,
                        reason="destination has no record for this operation key",
                    )
                    raise RuntimeError("effect is absent; obtain fresh approval before retry")
                journal.reconcile(
                    operation_key,
                    applied=True,
                    reason="destination confirms the idempotency key was applied",
                    result=result,
                )
            else:
                result = destination.apply(
                    operation_key, args["to"], int(args["amount"])
                )
                if phase == "crash":
                    print("Simulating process loss after destination commit.", flush=True)
                    _crash_process_tree()
                journal.complete(operation_key, result)

            return {"content": [{"type": "text", "text": json.dumps(result)}]}

        async def pre_tool_use(input_data: dict[str, Any], tool_use_id: str | None, _context: Any) -> dict[str, Any]:
            if phase == "defer":
                return {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "defer",
                }}
            if not tool_use_id or not session_id:
                raise RuntimeError("resumed tool call is missing its stable identity")

            operation_key = f"claude:{session_id}:{tool_use_id}"
            arguments = {
                "to": input_data["tool_input"]["to"],
                "amount": int(input_data["tool_input"]["amount"]),
            }
            approval_id = journal.approve(
                run_id=run_id,
                call_id=tool_use_id,
                operation_key=operation_key,
                tool_name="pay",
                arguments=arguments,
            )
            active_approvals[operation_key] = (approval_id, tool_use_id)
            updated_input = dict(input_data["tool_input"])
            updated_input["idempotency_key"] = operation_key
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": updated_input,
            }}

        options = ClaudeAgentOptions(
            cwd=str(root / "work"),
            model="claude-sonnet-5",
            permission_mode="default",
            setting_sources=[],
            mcp_servers={"payments": create_sdk_mcp_server("payments", tools=[pay])},
            allowed_tools=[TOOL],
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]},
            resume=session_id,
            env={
                "CLAUDE_CONFIG_DIR": str(root / "claude-config"),
                "ANTHROPIC_BASE_URL": os.environ["ANTHROPIC_BASE_URL"],
                "ANTHROPIC_API_KEY": "local-stub",
                "ANTHROPIC_AUTH_TOKEN": "local-stub",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "ENABLE_TOOL_SEARCH": "false",
            },
        )

        stop = asyncio.Event()

        async def prompts():
            if phase == "defer":
                yield {
                    "type": "user",
                    "message": {"role": "user", "content": "Pay Alice 10"},
                    "parent_tool_use_id": None,
                }
            await stop.wait()

        result_message: Any = None
        observed_tool_use_id: str | None = None
        async for message in query(prompt=prompts(), options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        observed_tool_use_id = block.id
            if isinstance(message, ResultMessage):
                result_message = message
                break
        stop.set()

    if result_message is None:
        raise RuntimeError("Claude SDK ended without a ResultMessage")
    outcome = {
        "session_id": result_message.session_id,
        "stop_reason": result_message.stop_reason,
        "deferred_tool_use_id": (
            result_message.deferred_tool_use.id
            if result_message.deferred_tool_use
            else None
        ),
        "observed_tool_use_id": observed_tool_use_id,
    }
    if phase == "defer":
        state_path.write_text(json.dumps(outcome), encoding="utf-8")
    print("RESULT " + json.dumps(outcome), flush=True)


def _child(phase: str, state_path: Path) -> None:
    asyncio.run(run_child(phase, state_path))


def _run_phase(phase: str, state_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        [sys.executable, __file__, "--child", phase, str(state_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=(os.name != "nt"),
    )
    try:
        stdout, stderr = process.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        return subprocess.CompletedProcess(process.args, 124, stdout, stderr)
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agent-deferred-claude-") as temp_dir:
        root = Path(temp_dir)
        (root / "work").mkdir()
        (root / "claude-config").mkdir()
        server = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedMessagesAPI)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = dict(os.environ)
        env.update({
            "DEMO_ROOT": str(root),
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
        })
        state_path = root / "session.json"
        try:
            deferred = _run_phase("defer", state_path, env)
            if deferred.returncode != 0:
                sys.stderr.write(deferred.stderr)
                raise RuntimeError("Claude defer phase failed")
            print(deferred.stdout, end="")

            crashed = _run_phase("crash", state_path, env)
            if crashed.returncode == 0:
                sys.stderr.write(crashed.stderr)
                raise RuntimeError("injected process loss did not occur")
            crash_invocations, crash_effects = IdempotentPaymentDestination(
                str(root / "destination.sqlite3")
            ).counts()
            if (crash_invocations, crash_effects) != (1, 1):
                sys.stderr.write(crashed.stderr)
                raise RuntimeError("the injected crash did not happen after one effect")
            print("Process exited after the destination committed its effect.")

            recovered = _run_phase("recover", state_path, env)
            if recovered.returncode != 0:
                sys.stderr.write(recovered.stderr)
                raise RuntimeError("Claude recovery phase failed")
            print(recovered.stdout, end="")

            invocations, effects = IdempotentPaymentDestination(
                str(root / "destination.sqlite3")
            ).counts()
            print(f"Tool callback invocations: {invocations}")
            print(f"Unique destination effects: {effects}")
            return 0 if invocations == 2 and effects == 1 else 1
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        _child(sys.argv[2], Path(sys.argv[3]))
    else:
        raise SystemExit(main())

