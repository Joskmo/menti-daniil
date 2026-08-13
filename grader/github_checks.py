import json
import re
from typing import Any, Protocol
from urllib.parse import quote

_CHECK_NAME = "hidden-grade"
_REPOSITORY_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_ATTEMPT_ID = re.compile(r"[0-9a-f]{64}")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


class GitHubChecksTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class GitHubHiddenGradePublisher:
    def __init__(self, transport: GitHubChecksTransport, owner: str, repository: str) -> None:
        if (
            not isinstance(owner, str)
            or not isinstance(repository, str)
            or not _REPOSITORY_PART.fullmatch(owner)
            or not _REPOSITORY_PART.fullmatch(repository)
            or ".." in owner
            or ".." in repository
        ):
            raise ValueError("GitHub repository identity is invalid")
        self.transport = transport
        self.owner = owner
        self.repository = repository

    def publish_result(
        self,
        *,
        attempt_id: str,
        commit_sha: str,
        passed: bool,
    ) -> None:
        if (
            not _ATTEMPT_ID.fullmatch(attempt_id)
            or not _COMMIT_SHA.fullmatch(commit_sha)
            or not isinstance(passed, bool)
        ):
            raise ValueError("hidden-grade result is invalid")
        existing = self._find(commit_sha, attempt_id)
        conclusion = "success" if passed else "failure"
        output = {
            "title": "Скрытая проверка пройдена" if passed else "Скрытая проверка не пройдена",
            "summary": (
                "Можно отправлять решение на ревью."
                if passed
                else "Исправьте решение и отправьте новый commit."
            ),
        }
        if existing is None:
            self.transport.request(
                "POST",
                self._base(),
                payload={
                    "name": _CHECK_NAME,
                    "head_sha": commit_sha,
                    "status": "completed",
                    "conclusion": conclusion,
                    "external_id": attempt_id,
                    "output": output,
                },
            )
        else:
            self.transport.request(
                "PATCH",
                f"{self._base()}/{existing}",
                payload={
                    "name": _CHECK_NAME,
                    "status": "completed",
                    "conclusion": conclusion,
                    "output": output,
                },
            )

    def _find(self, commit_sha: str, attempt_id: str) -> int | None:
        encoded_sha = quote(commit_sha, safe="")
        response = self.transport.request(
            "GET",
            f"/repos/{self.owner}/{self.repository}/commits/{encoded_sha}/check-runs"
            f"?check_name={_CHECK_NAME}&filter=all&per_page=100",
        )
        runs = response.get("check_runs")
        if not isinstance(runs, list) or len(runs) > 100:
            raise RuntimeError("GitHub returned an invalid check-run listing")
        matches = []
        for item in runs:
            if not isinstance(item, dict):
                raise RuntimeError("GitHub returned an invalid check run")
            if item.get("external_id") == attempt_id:
                identifier = item.get("id")
                invalid_identifier = (
                    isinstance(identifier, bool)
                    or not isinstance(identifier, int)
                    or identifier <= 0
                )
                if invalid_identifier:
                    raise RuntimeError("GitHub returned an invalid check-run identifier")
                matches.append(identifier)
        if len(matches) > 1:
            raise RuntimeError("GitHub returned duplicate hidden-grade attempts")
        return matches[0] if matches else None

    def _base(self) -> str:
        return f"/repos/{self.owner}/{self.repository}/check-runs"


class GitHubHiddenGradeStatusPublisher:
    """Fallback publisher for OAuth/PAT credentials without Checks write access."""

    def __init__(self, transport: GitHubChecksTransport, owner: str, repository: str) -> None:
        if (
            not isinstance(owner, str)
            or not isinstance(repository, str)
            or not _REPOSITORY_PART.fullmatch(owner)
            or not _REPOSITORY_PART.fullmatch(repository)
            or ".." in owner
            or ".." in repository
        ):
            raise ValueError("GitHub repository identity is invalid")
        self.transport = transport
        self.owner = owner
        self.repository = repository

    def publish_result(
        self,
        *,
        attempt_id: str,
        commit_sha: str,
        passed: bool,
    ) -> None:
        if (
            not _ATTEMPT_ID.fullmatch(attempt_id)
            or not _COMMIT_SHA.fullmatch(commit_sha)
            or not isinstance(passed, bool)
        ):
            raise ValueError("hidden-grade result is invalid")
        self.transport.request(
            "POST",
            f"/repos/{self.owner}/{self.repository}/statuses/{commit_sha}",
            payload={
                "state": "success" if passed else "failure",
                "context": _CHECK_NAME,
                "description": (
                    "Скрытая проверка пройдена"
                    if passed
                    else "Скрытая проверка не пройдена"
                ),
            },
        )


def redact_checks_payload(payload: dict[str, Any]) -> str:
    """Bounded debug representation that never serializes check output details."""
    return json.dumps(
        {
            "name": payload.get("name"),
            "status": payload.get("status"),
            "conclusion": payload.get("conclusion"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
