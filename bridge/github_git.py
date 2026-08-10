import os
import re
import subprocess
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

    def ensure_branch(self, branch: str) -> str:
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
            self._run(
                [
                    "git",
                    "--git-dir",
                    str(self.git_dir),
                    "push",
                    self.repository_ssh,
                    (
                        f"refs/remotes/origin/{self.base_branch}:"
                        f"refs/heads/{branch}"
                    ),
                ]
            )
            return self._branch_url(branch)

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
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {self.ssh_key} -o IdentitiesOnly=yes "
            "-o StrictHostKeyChecking=yes "
            f"-o UserKnownHostsFile={self.known_hosts}"
        )
        return self.runner.run(command, env)

    def _branch_url(self, branch: str) -> str:
        return f"{self.repository_web}/tree/{branch}"
