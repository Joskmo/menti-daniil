import hashlib
import hmac
import io
import json
import sqlite3
from collections.abc import Callable

import pytest

from bridge.github_webhook import GitHubApiPullRequestLookup, GitHubWebhookHandler
from bridge.store import SQLiteStore

REPOSITORY = "Joskmo/menti-daniil"
BRANCH = "task/PY-001-pustoy-json"


class FakeYonote:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, str]]] = []
        self.on_update: Callable[[], None] | None = None

    def update_fields(self, row_id: str, fields: dict[str, str]) -> None:
        if self.on_update is not None:
            self.on_update()
        self.updates.append((row_id, fields))


def signed(secret: str, payload: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


class FakePullRequestLookup:
    def __init__(self, state: str, updated_at: str) -> None:
        self.state = state
        self.updated_at = updated_at
        self.calls: list[tuple[str, int, str, str]] = []

    def resolve(
        self, repository: str, number: int, branch: str, base_branch: str
    ) -> tuple[str, str]:
        self.calls.append((repository, number, branch, base_branch))
        return self.state, self.updated_at


def make_handler(
    tmp_path, pull_request_lookup: FakePullRequestLookup | None = None
) -> tuple[SQLiteStore, FakeYonote, GitHubWebhookHandler]:
    store = SQLiteStore(tmp_path / "bridge.db")
    store.get_or_allocate("row-1", "PY-001")
    store.record_branch(
        "row-1",
        BRANCH,
        f"https://github.com/{REPOSITORY}/tree/{BRANCH}",
    )
    yonote = FakeYonote()
    handler = GitHubWebhookHandler(
        secret="secret",
        repository=REPOSITORY,
        base_branch="main",
        review_status_id="review",
        done_status_id="done",
        in_progress_status_id="in-progress",
        store=store,
        yonote=yonote,
        pull_request_lookup=pull_request_lookup,
    )
    return store, yonote, handler


def pull_request_payload(
    *,
    action: str = "opened",
    number: int = 7,
    repository: str = REPOSITORY,
    head_repository: str = REPOSITORY,
    base_repository: str = REPOSITORY,
    base_ref: str = "main",
    draft: bool = False,
    merged: bool = False,
    updated_at: str = "2026-08-10T10:00:00Z",
    html_url: str = "https://github.com/Joskmo/menti-daniil/pull/7",
) -> bytes:
    return json.dumps(
        {
            "action": action,
            "number": number,
            "repository": {"full_name": repository},
            "pull_request": {
                "html_url": html_url,
                "draft": draft,
                "merged": merged,
                "updated_at": updated_at,
                "head": {
                    "ref": BRANCH,
                    "repo": {"full_name": head_repository},
                },
                "base": {
                    "ref": base_ref,
                    "repo": {"full_name": base_repository},
                },
            },
        }
    ).encode()


def deliver(handler: GitHubWebhookHandler, delivery_id: str, payload: bytes) -> bool:
    return handler.handle("pull_request", delivery_id, signed("secret", payload), payload)


def delivery_status(store: SQLiteStore, delivery_id: str) -> str | None:
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
    return row[0] if row else None


def test_github_api_lookup_returns_authoritative_review_state(monkeypatch) -> None:
    response = {
        "state": "open",
        "draft": False,
        "merged_at": None,
        "updated_at": "2026-08-10T11:00:00Z",
        "head": {"ref": BRANCH, "repo": {"full_name": REPOSITORY}},
        "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
    }

    def fake_urlopen(request, timeout):
        assert request.full_url == f"https://api.github.com/repos/{REPOSITORY}/pulls/7"
        assert timeout == 10
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr("bridge.github_webhook.urllib.request.urlopen", fake_urlopen)

    assert GitHubApiPullRequestLookup().resolve(REPOSITORY, 7, BRANCH, "main") == (
        "review",
        "2026-08-10T11:00:00Z",
    )


@pytest.mark.parametrize(
    "signature",
    ["", "not-a-signature", "sha256=deadbeef", "sha1=deadbeef"],
)
def test_invalid_hmac_is_rejected_before_delivery_claim(tmp_path, signature: str) -> None:
    store, yonote, handler = make_handler(tmp_path)
    payload = pull_request_payload()

    with pytest.raises(PermissionError, match="Invalid GitHub webhook signature"):
        handler.handle("pull_request", "delivery-invalid-hmac", signature, payload)

    assert delivery_status(store, "delivery-invalid-hmac") is None
    assert yonote.updates == []


def test_opened_pull_request_updates_yonote_once(tmp_path) -> None:
    _, yonote, handler = make_handler(tmp_path)
    payload = pull_request_payload()

    assert deliver(handler, "delivery-1", payload)
    assert not deliver(handler, "delivery-1", payload)
    assert yonote.updates == [
        (
            "row-1",
            {"pr_url": "https://github.com/Joskmo/menti-daniil/pull/7", "status_id": "review"},
        )
    ]


def test_delivery_is_completed_after_yonote_and_sqlite_side_effects(
    tmp_path, monkeypatch
) -> None:
    store, yonote, handler = make_handler(tmp_path)
    payload = pull_request_payload()
    statuses_during_side_effects: list[str | None] = []
    original_record = store.record_pr_event

    def observe_yonote_update() -> None:
        statuses_during_side_effects.append(delivery_status(store, "delivery-order"))

    def observe_record(*args):
        result = original_record(*args)
        statuses_during_side_effects.append(delivery_status(store, "delivery-order"))
        return result

    yonote.on_update = observe_yonote_update
    monkeypatch.setattr(store, "record_pr_event", observe_record)

    assert deliver(handler, "delivery-order", payload)

    assert statuses_during_side_effects == ["processing", "processing"]
    assert delivery_status(store, "delivery-order") == "completed"


def test_failed_delivery_is_persisted_and_retried_without_github_redelivery(tmp_path) -> None:
    store, yonote, handler = make_handler(tmp_path)
    payload = pull_request_payload()

    def fail_update() -> None:
        raise RuntimeError("Yonote unavailable")

    yonote.on_update = fail_update
    with pytest.raises(RuntimeError, match="Yonote unavailable"):
        deliver(handler, "delivery-retry", payload)

    assert delivery_status(store, "delivery-retry") == "pending"
    with sqlite3.connect(store.path) as connection:
        persisted = connection.execute(
            "SELECT event, payload FROM webhook_deliveries WHERE delivery_id = ?",
            ("delivery-retry",),
        ).fetchone()
    assert persisted == ("pull_request", payload)

    yonote.on_update = None
    assert handler.retry_pending_once()
    assert delivery_status(store, "delivery-retry") == "completed"
    assert yonote.updates == [
        (
            "row-1",
            {"pr_url": "https://github.com/Joskmo/menti-daniil/pull/7", "status_id": "review"},
        )
    ]


def test_merged_pull_request_marks_task_done(tmp_path) -> None:
    _, yonote, handler = make_handler(tmp_path)
    payload = pull_request_payload(action="closed", merged=True)

    assert deliver(handler, "delivery-2", payload)
    assert yonote.updates[-1] == (
        "row-1",
        {"pr_url": "https://github.com/Joskmo/menti-daniil/pull/7", "status_id": "done"},
    )


@pytest.mark.parametrize(
    ("action", "draft", "expected_status"),
    [
        ("opened", True, "in-progress"),
        ("reopened", True, "in-progress"),
        ("opened", False, "review"),
        ("reopened", False, "review"),
        ("ready_for_review", False, "review"),
        ("converted_to_draft", True, "in-progress"),
    ],
)
def test_explicit_pull_request_actions_set_expected_status(
    tmp_path, action: str, draft: bool, expected_status: str
) -> None:
    _, yonote, handler = make_handler(tmp_path)
    payload = pull_request_payload(action=action, draft=draft)

    assert deliver(handler, f"delivery-{action}-{draft}", payload)
    assert yonote.updates == [
        (
            "row-1",
            {
                "pr_url": "https://github.com/Joskmo/menti-daniil/pull/7",
                "status_id": expected_status,
            },
        )
    ]


def test_irrelevant_pull_request_action_is_ignored(tmp_path) -> None:
    _, yonote, handler = make_handler(tmp_path)
    payload = pull_request_payload(action="synchronize")

    assert deliver(handler, "delivery-synchronize", payload)
    assert yonote.updates == []


def test_stale_pull_request_event_is_ignored_and_state_is_persisted(tmp_path) -> None:
    store, yonote, handler = make_handler(tmp_path)
    current = pull_request_payload(action="ready_for_review", updated_at="2026-08-10T11:00:00Z")
    stale = pull_request_payload(
        action="converted_to_draft",
        draft=True,
        updated_at="2026-08-10T10:00:00Z",
    )

    assert deliver(handler, "delivery-current", current)
    assert deliver(handler, "delivery-stale", stale)

    mapping = store.find_by_branch(BRANCH)
    assert mapping is not None
    assert mapping.pr_number == 7
    assert mapping.pr_state == "review"
    assert mapping.pr_updated_at == "2026-08-10T11:00:00Z"
    assert yonote.updates == [
        (
            "row-1",
            {"pr_url": "https://github.com/Joskmo/menti-daniil/pull/7", "status_id": "review"},
        )
    ]


def test_equal_timestamp_transition_is_resolved_from_current_github_state(tmp_path) -> None:
    timestamp = "2026-08-10T11:00:00Z"
    lookup = FakePullRequestLookup("review", timestamp)
    store, yonote, handler = make_handler(tmp_path, lookup)
    opened_draft = pull_request_payload(
        action="opened",
        draft=True,
        updated_at=timestamp,
    )
    ready = pull_request_payload(
        action="ready_for_review",
        draft=False,
        updated_at=timestamp,
    )

    assert deliver(handler, "delivery-draft", opened_draft)
    assert deliver(handler, "delivery-ready", ready)

    mapping = store.find_by_branch(BRANCH)
    assert mapping is not None
    assert mapping.pr_state == "review"
    assert lookup.calls == [(REPOSITORY, 7, BRANCH, "main")]
    assert yonote.updates[-1] == (
        "row-1",
        {"pr_url": "https://github.com/Joskmo/menti-daniil/pull/7", "status_id": "review"},
    )


def test_done_pull_request_state_cannot_be_downgraded(tmp_path) -> None:
    store, yonote, handler = make_handler(tmp_path)
    merged = pull_request_payload(
        action="closed",
        merged=True,
        updated_at="2026-08-10T11:00:00Z",
    )
    reopened = pull_request_payload(action="reopened", updated_at="2026-08-10T12:00:00Z")

    assert deliver(handler, "delivery-merged", merged)
    assert deliver(handler, "delivery-reopened", reopened)

    mapping = store.find_by_branch(BRANCH)
    assert mapping is not None
    assert mapping.pr_state == "done"
    assert yonote.updates == [
        (
            "row-1",
            {"pr_url": "https://github.com/Joskmo/menti-daniil/pull/7", "status_id": "done"},
        )
    ]


def test_newer_pull_request_can_replace_completed_older_pr(tmp_path) -> None:
    store, yonote, handler = make_handler(tmp_path)
    merged = pull_request_payload(
        action="closed",
        number=7,
        merged=True,
        updated_at="2026-08-10T11:00:00Z",
    )
    newer = pull_request_payload(
        action="opened",
        number=8,
        updated_at="2026-08-10T12:00:00Z",
    )

    assert deliver(handler, "delivery-pr-7-merged", merged)
    assert deliver(handler, "delivery-pr-8-opened", newer)

    mapping = store.find_by_branch(BRANCH)
    assert mapping is not None
    assert mapping.pr_number == 8
    assert mapping.pr_state == "review"
    assert yonote.updates[-1] == (
        "row-1",
        {"pr_url": "https://github.com/Joskmo/menti-daniil/pull/8", "status_id": "review"},
    )


def test_older_pull_request_cannot_replace_newer_pr_on_same_branch(tmp_path) -> None:
    store, yonote, handler = make_handler(tmp_path)
    newer_pr = pull_request_payload(number=8, updated_at="2026-08-10T11:00:00Z")
    older_pr = pull_request_payload(
        action="closed",
        number=7,
        merged=True,
        updated_at="2026-08-10T12:00:00Z",
    )

    assert deliver(handler, "delivery-pr-8", newer_pr)
    assert deliver(handler, "delivery-pr-7", older_pr)

    mapping = store.find_by_branch(BRANCH)
    assert mapping is not None
    assert mapping.pr_number == 8
    assert mapping.pr_url == "https://github.com/Joskmo/menti-daniil/pull/8"
    assert mapping.pr_state == "review"
    assert yonote.updates == [
        (
            "row-1",
            {"pr_url": "https://github.com/Joskmo/menti-daniil/pull/8", "status_id": "review"},
        )
    ]


def test_pull_request_from_wrong_repository_is_rejected(tmp_path) -> None:
    _, yonote, handler = make_handler(tmp_path)
    payload = pull_request_payload(repository="attacker/menti-daniil")

    assert not deliver(handler, "delivery-wrong-repo", payload)
    assert yonote.updates == []


def test_pull_request_from_fork_is_rejected(tmp_path) -> None:
    _, yonote, handler = make_handler(tmp_path)
    payload = pull_request_payload(head_repository="attacker/menti-daniil")

    assert not deliver(handler, "delivery-fork", payload)
    assert yonote.updates == []


def test_pull_request_with_wrong_base_repository_is_rejected(tmp_path) -> None:
    _, yonote, handler = make_handler(tmp_path)
    payload = pull_request_payload(base_repository="attacker/menti-daniil")

    assert not deliver(handler, "delivery-wrong-base-repo", payload)
    assert yonote.updates == []


def test_pull_request_to_wrong_base_branch_is_rejected(tmp_path) -> None:
    _, yonote, handler = make_handler(tmp_path)
    payload = pull_request_payload(base_ref="release")

    assert not deliver(handler, "delivery-wrong-base", payload)
    assert yonote.updates == []


def test_pull_request_url_is_built_from_configured_repository(tmp_path) -> None:
    _, yonote, handler = make_handler(tmp_path)
    payload = pull_request_payload(html_url="https://attacker.test/forged")

    assert deliver(handler, "delivery-canonical-url", payload)
    assert yonote.updates == [
        (
            "row-1",
            {"pr_url": "https://github.com/Joskmo/menti-daniil/pull/7", "status_id": "review"},
        )
    ]
