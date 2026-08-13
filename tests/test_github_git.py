import subprocess
from pathlib import Path

import pytest

from bridge.github_git import GitBranchClient


class FakeRunner:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def run(self, command: list[str], env: dict[str, str]) -> str:
        self.calls.append(command)
        return self.outputs.pop(0)


def test_existing_branch_must_match_reserved_starter_sha(tmp_path: Path) -> None:
    branch = "task/PY-001-pustoy-json"
    runner = FakeRunner([f"{'a' * 40}\trefs/heads/{branch}\n"])
    client = GitBranchClient(
        repository_ssh="git@github.com:Joskmo/menti-daniil.git",
        repository_web="https://github.com/Joskmo/menti-daniil",
        base_branch="main",
        git_dir=tmp_path / "repo.git",
        ssh_key=tmp_path / "key",
        known_hosts=tmp_path / "known_hosts",
        runner=runner,
    )

    assert client.ensure_prepared_branch(branch, "a" * 40) == (
        f"https://github.com/Joskmo/menti-daniil/tree/{branch}"
    )
    assert runner.calls == [
        [
            "git",
            "ls-remote",
            "--heads",
            "git@github.com:Joskmo/menti-daniil.git",
            f"refs/heads/{branch}",
        ]
    ]


def test_existing_mismatched_branch_is_rejected_without_push(tmp_path: Path) -> None:
    runner = FakeRunner(
        [f"{'b' * 40}\trefs/heads/task/PY-001-pustoy-json\n"]
    )
    client = GitBranchClient(
        repository_ssh="git@github.com:Joskmo/menti-daniil.git",
        repository_web="https://github.com/Joskmo/menti-daniil",
        base_branch="main",
        git_dir=tmp_path / "repo.git",
        ssh_key=tmp_path / "key",
        known_hosts=tmp_path / "known_hosts",
        runner=runner,
    )

    with pytest.raises(RuntimeError, match="reserved starter"):
        client.ensure_prepared_branch("task/PY-001-pustoy-json", "a" * 40)

    assert len(runner.calls) == 1
    assert runner.calls[0][-1] == "refs/heads/task/PY-001-pustoy-json"


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _create_remote_with_main(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    work = tmp_path / "seed"
    _git("init", "--bare", str(remote))
    _git("init", "--initial-branch=main", str(work))
    _git("config", "user.name", "Test", cwd=work)
    _git("config", "user.email", "test@example.com", cwd=work)
    (work / "README.md").write_text("# Mentoring\n")
    (work / "projects").mkdir()
    (work / "projects" / "README.md").write_text("# Projects\n")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "initial", cwd=work)
    _git("remote", "add", "origin", str(remote), cwd=work)
    _git("push", "origin", "main", cwd=work)
    return remote


def test_new_branch_contains_project_scaffold_when_directory_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    remote = _create_remote_with_main(tmp_path)
    monkeypatch.setenv("GIT_DIR", "/untrusted/inherited/repository.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/untrusted/inherited/worktree")
    client = GitBranchClient(
        repository_ssh=str(remote),
        repository_web="https://github.com/Joskmo/menti-daniil",
        base_branch="main",
        git_dir=tmp_path / "cache.git",
        ssh_key=tmp_path / "key",
        known_hosts=tmp_path / "known_hosts",
    )
    branch = "task/PY-003-konverter-valyut"

    client.ensure_branch(branch, "json")
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")

    readme = _git(
        "--git-dir",
        str(remote),
        "show",
        f"refs/heads/{branch}:projects/json/README.md",
    )
    main_py = _git(
        "--git-dir",
        str(remote),
        "show",
        f"refs/heads/{branch}:projects/json/main.py",
    )
    branch_parent = _git(
        "--git-dir", str(remote), "rev-parse", f"refs/heads/{branch}^"
    )
    current_main = _git("--git-dir", str(remote), "rev-parse", "refs/heads/main")
    assert branch_parent == current_main
    assert readme == (
        "# json\n\n"
        "Учебный проект из Yonote.\n\n"
        "Добавь исходный код проекта и тесты."
    )
    assert main_py == "'Учебный проект json.'"


def test_existing_project_is_not_overwritten(tmp_path: Path) -> None:
    remote = _create_remote_with_main(tmp_path)
    seed = tmp_path / "seed"
    project = seed / "projects" / "json"
    project.mkdir()
    (project / "main.py").write_text("VALUE = 42\n")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "add existing project", cwd=seed)
    _git("push", "origin", "main", cwd=seed)
    main_commit = _git("--git-dir", str(remote), "rev-parse", "refs/heads/main")
    client = GitBranchClient(
        repository_ssh=str(remote),
        repository_web="https://github.com/Joskmo/menti-daniil",
        base_branch="main",
        git_dir=tmp_path / "cache.git",
        ssh_key=tmp_path / "key",
        known_hosts=tmp_path / "known_hosts",
    )
    branch = "task/PY-004-existing-project"

    client.ensure_branch(branch, "json")

    branch_commit = _git(
        "--git-dir", str(remote), "rev-parse", f"refs/heads/{branch}"
    )
    existing_code = _git(
        "--git-dir",
        str(remote),
        "show",
        f"refs/heads/{branch}:projects/json/main.py",
    )
    assert branch_commit == main_commit
    assert existing_code == "VALUE = 42"


def test_multiple_tasks_share_one_project_directory(tmp_path: Path) -> None:
    remote = _create_remote_with_main(tmp_path)
    client = GitBranchClient(
        repository_ssh=str(remote),
        repository_web="https://github.com/Joskmo/menti-daniil",
        base_branch="main",
        git_dir=tmp_path / "cache.git",
        ssh_key=tmp_path / "key",
        known_hosts=tmp_path / "known_hosts",
    )
    first_branch = "task/PY-010-initialize-json"
    second_branch = "task/PY-011-fix-json-errors"
    client.ensure_branch(first_branch, "json")
    client.ensure_branch(second_branch, "json")

    first_project_tree = _git(
        "--git-dir",
        str(remote),
        "rev-parse",
        f"refs/heads/{first_branch}:projects/json",
    )
    second_project_tree = _git(
        "--git-dir",
        str(remote),
        "rev-parse",
        f"refs/heads/{second_branch}:projects/json",
    )
    assert first_project_tree == second_project_tree

    seed = tmp_path / "seed"
    _git("fetch", "origin", first_branch, second_branch, cwd=seed)
    _git("merge", "--ff-only", f"origin/{first_branch}", cwd=seed)
    _git("merge", "--no-edit", f"origin/{second_branch}", cwd=seed)

    paths = _git(
        "--git-dir",
        str(remote),
        "ls-tree",
        "-r",
        "--name-only",
        f"refs/heads/{second_branch}",
    ).splitlines()
    assert "projects/json/README.md" in paths
    assert not any(path.startswith("projects/PY-") for path in paths)


@pytest.mark.parametrize("project", ["../json", "a" * 65])
def test_unsafe_project_directory_is_rejected_before_git_calls(
    tmp_path: Path,
    project: str,
) -> None:
    runner = FakeRunner([])
    client = GitBranchClient(
        repository_ssh="git@github.com:Joskmo/menti-daniil.git",
        repository_web="https://github.com/Joskmo/menti-daniil",
        base_branch="main",
        git_dir=tmp_path / "repo.git",
        ssh_key=tmp_path / "key",
        known_hosts=tmp_path / "known_hosts",
        runner=runner,
    )

    with pytest.raises(ValueError, match="Unsafe project directory"):
        client.ensure_branch("task/PY-012-unsafe-project", project)

    assert runner.calls == []


def test_project_path_file_collision_fails_without_creating_branch(tmp_path: Path) -> None:
    remote = _create_remote_with_main(tmp_path)
    seed = tmp_path / "seed"
    collision = seed / "projects" / "json"
    collision.write_text("not a directory\n")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "add path collision", cwd=seed)
    _git("push", "origin", "main", cwd=seed)
    client = GitBranchClient(
        repository_ssh=str(remote),
        repository_web="https://github.com/Joskmo/menti-daniil",
        base_branch="main",
        git_dir=tmp_path / "cache.git",
        ssh_key=tmp_path / "key",
        known_hosts=tmp_path / "known_hosts",
    )
    branch = "task/PY-005-file-collision"

    with pytest.raises(RuntimeError, match="safe directory"):
        client.ensure_branch(branch, "json")

    assert _git("ls-remote", "--heads", str(remote), f"refs/heads/{branch}") == ""
