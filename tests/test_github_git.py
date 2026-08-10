from pathlib import Path

from bridge.github_git import GitBranchClient


class FakeRunner:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def run(self, command: list[str], env: dict[str, str]) -> str:
        self.calls.append(command)
        return self.outputs.pop(0)


def test_existing_branch_is_returned_without_push(tmp_path: Path) -> None:
    runner = FakeRunner(["abc123\trefs/heads/task/PY-001-pustoy-json\n"])
    client = GitBranchClient(
        repository_ssh="git@github.com:Joskmo/menti-daniil.git",
        repository_web="https://github.com/Joskmo/menti-daniil",
        base_branch="main",
        git_dir=tmp_path / "repo.git",
        ssh_key=tmp_path / "key",
        known_hosts=tmp_path / "known_hosts",
        runner=runner,
    )

    url = client.ensure_branch("task/PY-001-pustoy-json")

    assert url == (
        "https://github.com/Joskmo/menti-daniil/tree/task/PY-001-pustoy-json"
    )
    assert len(runner.calls) == 1
    assert runner.calls[0][-1] == "refs/heads/task/PY-001-pustoy-json"


def test_missing_branch_is_created_from_current_main(tmp_path: Path) -> None:
    runner = FakeRunner(["", "", "", ""])
    client = GitBranchClient(
        repository_ssh="git@github.com:Joskmo/menti-daniil.git",
        repository_web="https://github.com/Joskmo/menti-daniil",
        base_branch="main",
        git_dir=tmp_path / "repo.git",
        ssh_key=tmp_path / "key",
        known_hosts=tmp_path / "known_hosts",
        runner=runner,
    )

    client.ensure_branch("task/PY-002-tip-dannyh-input")

    assert runner.calls[1][:2] == ["git", "init"]
    assert runner.calls[2][-1] == "refs/heads/main:refs/remotes/origin/main"
    assert runner.calls[3][-1] == (
        "refs/remotes/origin/main:refs/heads/task/PY-002-tip-dannyh-input"
    )
