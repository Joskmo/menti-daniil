import subprocess
from pathlib import Path

import pytest

from grader.source import GitProjectExporter, GitSourceLoader


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    _git("init", "--bare", str(remote))
    _git("init", "--initial-branch=main", str(work))
    _git("config", "user.name", "Test", cwd=work)
    _git("config", "user.email", "test@example.com", cwd=work)
    project = work / "projects" / "json"
    project.mkdir(parents=True)
    (project / "main.py").write_text("def next_id(rows): return len(rows) + 1\n")
    (project / "README.md").write_text("# Public assignment\n")
    (project / "test_solution.py").write_text("EXPECTED = 9\n")
    (project / "fixtures").mkdir()
    (project / "fixtures" / "expected.txt").write_text("secret oracle\n")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "starter", cwd=work)
    branch = "task/PY-002-next-id"
    _git("branch", branch, cwd=work)
    _git("remote", "add", "origin", str(remote), cwd=work)
    _git("push", "origin", "main", branch, cwd=work)
    sha = _git("rev-parse", branch, cwd=work)
    return remote, branch, sha


def test_source_loader_reads_only_bounded_non_test_project_blobs(tmp_path, monkeypatch) -> None:
    remote, branch, sha = _repository(tmp_path)
    monkeypatch.setenv("GIT_DIR", "/attacker/repository.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/attacker/worktree")
    loader = GitSourceLoader(remote, tmp_path / "cache.git")

    files = loader.load("json", branch, sha)

    assert [(file.path, file.content) for file in files] == [
        ("README.md", "# Public assignment\n"),
        ("main.py", "def next_id(rows): return len(rows) + 1\n"),
    ]


def test_project_exporter_materializes_exact_reachable_commit_without_checkout(tmp_path) -> None:
    remote, branch, starter_sha = _repository(tmp_path)
    work = tmp_path / "work"
    _git("checkout", branch, cwd=work)
    project = work / "projects" / "json"
    (project / "data.json").write_text('[{"id": 1}]\n')
    (project / "settings.ini").write_text("mode=strict\n")
    (project / "test_solution.py").write_text("def test_public(): pass\n")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "student commit", cwd=work)
    commit_sha = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "origin", branch, cwd=work)
    destination = tmp_path / "exported"

    GitProjectExporter(remote, tmp_path / "grade-cache.git").export(
        "json",
        branch,
        starter_sha,
        commit_sha,
        destination,
    )

    assert sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*")) == [
        "README.md",
        "data.json",
        "fixtures",
        "fixtures/expected.txt",
        "main.py",
        "settings.ini",
        "test_solution.py",
    ]
    assert not (destination / ".git").exists()


def test_project_exporter_rejects_force_pushed_unrelated_history(tmp_path) -> None:
    remote, branch, starter_sha = _repository(tmp_path)
    work = tmp_path / "work"
    _git("checkout", "--orphan", "replacement", cwd=work)
    _git("rm", "-rf", ".", cwd=work)
    project = work / "projects" / "json"
    project.mkdir(parents=True)
    (project / "main.py").write_text("def next_id(rows): return 999\n")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "unrelated replacement", cwd=work)
    replacement_sha = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "--force", "origin", f"HEAD:{branch}", cwd=work)

    with pytest.raises(RuntimeError, match="pinned starter"):
        GitProjectExporter(remote, tmp_path / "grade-cache.git").export(
            "json",
            branch,
            starter_sha,
            replacement_sha,
            tmp_path / "exported",
        )


def test_source_loader_fails_closed_when_remote_branch_moved_from_pinned_sha(tmp_path) -> None:
    remote, branch, sha = _repository(tmp_path)
    work = tmp_path / "work"
    _git("checkout", branch, cwd=work)
    (work / "projects" / "json" / "main.py").write_text("CHANGED = True\n")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "move branch", cwd=work)
    _git("push", "origin", branch, cwd=work)
    loader = GitSourceLoader(remote, tmp_path / "cache.git")

    with pytest.raises(RuntimeError, match="pinned starter commit"):
        loader.load("json", branch, sha)


def test_source_loader_keeps_exact_execution_tree_separate_from_llm_snapshot(tmp_path) -> None:
    remote, branch, sha = _repository(tmp_path)
    work = tmp_path / "work"
    _git("checkout", branch, cwd=work)
    project = work / "projects" / "json"
    (project / "settings.ini").write_text("mode=strict\n")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "add runtime configuration", cwd=work)
    _git("push", "origin", branch, cwd=work)
    sha = _git("rev-parse", "HEAD", cwd=work)
    loader = GitSourceLoader(remote, tmp_path / "cache.git")

    author_visible = loader.load("json", branch, sha)
    execution = loader.load_execution("json", branch, sha)

    assert "settings.ini" not in {item.path for item in author_visible}
    assert {item.path for item in execution} >= {
        "main.py",
        "settings.ini",
        "test_solution.py",
        "fixtures/expected.txt",
    }
