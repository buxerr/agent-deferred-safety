"""Durable approval and side-effect journal for tool execution.

The journal binds approvals to one run, tool call, operation key, tool name,
and canonical argument digest. It records a side effect as ``started`` before
the caller performs it. If the process dies before recording completion, a
later claim returns ``unknown`` instead of silently performing it again.

This is a conservative host-side primitive, not an exactly-once guarantee.
For safe retries after an ambiguous outcome, the destination must support a
stable idempotency key or the host must reconcile the effect first.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator, Literal
import uuid


class JournalError(Exception):
    """Base class for journal errors."""


class InvalidApprovalError(JournalError):
    """The approval is missing, stale, mismatched, or already consumed."""


class OperationConflictError(JournalError):
    """An operation key was reused for a different tool or argument set."""


class InvalidTransitionError(JournalError):
    """The requested operation state transition is not valid."""


class JournalClosedError(JournalError):
    """The journal has already been closed."""


@dataclass(frozen=True)
class EffectClaim:
    """Decision returned before a host calls an external tool.

    ``execute`` means this claim owns the first attempt and may call the
    external effect. ``completed`` means a prior attempt completed and its
    recorded result is available. ``unknown`` means an earlier attempt was
    started but never reconciled; callers must not repeat it blindly.
    """

    status: Literal["execute", "completed", "unknown"]
    operation_key: str
    attempt: int
    result: Any = None


class ToolExecutionJournal:
    """SQLite-backed run-scoped approvals and effect outcome tracking.

    Arguments are stored only as a SHA-256 digest. Completed results are
    stored as JSON so a caller can return the original result on a duplicate
    request; avoid placing secrets in results, or protect the database with
    the host application's normal encryption and access controls.
    """

    def __init__(self, database: str | Path) -> None:
        database_name = str(database)
        if database_name != ":memory:":
            Path(database_name).parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            database_name,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        if database_name != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                operation_key TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('approved', 'consumed')),
                created_at_ns INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS operations (
                operation_key TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                arguments_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('started', 'completed', 'not_applied')
                ),
                attempt_count INTEGER NOT NULL,
                result_json TEXT,
                updated_at_ns INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS attempts (
                operation_key TEXT NOT NULL REFERENCES operations(operation_key),
                attempt INTEGER NOT NULL,
                run_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                approval_id TEXT NOT NULL REFERENCES approvals(approval_id),
                status TEXT NOT NULL CHECK (
                    status IN ('started', 'completed', 'not_applied')
                ),
                started_at_ns INTEGER NOT NULL,
                finished_at_ns INTEGER,
                result_json TEXT,
                resolution TEXT,
                PRIMARY KEY (operation_key, attempt)
            );
            """
        )

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise JournalClosedError("journal is closed")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    @staticmethod
    def _required_text(name: str, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value

    @staticmethod
    def _arguments_digest(arguments: Any) -> str:
        try:
            encoded = json.dumps(
                arguments,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("arguments must be JSON-serializable") from exc
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _encode_result(result: Any) -> str:
        try:
            return json.dumps(
                result,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("result must be JSON-serializable") from exc

    def approve(
        self,
        *,
        run_id: str,
        call_id: str,
        operation_key: str,
        tool_name: str,
        arguments: Any,
    ) -> str:
        """Record one approval and return its opaque identifier.

        The application is responsible for obtaining the human decision
        before calling this method. The returned identifier is valid only for
        the exact run, call, operation key, tool, and argument content supplied
        here.
        """
        run_id = self._required_text("run_id", run_id)
        call_id = self._required_text("call_id", call_id)
        operation_key = self._required_text("operation_key", operation_key)
        tool_name = self._required_text("tool_name", tool_name)
        digest = self._arguments_digest(arguments)
        approval_id = uuid.uuid4().hex
        now = time.time_ns()

        with self._write() as connection:
            connection.execute(
                """INSERT INTO approvals (
                    approval_id, run_id, call_id, operation_key, tool_name,
                    arguments_digest, status, created_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, 'approved', ?)""",
                (approval_id, run_id, call_id, operation_key, tool_name, digest, now),
            )
        return approval_id

    def begin_effect(
        self,
        *,
        approval_id: str,
        run_id: str,
        call_id: str,
        tool_name: str,
        arguments: Any,
        operation_key: str,
    ) -> EffectClaim:
        """Claim an effect before executing it outside the journal.

        ``operation_key`` must be stable for retries of the same real-world
        operation. A fresh SDK call ID alone may not be stable across runs.
        The journal commits the ``started`` record and consumes the approval
        in one SQLite transaction before returning ``status='execute'``.
        """
        approval_id = self._required_text("approval_id", approval_id)
        run_id = self._required_text("run_id", run_id)
        call_id = self._required_text("call_id", call_id)
        tool_name = self._required_text("tool_name", tool_name)
        operation_key = self._required_text("operation_key", operation_key)
        digest = self._arguments_digest(arguments)

        with self._write() as connection:
            approval = connection.execute(
                "SELECT * FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if approval is None or approval["status"] != "approved":
                raise InvalidApprovalError("approval is missing or already consumed")
            if (
                approval["run_id"] != run_id
                or approval["call_id"] != call_id
                or approval["operation_key"] != operation_key
                or approval["tool_name"] != tool_name
                or approval["arguments_digest"] != digest
            ):
                raise InvalidApprovalError(
                    "approval does not match this run, call, operation, tool, and arguments"
                )

            operation = connection.execute(
                "SELECT * FROM operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()

            if operation is not None:
                if (
                    operation["tool_name"] != tool_name
                    or operation["arguments_digest"] != digest
                ):
                    raise OperationConflictError(
                        "operation_key was previously used for different content"
                    )

                if operation["status"] == "completed":
                    self._consume_approval(connection, approval_id)
                    return EffectClaim(
                        "completed",
                        operation_key,
                        int(operation["attempt_count"]),
                        json.loads(operation["result_json"]),
                    )

                if operation["status"] == "started":
                    self._consume_approval(connection, approval_id)
                    return EffectClaim(
                        "unknown", operation_key, int(operation["attempt_count"])
                    )

                attempt = int(operation["attempt_count"]) + 1
                connection.execute(
                    """UPDATE operations SET status = 'started',
                        attempt_count = ?, updated_at_ns = ?
                        WHERE operation_key = ?""",
                    (attempt, time.time_ns(), operation_key),
                )
            else:
                attempt = 1
                connection.execute(
                    """INSERT INTO operations (
                        operation_key, tool_name, arguments_digest, status,
                        attempt_count, result_json, updated_at_ns
                    ) VALUES (?, ?, ?, 'started', 1, NULL, ?)""",
                    (operation_key, tool_name, digest, time.time_ns()),
                )

            self._consume_approval(connection, approval_id)
            connection.execute(
                """INSERT INTO attempts (
                    operation_key, attempt, run_id, call_id, approval_id,
                    status, started_at_ns
                ) VALUES (?, ?, ?, ?, ?, 'started', ?)""",
                (
                    operation_key,
                    attempt,
                    run_id,
                    call_id,
                    approval_id,
                    time.time_ns(),
                ),
            )
            return EffectClaim("execute", operation_key, attempt)

    @staticmethod
    def _consume_approval(connection: sqlite3.Connection, approval_id: str) -> None:
        connection.execute(
            "UPDATE approvals SET status = 'consumed' WHERE approval_id = ?",
            (approval_id,),
        )

    def complete(self, operation_key: str, result: Any) -> None:
        """Durably record a completed side effect and its JSON result."""
        operation_key = self._required_text("operation_key", operation_key)
        result_json = self._encode_result(result)
        with self._write() as connection:
            self._complete_in_transaction(
                connection, operation_key, result_json, resolution=None
            )

    def _complete_in_transaction(
        self,
        connection: sqlite3.Connection,
        operation_key: str,
        result_json: str,
        resolution: str | None,
    ) -> None:
        operation = connection.execute(
            "SELECT * FROM operations WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        if operation is None:
            raise InvalidTransitionError("operation has not been started")
        if operation["status"] == "completed":
            if operation["result_json"] != result_json:
                raise OperationConflictError(
                    "operation is complete with a different recorded result"
                )
            return
        if operation["status"] != "started":
            raise InvalidTransitionError(
                "only a started operation can be marked completed"
            )

        attempt = connection.execute(
            """SELECT attempt FROM attempts
                WHERE operation_key = ? AND status = 'started'
                ORDER BY attempt DESC LIMIT 1""",
            (operation_key,),
        ).fetchone()
        if attempt is None:
            raise InvalidTransitionError("operation has no active attempt")

        now = time.time_ns()
        connection.execute(
            """UPDATE attempts SET status = 'completed', finished_at_ns = ?,
                result_json = ?, resolution = ?
                WHERE operation_key = ? AND attempt = ?""",
            (now, result_json, resolution, operation_key, int(attempt["attempt"])),
        )
        connection.execute(
            """UPDATE operations SET status = 'completed', result_json = ?,
                updated_at_ns = ? WHERE operation_key = ?""",
            (result_json, now, operation_key),
        )

    def reconcile(
        self,
        operation_key: str,
        *,
        applied: bool,
        reason: str,
        result: Any = None,
    ) -> None:
        """Resolve a started operation after checking the real destination.

        If the effect happened, pass ``applied=True`` and its safe-to-store
        result. If it definitely did not happen, pass ``applied=False`` and a
        fresh approval will be required before another attempt can start.
        """
        operation_key = self._required_text("operation_key", operation_key)
        reason = self._required_text("reason", reason)
        if not applied and result is not None:
            raise ValueError("result must be omitted when applied is false")
        result_json = self._encode_result(result) if applied else None

        with self._write() as connection:
            operation = connection.execute(
                "SELECT * FROM operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if operation is None or operation["status"] != "started":
                raise InvalidTransitionError(
                    "only a started operation can be reconciled"
                )

            if applied:
                if result_json is None:
                    raise ValueError("applied results must be JSON-serializable")
                self._complete_in_transaction(
                    connection, operation_key, result_json, resolution=reason
                )
                return

            attempt = connection.execute(
                """SELECT attempt FROM attempts
                    WHERE operation_key = ? AND status = 'started'
                    ORDER BY attempt DESC LIMIT 1""",
                (operation_key,),
            ).fetchone()
            if attempt is None:
                raise InvalidTransitionError("operation has no active attempt")

            now = time.time_ns()
            connection.execute(
                """UPDATE attempts SET status = 'not_applied',
                    finished_at_ns = ?, resolution = ?
                    WHERE operation_key = ? AND attempt = ?""",
                (now, reason, operation_key, int(attempt["attempt"])),
            )
            connection.execute(
                """UPDATE operations SET status = 'not_applied',
                    result_json = NULL, updated_at_ns = ?
                    WHERE operation_key = ?""",
                (now, operation_key),
            )

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> ToolExecutionJournal:
        if self._closed:
            raise JournalClosedError("journal is closed")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
