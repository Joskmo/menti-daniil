import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    state: str
    token: str | None = None

    def __bool__(self) -> bool:
        return self.state == "claimed"


@dataclass(frozen=True, slots=True)
class DeliveryWork:
    delivery_id: str
    event: str
    payload: bytes
    token: str


@dataclass(frozen=True, slots=True)
class TaskMapping:
    row_id: str
    task_id: str
    branch_name: str | None
    branch_url: str | None
    pr_url: str | None
    pr_number: int | None
    pr_state: str | None
    pr_updated_at: str | None


class SQLiteStore:
    def __init__(
        self,
        path: str | Path,
        *,
        delivery_lease_seconds: float = 300,
        completed_delivery_retention: int = 10_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if delivery_lease_seconds <= 0:
            raise ValueError("delivery_lease_seconds must be positive")
        if completed_delivery_retention < 0:
            raise ValueError("completed_delivery_retention must not be negative")
        self.path = Path(path)
        self.delivery_lease_seconds = delivery_lease_seconds
        self.completed_delivery_retention = completed_delivery_retention
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_mappings (
                    row_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    branch_name TEXT UNIQUE,
                    branch_url TEXT,
                    pr_url TEXT
                )
                """
            )
            task_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(task_mappings)")
            }
            for column, declaration in (
                ("pr_number", "INTEGER"),
                ("pr_state", "TEXT"),
                ("pr_updated_at", "TEXT"),
            ):
                if column not in task_columns:
                    connection.execute(
                        f"ALTER TABLE task_mappings ADD COLUMN {column} {declaration}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            delivery_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(webhook_deliveries)")
            }
            for column, declaration in (
                ("status", "TEXT NOT NULL DEFAULT 'completed'"),
                ("event", "TEXT"),
                ("payload", "BLOB"),
                ("lease_expires_at", "REAL"),
                ("lease_token", "TEXT"),
                ("completed_at", "REAL"),
            ):
                if column not in delivery_columns:
                    connection.execute(
                        f"ALTER TABLE webhook_deliveries ADD COLUMN {column} {declaration}"
                    )

    def get_or_allocate(self, row_id: str, existing_task_id: str | None = None) -> TaskMapping:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM task_mappings WHERE row_id = ?", (row_id,)
            ).fetchone()
            if row:
                mapping = self._mapping(row)
                if existing_task_id and mapping.task_id != existing_task_id:
                    raise ValueError(
                        f"Task {row_id} changed ID from {mapping.task_id} "
                        f"to {existing_task_id}"
                    )
                return mapping

            task_id = existing_task_id or self._next_task_id(connection)
            connection.execute(
                "INSERT INTO task_mappings (row_id, task_id) VALUES (?, ?)",
                (row_id, task_id),
            )
            row = connection.execute(
                "SELECT * FROM task_mappings WHERE row_id = ?", (row_id,)
            ).fetchone()
            assert row is not None
            return self._mapping(row)

    def reserve_branch(self, row_id: str, branch_name: str) -> TaskMapping:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM task_mappings WHERE row_id = ?", (row_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown Yonote row: {row_id}")
            if row["branch_name"] is None:
                connection.execute(
                    "UPDATE task_mappings SET branch_name = ? WHERE row_id = ?",
                    (branch_name, row_id),
                )
                row = connection.execute(
                    "SELECT * FROM task_mappings WHERE row_id = ?", (row_id,)
                ).fetchone()
            assert row is not None
            return self._mapping(row)

    def record_branch(self, row_id: str, branch_name: str, branch_url: str) -> TaskMapping:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE task_mappings
                SET branch_name = ?, branch_url = ?
                WHERE row_id = ?
                """,
                (branch_name, branch_url, row_id),
            )
            row = connection.execute(
                "SELECT * FROM task_mappings WHERE row_id = ?", (row_id,)
            ).fetchone()
            assert row is not None
            return self._mapping(row)

    def find_by_branch(self, branch_name: str) -> TaskMapping | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_mappings WHERE branch_name = ?", (branch_name,)
            ).fetchone()
            return self._mapping(row) if row else None

    def record_pr(self, row_id: str, pr_url: str) -> TaskMapping:
        with self._connect() as connection:
            connection.execute(
                "UPDATE task_mappings SET pr_url = ? WHERE row_id = ?",
                (pr_url, row_id),
            )
            row = connection.execute(
                "SELECT * FROM task_mappings WHERE row_id = ?", (row_id,)
            ).fetchone()
            assert row is not None
            return self._mapping(row)

    def record_pr_event(
        self,
        row_id: str,
        pr_url: str,
        pr_number: int,
        pr_state: str,
        pr_updated_at: str,
    ) -> TaskMapping:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE task_mappings
                SET pr_url = ?, pr_number = ?, pr_state = ?, pr_updated_at = ?
                WHERE row_id = ?
                """,
                (pr_url, pr_number, pr_state, pr_updated_at, row_id),
            )
            row = connection.execute(
                "SELECT * FROM task_mappings WHERE row_id = ?", (row_id,)
            ).fetchone()
            assert row is not None
            return self._mapping(row)

    def claim_delivery(
        self,
        delivery_id: str,
        event: str | None = None,
        payload: bytes | None = None,
    ) -> DeliveryClaim:
        now = self.clock()
        lease_expires_at = now + self.delivery_lease_seconds
        lease_token = secrets.token_urlsafe(24)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT status, event, payload, lease_expires_at
                FROM webhook_deliveries
                WHERE delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO webhook_deliveries (
                        delivery_id, status, event, payload,
                        lease_expires_at, lease_token, completed_at
                    ) VALUES (?, 'processing', ?, ?, ?, ?, NULL)
                    """,
                    (delivery_id, event, payload, lease_expires_at, lease_token),
                )
                return DeliveryClaim("claimed", lease_token)
            if event is not None and row["event"] not in (None, event):
                raise ValueError(f"Delivery event changed for {delivery_id}")
            if payload is not None and row["payload"] not in (None, payload):
                raise ValueError(f"Delivery payload changed for {delivery_id}")
            if row["status"] == "completed":
                return DeliveryClaim("completed")
            current_lease = row["lease_expires_at"]
            if current_lease is not None and current_lease > now:
                return DeliveryClaim("busy")
            connection.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'processing', event = COALESCE(event, ?),
                    payload = COALESCE(payload, ?), lease_expires_at = ?, lease_token = ?,
                    completed_at = NULL
                WHERE delivery_id = ?
                """,
                (event, payload, lease_expires_at, lease_token, delivery_id),
            )
            return DeliveryClaim("claimed", lease_token)

    def claim_next_delivery(self) -> DeliveryWork | None:
        now = self.clock()
        lease_expires_at = now + self.delivery_lease_seconds
        lease_token = secrets.token_urlsafe(24)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT delivery_id, event, payload
                FROM webhook_deliveries
                WHERE event IS NOT NULL AND payload IS NOT NULL
                  AND (
                      status = 'pending'
                      OR (status = 'processing' AND lease_expires_at <= ?)
                  )
                ORDER BY received_at, rowid
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'processing', lease_expires_at = ?, lease_token = ?
                WHERE delivery_id = ?
                """,
                (lease_expires_at, lease_token, row["delivery_id"]),
            )
            return DeliveryWork(
                delivery_id=row["delivery_id"],
                event=row["event"],
                payload=bytes(row["payload"]),
                token=lease_token,
            )

    def complete_delivery(self, delivery_id: str, lease_token: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'completed', event = NULL, payload = NULL,
                    lease_expires_at = NULL, lease_token = NULL, completed_at = ?
                WHERE delivery_id = ? AND status = 'processing' AND lease_token = ?
                """,
                (self.clock(), delivery_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Delivery lease is not owned: {delivery_id}")
            connection.execute(
                """
                DELETE FROM webhook_deliveries
                WHERE status = 'completed'
                  AND delivery_id NOT IN (
                      SELECT delivery_id
                      FROM webhook_deliveries
                      WHERE status = 'completed'
                      ORDER BY completed_at DESC, rowid DESC
                      LIMIT ?
                  )
                """,
                (self.completed_delivery_retention,),
            )

    def release_delivery(self, delivery_id: str, lease_token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'pending', lease_expires_at = NULL, lease_token = NULL
                WHERE delivery_id = ? AND status = 'processing' AND lease_token = ?
                """,
                (delivery_id, lease_token),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _next_task_id(connection: sqlite3.Connection) -> str:
        numbers = []
        for row in connection.execute("SELECT task_id FROM task_mappings"):
            match = re.fullmatch(r"PY-(\d+)", row[0])
            if match:
                numbers.append(int(match.group(1)))
        return f"PY-{max(numbers, default=0) + 1:03d}"

    @staticmethod
    def _mapping(row: sqlite3.Row) -> TaskMapping:
        return TaskMapping(
            row_id=row["row_id"],
            task_id=row["task_id"],
            branch_name=row["branch_name"],
            branch_url=row["branch_url"],
            pr_url=row["pr_url"],
            pr_number=row["pr_number"],
            pr_state=row["pr_state"],
            pr_updated_at=row["pr_updated_at"],
        )
