import hashlib
import hmac
import json
import threading
import urllib.request
from datetime import datetime
from typing import Protocol

from bridge.store import SQLiteStore


class DeliveryInProgressError(RuntimeError):
    pass


class YonoteUpdater(Protocol):
    def update_fields(self, row_id: str, fields: dict[str, str]) -> None: ...


class CommitGrader(Protocol):
    def enqueue_commit(
        self,
        task_id: str,
        project: str,
        branch_name: str,
        commit_sha: str,
    ) -> object | None: ...


class PullRequestLookup(Protocol):
    def resolve(
        self, repository: str, number: int, branch: str, base_branch: str
    ) -> tuple[str, str]: ...


class GitHubApiPullRequestLookup:
    def resolve(
        self, repository: str, number: int, branch: str, base_branch: str
    ) -> tuple[str, str]:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repository}/pulls/{number}",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "menti-github-bridge",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            body = json.load(response)
        if body.get("head", {}).get("ref") != branch:
            raise ValueError("GitHub API returned an unexpected PR head branch")
        if body.get("head", {}).get("repo", {}).get("full_name") != repository:
            raise ValueError("GitHub API returned an unexpected PR head repository")
        if body.get("base", {}).get("ref") != base_branch:
            raise ValueError("GitHub API returned an unexpected PR base branch")
        if body.get("base", {}).get("repo", {}).get("full_name") != repository:
            raise ValueError("GitHub API returned an unexpected PR base repository")
        updated_at = body.get("updated_at")
        if not isinstance(updated_at, str):
            raise ValueError("GitHub API returned a PR without updated_at")
        if body.get("merged_at"):
            return "done", updated_at
        if body.get("state") != "open" or body.get("draft"):
            return "in_progress", updated_at
        return "review", updated_at


class GitHubWebhookHandler:
    def __init__(
        self,
        secret: str,
        repository: str,
        base_branch: str,
        review_status_id: str,
        done_status_id: str,
        in_progress_status_id: str,
        store: SQLiteStore,
        yonote: YonoteUpdater,
        pull_request_lookup: PullRequestLookup | None = None,
        grader: CommitGrader | None = None,
    ) -> None:
        self.secret = secret
        self.repository = repository
        self.base_branch = base_branch
        self.review_status_id = review_status_id
        self.done_status_id = done_status_id
        self.in_progress_status_id = in_progress_status_id
        self.store = store
        self.yonote = yonote
        self.pull_request_lookup = pull_request_lookup
        self.grader = grader
        self._lifecycle_lock = threading.Lock()

    def handle(
        self,
        event: str,
        delivery_id: str,
        signature: str,
        payload: bytes,
    ) -> bool:
        self._verify_signature(signature, payload)
        claim = self.store.claim_delivery(delivery_id, event, payload)
        if claim.state == "completed":
            return False
        if claim.state == "busy":
            raise DeliveryInProgressError(f"Delivery is already processing: {delivery_id}")
        if claim.token is None:
            raise RuntimeError(f"Claimed delivery has no lease token: {delivery_id}")
        return self._process_delivery(delivery_id, event, payload, claim.token)

    def retry_pending_once(self) -> bool:
        delivery = self.store.claim_next_delivery()
        if delivery is None:
            return False
        try:
            self._handle_claimed(delivery.event, delivery.payload)
        except Exception:
            self.store.release_delivery(
                delivery.delivery_id,
                delivery.token,
                delay_seconds=30,
            )
            raise
        self.store.complete_delivery(delivery.delivery_id, delivery.token)
        return True

    def _process_delivery(
        self,
        delivery_id: str,
        event: str,
        payload: bytes,
        lease_token: str,
    ) -> bool:
        try:
            accepted = self._handle_claimed(event, payload)
        except Exception:
            self.store.release_delivery(delivery_id, lease_token)
            raise
        self.store.complete_delivery(delivery_id, lease_token)
        return accepted

    def _handle_claimed(self, event: str, payload: bytes) -> bool:
        if event == "push":
            return self._handle_push(json.loads(payload))
        if event != "pull_request":
            return True
        body = json.loads(payload)
        if body.get("repository", {}).get("full_name") != self.repository:
            return False
        pull_request = body["pull_request"]
        if pull_request.get("head", {}).get("repo", {}).get("full_name") != self.repository:
            return False
        if pull_request.get("base", {}).get("repo", {}).get("full_name") != self.repository:
            return False
        if pull_request.get("base", {}).get("ref") != self.base_branch:
            return False
        branch = pull_request["head"]["ref"]
        mapping = self.store.find_by_branch(branch)
        if mapping is None:
            return True

        action = body.get("action")
        if action in {"opened", "reopened"}:
            if pull_request.get("draft"):
                pr_state = "in_progress"
                status_id = self.in_progress_status_id
            else:
                pr_state = "review"
                status_id = self.review_status_id
        elif action == "ready_for_review":
            pr_state = "review"
            status_id = self.review_status_id
        elif action == "converted_to_draft":
            pr_state = "in_progress"
            status_id = self.in_progress_status_id
        elif action == "closed" and pull_request.get("merged"):
            pr_state = "done"
            status_id = self.done_status_id
        elif action == "closed":
            pr_state = "in_progress"
            status_id = self.in_progress_status_id
        else:
            return True

        pr_number = body.get("number")
        updated_at = pull_request.get("updated_at")
        event_time = self._parse_timestamp(updated_at)
        if not isinstance(pr_number, int) or event_time is None:
            return False
        pr_url = f"https://github.com/{self.repository}/pull/{pr_number}"
        with self._lifecycle_lock:
            current = self.store.find_by_branch(branch)
            if current is None:
                return True
            allow_equal_transition = False
            current_time = self._parse_timestamp(current.pr_updated_at)
            if (
                current.pr_number == pr_number
                and current.pr_state != pr_state
                and current_time == event_time
            ):
                if self.pull_request_lookup is None:
                    return True
                pr_state, updated_at = self.pull_request_lookup.resolve(
                    self.repository,
                    pr_number,
                    branch,
                    self.base_branch,
                )
                status_id = self._status_id(pr_state)
                event_time = self._parse_timestamp(updated_at)
                if event_time is None:
                    raise ValueError("GitHub API returned an invalid PR timestamp")
                allow_equal_transition = True
            if not self._should_apply(
                current.pr_number,
                current.pr_state,
                current.pr_updated_at,
                pr_number,
                pr_state,
                event_time,
                allow_equal_transition=allow_equal_transition,
            ):
                return True
            self.yonote.update_fields(
                current.row_id,
                {"pr_url": pr_url, "status_id": status_id},
            )
            self.store.record_pr_event(
                current.row_id,
                pr_url,
                pr_number,
                pr_state,
                updated_at,
            )
        return True

    def _handle_push(self, body: object) -> bool:
        if not isinstance(body, dict):
            return False
        repository = body.get("repository")
        if (
            not isinstance(repository, dict)
            or repository.get("full_name") != self.repository
        ):
            return False
        if body.get("deleted") is True:
            return True
        reference = body.get("ref")
        commit_sha = body.get("after")
        if not isinstance(reference, str) or not reference.startswith("refs/heads/"):
            return False
        branch = reference.removeprefix("refs/heads/")
        if (
            not isinstance(commit_sha, str)
            or len(commit_sha) != 40
            or any(character not in "0123456789abcdef" for character in commit_sha)
        ):
            return False
        mapping = self.store.find_by_branch(branch)
        if mapping is None or self.grader is None:
            return True
        if mapping.project_name is None or mapping.starter_sha is None:
            raise RuntimeError("task mapping identity is not ready for grading")
        if body.get("created") is True and commit_sha == mapping.starter_sha:
            return True
        self.grader.enqueue_commit(
            mapping.task_id,
            mapping.project_name,
            branch,
            commit_sha,
        )
        return True

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return timestamp if timestamp.tzinfo is not None else None

    @classmethod
    def _should_apply(
        cls,
        current_number: int | None,
        current_state: str | None,
        current_updated_at: str | None,
        incoming_number: int,
        incoming_state: str,
        incoming_time: datetime,
        *,
        allow_equal_transition: bool = False,
    ) -> bool:
        if current_number is None:
            return True
        if incoming_number < current_number:
            return False
        if incoming_number > current_number:
            return True
        if current_state == "done" and incoming_state != "done":
            return False
        if current_updated_at is None:
            return True
        current_time = cls._parse_timestamp(current_updated_at)
        if current_time is None:
            return False
        if incoming_time > current_time:
            return True
        return (
            allow_equal_transition
            and incoming_time == current_time
            and incoming_state != current_state
        )

    def _status_id(self, pr_state: str) -> str:
        if pr_state == "done":
            return self.done_status_id
        if pr_state == "review":
            return self.review_status_id
        if pr_state == "in_progress":
            return self.in_progress_status_id
        raise ValueError(f"Unsupported PR state: {pr_state}")

    def _verify_signature(self, signature: str, payload: bytes) -> None:
        expected = "sha256=" + hmac.new(
            self.secret.encode(), payload, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise PermissionError("Invalid GitHub webhook signature")
