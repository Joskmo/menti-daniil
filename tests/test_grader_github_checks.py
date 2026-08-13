import json

import pytest

from grader.github_checks import GitHubHiddenGradePublisher, GitHubHiddenGradeStatusPublisher


class FakeTransport:
    def __init__(self, listing) -> None:
        self.listing = listing
        self.calls = []

    def request(self, method, path, *, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET":
            return self.listing
        return {"id": 123}


def test_hidden_grade_publisher_rejects_unsafe_repository_identity() -> None:
    transport = FakeTransport({"check_runs": []})

    with pytest.raises(ValueError, match="identity"):
        GitHubHiddenGradePublisher(transport, "Joskmo/other", "menti-daniil")


def test_hidden_grade_publisher_creates_coarse_check_without_private_details() -> None:
    transport = FakeTransport({"check_runs": []})
    publisher = GitHubHiddenGradePublisher(transport, "Joskmo", "menti-daniil")

    publisher.publish_result(
        attempt_id="a" * 64,
        commit_sha="b" * 40,
        passed=False,
    )

    assert transport.calls[1] == (
        "POST",
        "/repos/Joskmo/menti-daniil/check-runs",
        {
            "name": "hidden-grade",
            "head_sha": "b" * 40,
            "status": "completed",
            "conclusion": "failure",
            "external_id": "a" * 64,
            "output": {
                "title": "Скрытая проверка не пройдена",
                "summary": "Исправьте решение и отправьте новый commit.",
            },
        },
    )
    serialized = json.dumps(transport.calls, ensure_ascii=False)
    assert "case" not in serialized
    assert "expected" not in serialized
    assert "private" not in serialized
    assert "7 из 10" not in serialized
    assert "passed_count" not in serialized
    assert "total_count" not in serialized


def test_hidden_grade_publisher_updates_same_attempt_idempotently() -> None:
    transport = FakeTransport(
        {"check_runs": [{"id": 321, "external_id": "a" * 64}]}
    )
    publisher = GitHubHiddenGradePublisher(transport, "Joskmo", "menti-daniil")

    publisher.publish_result(
        attempt_id="a" * 64,
        commit_sha="b" * 40,
        passed=True,
    )

    assert transport.calls[1][0:2] == (
        "PATCH",
        "/repos/Joskmo/menti-daniil/check-runs/321",
    )
    assert transport.calls[1][2]["conclusion"] == "success"
    assert "head_sha" not in transport.calls[1][2]
    assert "external_id" not in transport.calls[1][2]


def test_hidden_grade_publisher_fails_closed_on_duplicate_attempt() -> None:
    transport = FakeTransport(
        {
            "check_runs": [
                {"id": 1, "external_id": "a" * 64},
                {"id": 2, "external_id": "a" * 64},
            ]
        }
    )
    publisher = GitHubHiddenGradePublisher(transport, "Joskmo", "menti-daniil")

    with pytest.raises(RuntimeError, match="duplicate"):
        publisher.publish_result(
            attempt_id="a" * 64,
            commit_sha="b" * 40,
            passed=False,
        )


def test_hidden_grade_status_fallback_publishes_same_coarse_context() -> None:
    transport = FakeTransport({})
    publisher = GitHubHiddenGradeStatusPublisher(transport, "Joskmo", "menti-daniil")

    publisher.publish_result(
        attempt_id="a" * 64,
        commit_sha="b" * 40,
        passed=True,
    )

    assert transport.calls == [
        (
            "POST",
            f"/repos/Joskmo/menti-daniil/statuses/{'b' * 40}",
            {
                "state": "success",
                "context": "hidden-grade",
                "description": "Скрытая проверка пройдена",
            },
        )
    ]
