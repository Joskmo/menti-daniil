import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Protocol

_BRANCH_PATTERN = re.compile(r"task/PY-\d{3,}-[a-z0-9]+(?:-[a-z0-9]+)*")


class Runner(Protocol):
    def run(self, command: list[str], env: dict[str, str]) -> str: ...


class SubprocessRunner:
    def run(self, command: list[str], env: dict[str, str]) -> str:
        completed = subprocess.run(
            command,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return completed.stdout


class GitBranchClient:
    def __init__(
        self,
        repository_ssh: str,
        repository_web: str,
        base_branch: str,
        git_dir: str | Path,
        ssh_key: str | Path,
        known_hosts: str | Path,
        runner: Runner | None = None,
    ) -> None:
        self.repository_ssh = repository_ssh
        self.repository_web = repository_web.rstrip("/")
        self.base_branch = base_branch
        self.git_dir = Path(git_dir)
        self.ssh_key = Path(ssh_key)
        self.known_hosts = Path(known_hosts)
        self.runner = runner or SubprocessRunner()
        self._lock = threading.Lock()

    def ensure_branch(self, branch: str, task_title: str) -> str:
        if not _BRANCH_PATTERN.fullmatch(branch):
            raise ValueError(f"Unsafe branch name: {branch!r}")

        with self._lock:
            if self._branch_exists(branch):
                return self._branch_url(branch)

            self.git_dir.parent.mkdir(parents=True, exist_ok=True)
            if not self.git_dir.exists():
                self._run(["git", "init", "--bare", str(self.git_dir)])
            self._run(
                [
                    "git",
                    "--git-dir",
                    str(self.git_dir),
                    "fetch",
                    "--force",
                    "--no-tags",
                    self.repository_ssh,
                    f"refs/heads/{self.base_branch}:refs/remotes/origin/{self.base_branch}",
                ]
            )
            commit = self._project_commit(branch, task_title)
            self._run(
                [
                    "git",
                    "--git-dir",
                    str(self.git_dir),
                    "push",
                    self.repository_ssh,
                    f"{commit}:refs/heads/{branch}",
                ]
            )
            return self._branch_url(branch)

    def _project_commit(self, branch: str, task_title: str) -> str:
        project_name = branch.removeprefix("task/")
        task_id = "-".join(project_name.split("-", 2)[:2])
        base_ref = f"refs/remotes/origin/{self.base_branch}"
        normalized_title = " ".join(task_title.split()) or task_id
        safe_title = re.sub(
            r"[\ud800-\udfff]",
            "\N{REPLACEMENT CHARACTER}",
            normalized_title,
        )
        self._run(
            [
                "git",
                "--git-dir",
                str(self.git_dir),
                "worktree",
                "prune",
            ]
        )
        with tempfile.TemporaryDirectory(
            dir=self.git_dir.parent,
            prefix="project-scaffold-",
        ) as temporary_directory:
            workspace = Path(temporary_directory) / "worktree"
            self._run(
                [
                    "git",
                    "--git-dir",
                    str(self.git_dir),
                    "worktree",
                    "add",
                    "--detach",
                    str(workspace),
                    base_ref,
                ]
            )
            try:
                projects_root = workspace / "projects"
                if projects_root.is_symlink() or not projects_root.is_dir():
                    raise RuntimeError("Repository projects path is not a safe directory")
                project_directory = projects_root / project_name
                if project_directory.is_symlink():
                    raise RuntimeError("Project path must not be a symbolic link")
                if project_directory.exists() and not project_directory.is_dir():
                    raise RuntimeError("Project path is not a safe directory")
                if not project_directory.exists():
                    project_directory.mkdir()
                    (project_directory / "README.md").write_text(
                        f"# {task_id} — {safe_title}\n\n"
                        "Учебный проект по задаче из Yonote.\n\n"
                        "Начни реализацию в `main.py`.\n",
                        encoding="utf-8",
                    )
                    module_title = f"{task_id}: {safe_title}."
                    (project_directory / "main.py").write_text(
                        f"{module_title!r}\n",
                        encoding="utf-8",
                    )
                    self._run(
                        [
                            "git",
                            "-C",
                            str(workspace),
                            "add",
                            "--",
                            f"projects/{project_name}",
                        ]
                    )
                    self._run(
                        [
                            "git",
                            "-C",
                            str(workspace),
                            "commit",
                            "-m",
                            f"chore({task_id}): initialize project scaffold",
                        ]
                    )
                return self._run(
                    ["git", "-C", str(workspace), "rev-parse", "HEAD"]
                ).strip()
            finally:
                self._run(
                    [
                        "git",
                        "--git-dir",
                        str(self.git_dir),
                        "worktree",
                        "remove",
                        "--force",
                        str(workspace),
                    ]
                )

    def _branch_exists(self, branch: str) -> bool:
        output = self._run(
            [
                "git",
                "ls-remote",
                "--heads",
                self.repository_ssh,
                f"refs/heads/{branch}",
            ]
        )
        return bool(output.strip())

    def _run(self, command: list[str]) -> str:
        env = os.environ.copy()
        for variable in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_NAMESPACE",
        ):
            env.pop(variable, None)
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {self.ssh_key} -o IdentitiesOnly=yes "
            "-o StrictHostKeyChecking=yes "
            f"-o UserKnownHostsFile={self.known_hosts}"
        )
        env["GIT_AUTHOR_NAME"] = "Yonote GitHub Bridge"
        env["GIT_AUTHOR_EMAIL"] = "bridge@menti-github.jos-dev.ru"
        env["GIT_COMMITTER_NAME"] = env["GIT_AUTHOR_NAME"]
        env["GIT_COMMITTER_EMAIL"] = env["GIT_AUTHOR_EMAIL"]
        return self.runner.run(command, env)

    def _branch_url(self, branch: str) -> str:
        return f"{self.repository_web}/tree/{branch}"
