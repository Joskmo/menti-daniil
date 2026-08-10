import ast
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

    url = client.ensure_branch("task/PY-001-pustoy-json", "Пустой JSON")

    assert url == (
        "https://github.com/Joskmo/menti-daniil/tree/task/PY-001-pustoy-json"
    )
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
) -> None:
    remote = _create_remote_with_main(tmp_path)
    client = GitBranchClient(
        repository_ssh=str(remote),
        repository_web="https://github.com/Joskmo/menti-daniil",
        base_branch="main",
        git_dir=tmp_path / "cache.git",
        ssh_key=tmp_path / "key",
        known_hosts=tmp_path / "known_hosts",
    )
    branch = "task/PY-003-konverter-valyut"

    client.ensure_branch(branch, "Конвертер валют")

    readme = _git(
        "--git-dir",
        str(remote),
        "show",
        f"refs/heads/{branch}:projects/PY-003-konverter-valyut/README.md",
    )
    main_py = _git(
        "--git-dir",
        str(remote),
        "show",
        f"refs/heads/{branch}:projects/PY-003-konverter-valyut/main.py",
    )
    branch_parent = _git(
        "--git-dir", str(remote), "rev-parse", f"refs/heads/{branch}^"
    )
    current_main = _git("--git-dir", str(remote), "rev-parse", "refs/heads/main")
    assert branch_parent == current_main
    assert readme == (
        "# PY-003 — Конвертер валют\n\n"
        "Учебный проект по задаче из Yonote.\n\n"
        "Начни реализацию в `main.py`."
    )
    assert main_py == "'PY-003: Конвертер валют.'"


def test_existing_project_is_not_overwritten(tmp_path: Path) -> None:
    remote = _create_remote_with_main(tmp_path)
    seed = tmp_path / "seed"
    project = seed / "projects" / "PY-004-existing-project"
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

    client.ensure_branch(branch, "Новое название не должно затереть проект")

    branch_commit = _git(
        "--git-dir", str(remote), "rev-parse", f"refs/heads/{branch}"
    )
    existing_code = _git(
        "--git-dir",
        str(remote),
        "show",
        f"refs/heads/{branch}:projects/PY-004-existing-project/main.py",
    )
    assert branch_commit == main_commit
    assert existing_code == "VALUE = 42"


def test_project_path_file_collision_fails_without_creating_branch(tmp_path: Path) -> None:
    remote = _create_remote_with_main(tmp_path)
    seed = tmp_path / "seed"
    collision = seed / "projects" / "PY-005-file-collision"
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
        client.ensure_branch(branch, "Коллизия пути")

    assert _git("ls-remote", "--heads", str(remote), f"refs/heads/{branch}") == ""


def test_invalid_unicode_in_title_is_replaced_in_scaffold(tmp_path: Path) -> None:
    remote = _create_remote_with_main(tmp_path)
    client = GitBranchClient(
        repository_ssh=str(remote),
        repository_web="https://github.com/Joskmo/menti-daniil",
        base_branch="main",
        git_dir=tmp_path / "cache.git",
        ssh_key=tmp_path / "key",
        known_hosts=tmp_path / "known_hosts",
    )
    branch = "task/PY-006-invalid-unicode"

    client.ensure_branch(branch, "Некорректный \ud800 заголовок")

    readme = _git(
        "--git-dir",
        str(remote),
        "show",
        f"refs/heads/{branch}:projects/PY-006-invalid-unicode/README.md",
    )
    main_py = _git(
        "--git-dir",
        str(remote),
        "show",
        f"refs/heads/{branch}:projects/PY-006-invalid-unicode/main.py",
    )
    assert "Некорректный � заголовок" in readme
    ast.parse(main_py)
