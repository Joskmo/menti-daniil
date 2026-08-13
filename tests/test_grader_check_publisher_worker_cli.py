from grader.check_publisher_worker_cli import build_from_environment, run_cycle
from grader.github_app import StaticGitHubTokenProvider
from grader.github_checks import GitHubHiddenGradeStatusPublisher


class FakeCoordinator:
    def __init__(self, result: str) -> None:
        self.result = result
        self.calls = 0

    def process_once(self) -> str:
        self.calls += 1
        return self.result


def test_check_publisher_worker_cycle_processes_exactly_one_publication() -> None:
    coordinator = FakeCoordinator("published")

    assert run_cycle(coordinator) == "published"
    assert coordinator.calls == 1


def test_check_publisher_uses_existing_token_without_github_app(monkeypatch, tmp_path) -> None:
    database = tmp_path / "grader.db"
    monkeypatch.setenv("GRADER_DATABASE_PATH", str(database))
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "Joskmo")
    monkeypatch.setenv("GITHUB_REPOSITORY_NAME", "menti-daniil")
    monkeypatch.setenv("GITHUB_TOKEN", "gho_synthetic_existing_oauth_token")
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)

    coordinator = build_from_environment()

    assert isinstance(coordinator.publisher.transport.tokens, StaticGitHubTokenProvider)
    assert isinstance(coordinator.publisher, GitHubHiddenGradeStatusPublisher)