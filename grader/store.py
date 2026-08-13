import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TASK_ID = re.compile(r"PY-[0-9]{3,9}")
_PROJECT = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_BRANCH = re.compile(r"task/PY-[0-9]{3,9}-[a-z0-9]+(?:-[a-z0-9]+)*")
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_HASH = re.compile(r"[0-9a-f]{64}")


class VaultConflictError(RuntimeError):
    """A suite path is unsafe or an immutable suite would be changed."""


@dataclass(frozen=True, slots=True)
class AuthoringJob:
    task_id: str
    row_id: str
    project: str
    branch_name: str
    starter_sha: str
    assignment_json: str
    state: str
    lease_token: str | None
    attempts: int
    suite_hash: str | None
    clarification_revision: int
    clarification_question: str | None
    clarification_answer: str | None
    critic_feedback_json: str | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class GradingAttempt:
    attempt_id: str
    task_id: str
    project: str
    branch_name: str
    commit_sha: str
    suite_hash: str
    state: str
    lease_token: str | None
    attempts: int
    passed: bool | None
    passed_count: int | None
    total_count: int | None
    private_report_json: str | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class CheckPublication:
    publication_id: str
    commit_sha: str
    passed: bool
    lease_token: str
    attempts: int


@dataclass(frozen=True, slots=True)
class MentorNotification:
    notification_id: str
    task_id: str
    commit_sha: str
    report_json: str
    lease_token: str
    attempts: int


@dataclass(frozen=True, slots=True)
class Clarification:
    nonce: str
    task_id: str
    revision: int
    question: str


@dataclass(frozen=True, slots=True)
class StoredSuite:
    task_id: str
    starter_sha: str
    suite_hash: str
    author_model: str
    suite_payload: dict[str, Any]


class GraderStore:
    def __init__(
        self,
        path: str | Path,
        *,
        lease_seconds: float = 300,
        max_grading_attempts: int = 5,
        grading_retry_base_seconds: float = 5,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if not 1 <= max_grading_attempts <= 20:
            raise ValueError("max_grading_attempts must be from 1 to 20")
        if grading_retry_base_seconds <= 0 or grading_retry_base_seconds > 3600:
            raise ValueError("grading_retry_base_seconds is outside the safe range")
        self.path = Path(path)
        self.lease_seconds = lease_seconds
        self.max_grading_attempts = max_grading_attempts
        self.grading_retry_base_seconds = grading_retry_base_seconds
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
                CREATE TABLE IF NOT EXISTS authoring_jobs (
                    task_id TEXT PRIMARY KEY,
                    row_id TEXT NOT NULL UNIQUE,
                    project TEXT NOT NULL,
                    branch_name TEXT NOT NULL UNIQUE,
                    starter_sha TEXT NOT NULL,
                    assignment_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    lease_expires_at REAL,
                    lease_token TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    suite_hash TEXT,
                    clarification_revision INTEGER NOT NULL DEFAULT 0,
                    clarification_question TEXT,
                    clarification_answer TEXT,
                    critic_feedback_json TEXT,
                    last_error_code TEXT,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            authoring_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(authoring_jobs)")
            }
            for column, declaration in (
                ("critic_feedback_json", "TEXT"),
                ("last_error_code", "TEXT"),
                ("next_attempt_at", "REAL NOT NULL DEFAULT 0"),
            ):
                if column not in authoring_columns:
                    connection.execute(
                        f"ALTER TABLE authoring_jobs ADD COLUMN {column} {declaration}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS clarifications (
                    nonce TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    answered_at REAL,
                    UNIQUE(task_id, revision),
                    FOREIGN KEY(task_id) REFERENCES authoring_jobs(task_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS grading_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    project TEXT NOT NULL,
                    branch_name TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    suite_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    lease_expires_at REAL,
                    lease_token TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    passed INTEGER,
                    passed_count INTEGER,
                    total_count INTEGER,
                    private_report_json TEXT,
                    last_error_code TEXT,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(task_id, commit_sha),
                    FOREIGN KEY(task_id) REFERENCES authoring_jobs(task_id)
                )
                """
            )
            grading_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(grading_attempts)")
            }
            if "next_attempt_at" not in grading_columns:
                connection.execute(
                    "ALTER TABLE grading_attempts "
                    "ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS check_publications (
                    publication_id TEXT PRIMARY KEY,
                    commit_sha TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    lease_expires_at REAL,
                    lease_token TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(publication_id) REFERENCES grading_attempts(attempt_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mentor_notifications (
                    notification_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    lease_expires_at REAL,
                    lease_token TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(notification_id) REFERENCES grading_attempts(attempt_id)
                )
                """
            )

    def enqueue_authoring(
        self,
        *,
        task_id: str,
        row_id: str,
        project: str,
        branch_name: str,
        starter_sha: str,
        assignment_json: str,
    ) -> AuthoringJob:
        _validate_identity(task_id, row_id, project, branch_name, starter_sha)
        assignment_json = _canonical_assignment(assignment_json)
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM authoring_jobs WHERE task_id = ? OR row_id = ?",
                (task_id, row_id),
            ).fetchone()
            if row is not None:
                existing = self._job(row)
                immutable = (
                    existing.task_id,
                    existing.row_id,
                    existing.project,
                    existing.branch_name,
                    existing.starter_sha,
                    existing.assignment_json,
                )
                incoming = (
                    task_id,
                    row_id,
                    project,
                    branch_name,
                    starter_sha,
                    assignment_json,
                )
                if immutable != incoming:
                    raise ValueError("authoring job input changed after enqueue")
                return existing
            connection.execute(
                """
                INSERT INTO authoring_jobs (
                    task_id, row_id, project, branch_name, starter_sha,
                    assignment_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    task_id,
                    row_id,
                    project,
                    branch_name,
                    starter_sha,
                    assignment_json,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM authoring_jobs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            assert row is not None
            return self._job(row)

    def claim_next_authoring(self) -> AuthoringJob | None:
        now = self.clock()
        lease_token = secrets.token_urlsafe(24)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM authoring_jobs
                WHERE (state = 'queued' AND next_attempt_at <= ?)
                   OR (state = 'authoring' AND lease_expires_at <= ?)
                ORDER BY created_at, task_id
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE authoring_jobs
                SET state = 'authoring', lease_token = ?, lease_expires_at = ?,
                    attempts = attempts + 1, last_error_code = NULL,
                    next_attempt_at = 0, updated_at = ?
                WHERE task_id = ?
                """,
                (lease_token, now + self.lease_seconds, now, row["task_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM authoring_jobs WHERE task_id = ?",
                (row["task_id"],),
            ).fetchone()
            assert claimed is not None
            return self._job(claimed)

    def release_authoring(self, task_id: str, lease_token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE authoring_jobs
                SET state = 'queued', lease_token = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE task_id = ? AND state = 'authoring' AND lease_token = ?
                """,
                (self.clock(), task_id, lease_token),
            )
            return cursor.rowcount == 1

    def requeue_after_critic(
        self,
        task_id: str,
        lease_token: str,
        feedback_json: str,
        *,
        delay_seconds: float = 0,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        feedback_json = _canonical_feedback(feedback_json)
        now = self.clock()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE authoring_jobs
                SET state = 'queued', critic_feedback_json = ?,
                    lease_token = NULL, lease_expires_at = NULL,
                    next_attempt_at = ?, updated_at = ?
                WHERE task_id = ? AND state = 'authoring' AND lease_token = ?
                """,
                (feedback_json, now + delay_seconds, now, task_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("authoring lease is not owned")

    def mark_authoring_failed(
        self,
        task_id: str,
        lease_token: str,
        error_code: str,
    ) -> bool:
        if not _PROJECT.fullmatch(error_code) or len(error_code) > 64:
            raise ValueError("error_code must be a lowercase ASCII key")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE authoring_jobs
                SET state = 'failed', last_error_code = ?, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE task_id = ? AND state = 'authoring' AND lease_token = ?
                """,
                (error_code, self.clock(), task_id, lease_token),
            )
            return cursor.rowcount == 1

    def mark_authoring_ready(self, task_id: str, lease_token: str, suite_hash: str) -> None:
        if not _HASH.fullmatch(suite_hash):
            raise ValueError("suite_hash must be lowercase SHA-256")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE authoring_jobs
                SET state = 'ready', suite_hash = ?, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE task_id = ? AND state = 'authoring' AND lease_token = ?
                """,
                (suite_hash, self.clock(), task_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("authoring lease is not owned")

    def finalize_authoring(
        self,
        task_id: str,
        lease_token: str,
        promote: Callable[[], StoredSuite],
    ) -> StoredSuite:
        """Promote vault output only while holding the current DB fencing token."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT starter_sha FROM authoring_jobs
                WHERE task_id = ? AND state = 'authoring' AND lease_token = ?
                """,
                (task_id, lease_token),
            ).fetchone()
            if row is None:
                raise RuntimeError("authoring lease is not owned")
            stored = promote()
            if (
                not isinstance(stored, StoredSuite)
                or stored.task_id != task_id
                or stored.starter_sha != row["starter_sha"]
                or not _HASH.fullmatch(stored.suite_hash)
            ):
                raise RuntimeError("vault promotion returned mismatched suite identity")
            cursor = connection.execute(
                """
                UPDATE authoring_jobs
                SET state = 'ready', suite_hash = ?, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE task_id = ? AND state = 'authoring' AND lease_token = ?
                """,
                (stored.suite_hash, self.clock(), task_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("authoring lease is not owned")
            return stored

    def request_clarification(
        self,
        task_id: str,
        lease_token: str,
        question: str,
    ) -> Clarification:
        question = _bounded_text(question, "question", 500)
        nonce = secrets.token_urlsafe(24)
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT clarification_revision FROM authoring_jobs
                WHERE task_id = ? AND state = 'authoring' AND lease_token = ?
                """,
                (task_id, lease_token),
            ).fetchone()
            if row is None:
                raise RuntimeError("authoring lease is not owned")
            revision = int(row["clarification_revision"]) + 1
            connection.execute(
                """
                INSERT INTO clarifications (
                    nonce, task_id, revision, question, status, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (nonce, task_id, revision, question, now),
            )
            connection.execute(
                """
                UPDATE authoring_jobs
                SET state = 'needs_clarification', clarification_revision = ?,
                    clarification_question = ?, clarification_answer = NULL,
                    lease_token = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE task_id = ? AND lease_token = ?
                """,
                (revision, question, now, task_id, lease_token),
            )
        return Clarification(nonce, task_id, revision, question)

    def next_pending_clarification(self) -> Clarification | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT nonce, task_id, revision, question
                FROM clarifications
                WHERE status = 'pending'
                ORDER BY created_at, nonce
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return Clarification(
            nonce=row["nonce"],
            task_id=row["task_id"],
            revision=row["revision"],
            question=row["question"],
        )

    def answer_clarification(self, nonce: str, revision: int, answer: str) -> bool:
        answer = _bounded_text(answer, "answer", 1_000)
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT task_id, status, answer FROM clarifications
                WHERE nonce = ? AND revision = ?
                """,
                (nonce, revision),
            ).fetchone()
            if row is None:
                return False
            if row["status"] == "answered":
                existing = row["answer"]
                return isinstance(existing, str) and hmac.compare_digest(
                    existing.encode("utf-8"), answer.encode("utf-8")
                )
            if row["status"] != "pending":
                return False
            cursor = connection.execute(
                """
                UPDATE clarifications
                SET status = 'answered', answer = ?, answered_at = ?
                WHERE nonce = ? AND revision = ? AND status = 'pending'
                """,
                (answer, now, nonce, revision),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                UPDATE authoring_jobs
                SET state = 'queued', clarification_answer = ?, updated_at = ?
                WHERE task_id = ? AND state = 'needs_clarification'
                  AND clarification_revision = ?
                """,
                (answer, now, row["task_id"], revision),
            )
            return True

    def enqueue_grading(
        self,
        *,
        task_id: str,
        project: str,
        branch_name: str,
        commit_sha: str,
        suite_hash: str,
    ) -> GradingAttempt:
        if not _TASK_ID.fullmatch(task_id):
            raise ValueError("invalid task_id")
        if not _PROJECT.fullmatch(project) or len(project) > 64:
            raise ValueError("invalid project")
        if not _BRANCH.fullmatch(branch_name) or len(branch_name) > 200:
            raise ValueError("invalid branch_name")
        if not _SHA.fullmatch(commit_sha):
            raise ValueError("invalid commit_sha")
        if not _HASH.fullmatch(suite_hash):
            raise ValueError("invalid suite_hash")
        identity = f"{task_id}\0{commit_sha}\0{suite_hash}".encode()
        attempt_id = hashlib.sha256(identity).hexdigest()
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM grading_attempts WHERE task_id = ? AND commit_sha = ?",
                (task_id, commit_sha),
            ).fetchone()
            if row is not None:
                existing = self._grading(row)
                if (
                    existing.project != project
                    or existing.branch_name != branch_name
                    or existing.suite_hash != suite_hash
                    or existing.attempt_id != attempt_id
                ):
                    raise ValueError("grading input changed after enqueue")
                return existing
            connection.execute(
                """
                INSERT INTO grading_attempts (
                    attempt_id, task_id, project, branch_name, commit_sha,
                    suite_hash, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    attempt_id,
                    task_id,
                    project,
                    branch_name,
                    commit_sha,
                    suite_hash,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM grading_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            assert row is not None
            return self._grading(row)

    def claim_next_grading(self) -> GradingAttempt | None:
        now = self.clock()
        token = secrets.token_hex(32)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM grading_attempts
                WHERE (state = 'queued' AND next_attempt_at <= ?)
                   OR (state = 'grading' AND lease_expires_at <= ?)
                ORDER BY created_at, attempt_id
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE grading_attempts
                SET state = 'grading', lease_token = ?, lease_expires_at = ?,
                    attempts = attempts + 1, last_error_code = NULL,
                    next_attempt_at = 0, updated_at = ?
                WHERE attempt_id = ?
                """,
                (token, now + self.lease_seconds, now, row["attempt_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM grading_attempts WHERE attempt_id = ?",
                (row["attempt_id"],),
            ).fetchone()
            assert claimed is not None
            return self._grading(claimed)

    def complete_grading(
        self,
        attempt_id: str,
        lease_token: str | None,
        *,
        passed: bool,
        passed_count: int,
        total_count: int,
        private_report_json: str,
    ) -> bool:
        if not _HASH.fullmatch(attempt_id):
            raise ValueError("invalid attempt_id")
        if not isinstance(passed, bool):
            raise ValueError("passed must be boolean")
        if (
            isinstance(passed_count, bool)
            or isinstance(total_count, bool)
            or not isinstance(passed_count, int)
            or not isinstance(total_count, int)
            or total_count <= 0
            or not 0 <= passed_count <= total_count
            or passed != (passed_count == total_count)
        ):
            raise ValueError("invalid grading counts")
        private_report_json = _canonical_private_report(private_report_json)
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE grading_attempts
                SET state = 'completed', passed = ?, passed_count = ?, total_count = ?,
                    private_report_json = ?, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE attempt_id = ? AND state = 'grading' AND lease_token = ?
                """,
                (
                    int(passed),
                    passed_count,
                    total_count,
                    private_report_json,
                    now,
                    attempt_id,
                    lease_token,
                ),
            )
            if cursor.rowcount != 1:
                return False
            attempt = connection.execute(
                "SELECT task_id, commit_sha FROM grading_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            assert attempt is not None
            connection.execute(
                """
                INSERT OR IGNORE INTO check_publications (
                    publication_id, commit_sha, passed, state, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, ?)
                """,
                (attempt_id, attempt["commit_sha"], int(passed), now, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO mentor_notifications (
                    notification_id, task_id, commit_sha, report_json,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    attempt_id,
                    attempt["task_id"],
                    attempt["commit_sha"],
                    private_report_json,
                    now,
                    now,
                ),
            )
            return True

    def release_grading(
        self,
        attempt_id: str,
        lease_token: str,
        error_code: str,
    ) -> bool:
        if not _PROJECT.fullmatch(error_code) or len(error_code) > 64:
            raise ValueError("error_code must be a lowercase ASCII key")
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT attempts FROM grading_attempts
                WHERE attempt_id = ? AND state = 'grading' AND lease_token = ?
                """,
                (attempt_id, lease_token),
            ).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"])
            if attempts >= self.max_grading_attempts:
                cursor = connection.execute(
                    """
                    UPDATE grading_attempts
                    SET state = 'failed', last_error_code = ?, lease_token = NULL,
                        lease_expires_at = NULL, next_attempt_at = 0, updated_at = ?
                    WHERE attempt_id = ? AND state = 'grading' AND lease_token = ?
                    """,
                    (error_code, now, attempt_id, lease_token),
                )
                return cursor.rowcount == 1
            delay = min(
                self.grading_retry_base_seconds * (2 ** max(0, attempts - 1)),
                3600,
            )
            cursor = connection.execute(
                """
                UPDATE grading_attempts
                SET state = 'queued', last_error_code = ?, lease_token = NULL,
                    lease_expires_at = NULL, next_attempt_at = ?, updated_at = ?
                WHERE attempt_id = ? AND state = 'grading' AND lease_token = ?
                """,
                (error_code, now + delay, now, attempt_id, lease_token),
            )
            return cursor.rowcount == 1

    def claim_next_check_publication(self) -> CheckPublication | None:
        now = self.clock()
        token = secrets.token_hex(32)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM check_publications
                WHERE (state = 'queued' AND next_attempt_at <= ?)
                   OR (state = 'publishing' AND lease_expires_at <= ?)
                ORDER BY created_at, publication_id
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE check_publications
                SET state = 'publishing', lease_token = ?, lease_expires_at = ?,
                    attempts = attempts + 1, last_error_code = NULL,
                    next_attempt_at = 0, updated_at = ?
                WHERE publication_id = ?
                """,
                (token, now + self.lease_seconds, now, row["publication_id"]),
            )
            return CheckPublication(
                publication_id=row["publication_id"],
                commit_sha=row["commit_sha"],
                passed=bool(row["passed"]),
                lease_token=token,
                attempts=int(row["attempts"]) + 1,
            )

    def complete_check_publication(self, publication_id: str, lease_token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE check_publications
                SET state = 'published', lease_token = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE publication_id = ? AND state = 'publishing' AND lease_token = ?
                """,
                (self.clock(), publication_id, lease_token),
            )
            return cursor.rowcount == 1

    def release_check_publication(
        self,
        publication_id: str,
        lease_token: str,
        error_code: str,
        *,
        delay_seconds: float = 30,
    ) -> bool:
        if not _PROJECT.fullmatch(error_code) or len(error_code) > 64:
            raise ValueError("error_code must be a lowercase ASCII key")
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        now = self.clock()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE check_publications
                SET state = 'queued', last_error_code = ?, lease_token = NULL,
                    lease_expires_at = NULL, next_attempt_at = ?, updated_at = ?
                WHERE publication_id = ? AND state = 'publishing' AND lease_token = ?
                """,
                (
                    error_code,
                    now + delay_seconds,
                    now,
                    publication_id,
                    lease_token,
                ),
            )
            return cursor.rowcount == 1

    def claim_next_mentor_notification(self) -> MentorNotification | None:
        now = self.clock()
        token = secrets.token_hex(32)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM mentor_notifications
                WHERE state = 'queued'
                   OR (state = 'delivering' AND lease_expires_at <= ?)
                ORDER BY created_at, notification_id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE mentor_notifications
                SET state = 'delivering', lease_token = ?, lease_expires_at = ?,
                    attempts = attempts + 1, updated_at = ?
                WHERE notification_id = ?
                """,
                (token, now + self.lease_seconds, now, row["notification_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM mentor_notifications WHERE notification_id = ?",
                (row["notification_id"],),
            ).fetchone()
            assert claimed is not None
            return MentorNotification(
                notification_id=claimed["notification_id"],
                task_id=claimed["task_id"],
                commit_sha=claimed["commit_sha"],
                report_json=claimed["report_json"],
                lease_token=claimed["lease_token"],
                attempts=claimed["attempts"],
            )

    def mark_mentor_notification_sent(
        self,
        notification_id: str,
        lease_token: str,
    ) -> bool:
        if not _HASH.fullmatch(notification_id) or not _HASH.fullmatch(lease_token):
            return False
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE mentor_notifications
                SET state = 'sent', lease_token = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE notification_id = ? AND state = 'delivering' AND lease_token = ?
                """,
                (self.clock(), notification_id, lease_token),
            )
            return cursor.rowcount == 1

    def release_mentor_notification(
        self,
        notification_id: str,
        lease_token: str,
    ) -> bool:
        if not _HASH.fullmatch(notification_id) or not _HASH.fullmatch(lease_token):
            return False
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE mentor_notifications
                SET state = 'queued', lease_token = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE notification_id = ? AND state = 'delivering' AND lease_token = ?
                """,
                (self.clock(), notification_id, lease_token),
            )
            return cursor.rowcount == 1

    def get_grading(self, attempt_id: str) -> GradingAttempt:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM grading_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return self._grading(row)

    def get_authoring(self, task_id: str) -> AuthoringJob:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM authoring_jobs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._job(row)

    @staticmethod
    def _grading(row: sqlite3.Row) -> GradingAttempt:
        passed_value = row["passed"]
        return GradingAttempt(
            attempt_id=row["attempt_id"],
            task_id=row["task_id"],
            project=row["project"],
            branch_name=row["branch_name"],
            commit_sha=row["commit_sha"],
            suite_hash=row["suite_hash"],
            state=row["state"],
            lease_token=row["lease_token"],
            attempts=row["attempts"],
            passed=None if passed_value is None else bool(passed_value),
            passed_count=row["passed_count"],
            total_count=row["total_count"],
            private_report_json=row["private_report_json"],
            last_error_code=row["last_error_code"],
        )

    @staticmethod
    def _job(row: sqlite3.Row) -> AuthoringJob:
        return AuthoringJob(
            task_id=row["task_id"],
            row_id=row["row_id"],
            project=row["project"],
            branch_name=row["branch_name"],
            starter_sha=row["starter_sha"],
            assignment_json=row["assignment_json"],
            state=row["state"],
            lease_token=row["lease_token"],
            attempts=row["attempts"],
            suite_hash=row["suite_hash"],
            clarification_revision=row["clarification_revision"],
            clarification_question=row["clarification_question"],
            clarification_answer=row["clarification_answer"],
            critic_feedback_json=row["critic_feedback_json"],
            last_error_code=row["last_error_code"],
        )


class SuiteVault:
    def __init__(self, root: str | Path, *, read_only: bool = False) -> None:
        if not isinstance(read_only, bool):
            raise ValueError("read_only must be boolean")
        self.root = Path(root)
        self.read_only = read_only
        self._ensure_directory(self.root, create=not read_only)

    def freeze(
        self,
        *,
        task_id: str,
        starter_sha: str,
        suite_payload: dict[str, Any],
        author_model: str,
    ) -> StoredSuite:
        if self.read_only:
            raise PermissionError("suite vault is read-only")
        if not _TASK_ID.fullmatch(task_id) or not _SHA.fullmatch(starter_sha):
            raise ValueError("invalid suite identity")
        author_model = _bounded_text(author_model, "author_model", 100)
        suite_bytes = _canonical_json_bytes(suite_payload)
        if len(suite_bytes) > 1_000_000:
            raise ValueError("suite payload is too large")
        suite_hash = hashlib.sha256(suite_bytes).hexdigest()
        task_directory = self.root / task_id
        manifest_path = task_directory / "manifest.json"
        suite_path = task_directory / "suite.json"
        if task_directory.exists() or task_directory.is_symlink():
            stored = self.load(task_id)
            if (
                stored.starter_sha != starter_sha
                or stored.suite_hash != suite_hash
                or stored.author_model != author_model
            ):
                raise VaultConflictError("immutable hidden suite would be changed")
            return stored
        manifest = {
            "schema_version": 1,
            "task_id": task_id,
            "starter_sha": starter_sha,
            "suite_hash": suite_hash,
            "author_model": author_model,
        }
        staging = self.root / f".{task_id}.{secrets.token_hex(8)}.staging"
        try:
            staging.mkdir(mode=0o700)
            self._ensure_directory(staging, create=False)
            self._atomic_write(staging, suite_path.name, suite_bytes)
            self._atomic_write(
                staging,
                manifest_path.name,
                _canonical_json_bytes(manifest),
            )
            staging_descriptor = os.open(staging, os.O_RDONLY)
            try:
                os.fsync(staging_descriptor)
            finally:
                os.close(staging_descriptor)
            os.rename(staging, task_directory)
            root_descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(root_descriptor)
            finally:
                os.close(root_descriptor)
        finally:
            if staging.exists() and not staging.is_symlink():
                shutil.rmtree(staging)
        return StoredSuite(task_id, starter_sha, suite_hash, author_model, suite_payload)

    def load(self, task_id: str) -> StoredSuite:
        if not _TASK_ID.fullmatch(task_id):
            raise ValueError("invalid task_id")
        task_directory = self.root / task_id
        self._ensure_directory(task_directory, create=False)
        manifest = self._read_json_file(task_directory / "manifest.json")
        suite_payload = self._read_json_file(task_directory / "suite.json")
        if not isinstance(manifest, dict) or not isinstance(suite_payload, dict):
            raise VaultConflictError("hidden suite files are invalid")
        expected_keys = {
            "schema_version",
            "task_id",
            "starter_sha",
            "suite_hash",
            "author_model",
        }
        if set(manifest) != expected_keys or manifest.get("task_id") != task_id:
            raise VaultConflictError("hidden suite manifest is invalid")
        suite_hash = hashlib.sha256(_canonical_json_bytes(suite_payload)).hexdigest()
        if suite_hash != manifest.get("suite_hash"):
            raise VaultConflictError("hidden suite hash mismatch")
        if not _SHA.fullmatch(str(manifest.get("starter_sha", ""))):
            raise VaultConflictError("hidden suite starter SHA is invalid")
        if not _HASH.fullmatch(str(manifest.get("suite_hash", ""))):
            raise VaultConflictError("hidden suite hash is invalid")
        author_model = manifest.get("author_model")
        if not isinstance(author_model, str):
            raise VaultConflictError("hidden suite author model is invalid")
        return StoredSuite(
            task_id=task_id,
            starter_sha=manifest["starter_sha"],
            suite_hash=suite_hash,
            author_model=author_model,
            suite_payload=suite_payload,
        )

    def _ensure_directory(self, path: Path, *, create: bool) -> None:
        if create:
            try:
                path.mkdir(parents=True, mode=0o700, exist_ok=True)
            except FileExistsError as error:
                raise VaultConflictError("suite path is not a safe directory") from error
        try:
            metadata = path.lstat()
        except FileNotFoundError as error:
            raise VaultConflictError("suite path is not a safe directory") from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise VaultConflictError("suite path is not a safe directory")
        if not self.read_only:
            os.chmod(path, 0o700)

    @staticmethod
    def _atomic_write(directory: Path, name: str, payload: bytes) -> None:
        temporary = directory / f".{name}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, directory / name)
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _read_json_file(path: Path) -> Any:
        try:
            metadata = path.lstat()
        except FileNotFoundError as error:
            raise VaultConflictError("hidden suite is incomplete") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise VaultConflictError("hidden suite file is unsafe")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise VaultConflictError("hidden suite file is invalid") from error


def _validate_identity(
    task_id: str,
    row_id: str,
    project: str,
    branch_name: str,
    starter_sha: str,
) -> None:
    if not _TASK_ID.fullmatch(task_id):
        raise ValueError("invalid task_id")
    if not isinstance(row_id, str) or not row_id or len(row_id) > 200 or "\x00" in row_id:
        raise ValueError("invalid row_id")
    if not _PROJECT.fullmatch(project) or len(project) > 64:
        raise ValueError("invalid project")
    if not _BRANCH.fullmatch(branch_name) or len(branch_name) > 200:
        raise ValueError("invalid branch_name")
    if not _SHA.fullmatch(starter_sha):
        raise ValueError("invalid starter_sha")


def _canonical_assignment(value: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 500_000:
        raise ValueError("assignment_json must be bounded JSON")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("assignment_json must be valid JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("assignment_json must contain an object")
    return _canonical_json_bytes(parsed).decode("utf-8")


def _canonical_feedback(value: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 100_000:
        raise ValueError("critic feedback must be bounded JSON")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("critic feedback must be valid JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("critic feedback must contain an object")
    return _canonical_json_bytes(parsed).decode("utf-8")


def _canonical_private_report(value: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 100_000:
        raise ValueError("private report must be bounded JSON")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("private report must be valid JSON") from error
    if not isinstance(parsed, dict) or set(parsed) != {
        "passed",
        "total",
        "failed_rubrics",
    }:
        raise ValueError("private report fields are invalid")
    passed_count = parsed["passed"]
    total_count = parsed["total"]
    failed = parsed["failed_rubrics"]
    if (
        isinstance(passed_count, bool)
        or isinstance(total_count, bool)
        or not isinstance(passed_count, int)
        or not isinstance(total_count, int)
        or total_count <= 0
        or not 0 <= passed_count <= total_count
    ):
        raise ValueError("private report counts are invalid")
    if not isinstance(failed, list) or len(failed) > 20:
        raise ValueError("private report rubrics are invalid")
    seen: set[str] = set()
    for item in failed:
        if not isinstance(item, dict) or set(item) != {"id", "description"}:
            raise ValueError("private report rubric fields are invalid")
        rubric_id = item["id"]
        description = item["description"]
        if (
            not isinstance(rubric_id, str)
            or len(rubric_id) > 64
            or not _PROJECT.fullmatch(rubric_id)
            or rubric_id in seen
            or not isinstance(description, str)
            or not description.strip()
            or len(description) > 500
            or "\x00" in description
        ):
            raise ValueError("private report rubric is invalid")
        seen.add(rubric_id)
    return _canonical_json_bytes(parsed).decode("utf-8")


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ValueError("value must be canonical JSON") from error


def _bounded_text(value: str, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise ValueError(f"{label} must be bounded text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be valid Unicode") from error
    return value.strip()
