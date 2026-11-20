"""Demonstrate that an approval cannot be moved to a different run."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_deferred_safety import InvalidApprovalError, ToolExecutionJournal  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agent-safety-approval-demo-") as folder:
        with ToolExecutionJournal(Path(folder) / "journal.sqlite3") as journal:
            approval_id = journal.approve(
                run_id="run-1",
                call_id="call-1",
                operation_key="operation-1",
                tool_name="write_marker",
                arguments={"path": "marker-a"},
            )

            try:
                journal.begin_effect(
                    approval_id=approval_id,
                    run_id="run-2",
                    call_id="call-2",
                    operation_key="operation-1",
                    tool_name="write_marker",
                    arguments={"path": "marker-a"},
                )
            except InvalidApprovalError as exc:
                print(f"Old approval rejected: {exc}")
                return 0

            print("Unexpected result: approval transferred to another run.")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
