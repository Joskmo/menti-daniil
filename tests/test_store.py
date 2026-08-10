import sqlite3

import pytest

from bridge.store import SQLiteStore


def test_processing_delivery_is_reclaimed_only_after_lease_expires(tmp_path) -> None:
    now = [100.0]
    store = SQLiteStore(
        tmp_path / "bridge.db",
        delivery_lease_seconds=10,
        clock=lambda: now[0],
    )

    assert store.claim_delivery("delivery-1")
    assert not store.claim_delivery("delivery-1")

    now[0] = 109.0
    assert not store.claim_delivery("delivery-1")

    now[0] = 111.0
    reclaimed = store.claim_delivery("delivery-1")
    assert reclaimed.state == "claimed"
    assert reclaimed.token is not None

    store.complete_delivery("delivery-1", reclaimed.token)
    assert not store.claim_delivery("delivery-1")


def test_released_delivery_can_be_claimed_immediately(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bridge.db")

    claim = store.claim_delivery("delivery-1")
    assert claim.token is not None
    store.release_delivery("delivery-1", claim.token)

    assert store.claim_delivery("delivery-1")


def test_completed_delivery_retention_is_bounded(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bridge.db", completed_delivery_retention=2)
    for delivery_id in ("delivery-1", "delivery-2", "delivery-3"):
        claim = store.claim_delivery(delivery_id)
        assert claim.token is not None
        store.complete_delivery(delivery_id, claim.token)

    assert store.claim_delivery("delivery-1")
    assert not store.claim_delivery("delivery-2")
    assert not store.claim_delivery("delivery-3")


def test_deployed_schema_is_migrated_additively(tmp_path) -> None:
    database = tmp_path / "bridge.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE task_mappings (
                row_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE,
                branch_name TEXT UNIQUE,
                branch_url TEXT,
                pr_url TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE webhook_deliveries (
                delivery_id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO task_mappings (row_id, task_id, branch_name) VALUES (?, ?, ?)",
            ("row-1", "PY-001", "task/PY-001-existing"),
        )
        connection.execute(
            "INSERT INTO webhook_deliveries (delivery_id) VALUES (?)",
            ("legacy-completed",),
        )

    store = SQLiteStore(database)

    mapping = store.find_by_branch("task/PY-001-existing")
    assert mapping is not None
    assert mapping.pr_number is None
    assert mapping.pr_state is None
    assert mapping.pr_updated_at is None
    with sqlite3.connect(database) as connection:
        delivery_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(webhook_deliveries)")
        }
    assert {"event", "payload", "lease_token"} <= delivery_columns
    assert not store.claim_delivery("legacy-completed")


def test_retry_worker_reclaims_expired_payload_with_new_fence(tmp_path) -> None:
    now = [100.0]
    store = SQLiteStore(
        tmp_path / "bridge.db",
        delivery_lease_seconds=10,
        clock=lambda: now[0],
    )
    first = store.claim_delivery("delivery-retry", "pull_request", b'{"number":7}')
    assert first.token is not None

    now[0] = 111.0
    work = store.claim_next_delivery()
    assert work is not None
    assert work.delivery_id == "delivery-retry"
    assert work.event == "pull_request"
    assert work.payload == b'{"number":7}'
    assert work.token != first.token

    assert not store.release_delivery("delivery-retry", first.token)
    store.complete_delivery("delivery-retry", work.token)
    assert store.claim_delivery("delivery-retry").state == "completed"


def test_stale_worker_cannot_release_reclaimed_delivery(tmp_path) -> None:
    now = [100.0]
    store = SQLiteStore(
        tmp_path / "bridge.db",
        delivery_lease_seconds=10,
        clock=lambda: now[0],
    )
    first = store.claim_delivery("delivery-fenced")
    assert first.state == "claimed"
    assert first.token is not None

    now[0] = 111.0
    second = store.claim_delivery("delivery-fenced")
    assert second.state == "claimed"
    assert second.token is not None
    assert second.token != first.token

    assert not store.release_delivery("delivery-fenced", first.token)
    busy = store.claim_delivery("delivery-fenced")
    assert busy.state == "busy"

    store.complete_delivery("delivery-fenced", second.token)
    assert store.claim_delivery("delivery-fenced").state == "completed"


def test_stale_worker_cannot_complete_reclaimed_delivery(tmp_path) -> None:
    now = [100.0]
    store = SQLiteStore(
        tmp_path / "bridge.db",
        delivery_lease_seconds=10,
        clock=lambda: now[0],
    )
    first = store.claim_delivery("delivery-fenced")
    now[0] = 111.0
    second = store.claim_delivery("delivery-fenced")
    assert first.token is not None
    assert second.token is not None

    with pytest.raises(RuntimeError, match="lease is not owned"):
        store.complete_delivery("delivery-fenced", first.token)

    store.complete_delivery("delivery-fenced", second.token)
    assert store.claim_delivery("delivery-fenced").state == "completed"
