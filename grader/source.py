import fcntl
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from grader.author import SourceFile

_PROJECT = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_BRANCH = re.compile(r"task/PY-[0-9]{3,}-[a-z0-9]+(?:-[a-z0-9]+)*")
_SHA = re.compile(r"[0-9a-f]{40}")
_SAFE_PATH = re.compile(r"[A-Za-z0-9._/-]+")
_ALLOWED_SUFFIXES = {".py", ".md", ".txt", ".toml", ".yaml", ".yml"}
_EXCLUDED_PARTS = {
    "answer",
    "answers",
    "expected",
    "fixture",
    "fixtures",
    "reference",
    "references",
    "solution",
    "solutions",
    "test",
    "tests",
}


class GitSourceLoader:
    def __init__(
        self,
        repository: str | Path,
        cache_dir: str | Path,
        *,
        max_files: int = 40,
        max_file_bytes: int = 64_000,
        max_total_bytes: int = 256_000,
    ) -> None:
        repository = str(repository)
        if not repository or "\x00" in repository or len(repository) > 2_000:
            raise ValueError("repository must be a bounded path or URL")
        if not 1 <= max_files <= 200:
            raise ValueError("max_files must be from 1 to 200")
        if not 1_024 <= max_file_bytes <= 500_000:
            raise ValueError("max_file_bytes is outside the safe range")
        if not max_file_bytes <= max_total_bytes <= 2_000_000:
            raise ValueError("max_total_bytes is outside the safe range")
        self.repository = repository
        self.cache_dir = Path(cache_dir)
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes

    def load(
        self,
        project: str,
        branch_name: str,
        starter_sha: str,
    ) -> tuple[SourceFile, ...]:
        self._validate_request(project, branch_name, starter_sha)
        with self._cache_lock():
            self._ensure_bare_repository()
            return self._load_locked(project, branch_name, starter_sha)

    def load_execution(
        self,
        project: str,
        branch_name: str,
        starter_sha: str,
    ) -> tuple[SourceFile, ...]:
        self._validate_request(project, branch_name, starter_sha)
        with self._cache_lock():
            self._ensure_bare_repository()
            remote_ref = self._fetch_branch(branch_name)
            fetched_sha = self._git_dir("rev-parse", "--verify", remote_ref).decode().strip()
            if fetched_sha != starter_sha:
                raise RuntimeError("task branch moved away from the pinned starter commit")
            return self._execution_files(starter_sha, project)

    def _validate_request(self, project: str, branch_name: str, commit_sha: str) -> None:
        if not _PROJECT.fullmatch(project) or len(project) > 64:
            raise ValueError("project is not a safe technical key")
        if not _BRANCH.fullmatch(branch_name) or len(branch_name) > 200:
            raise ValueError("branch name is not a supported task branch")
        if not _SHA.fullmatch(commit_sha):
            raise ValueError("commit SHA must be a lowercase Git SHA-1")

    def _cache_lock(self):
        self._prepare_parent()
        lock_path = self.cache_dir.with_name(f"{self.cache_dir.name}.lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        os.fchmod(descriptor, 0o600)
        return _FileLock(descriptor)

    def _prepare_parent(self) -> None:
        parent = self.cache_dir.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent.is_symlink() or not parent.is_dir():
            raise RuntimeError("Git cache parent must be a real directory")
        os.chmod(parent, 0o700)

    def _ensure_bare_repository(self) -> None:
        if self.cache_dir.exists():
            if self.cache_dir.is_symlink() or not self.cache_dir.is_dir():
                raise RuntimeError("Git cache must be a real directory")
            return
        self._run("init", "--bare", str(self.cache_dir))
        os.chmod(self.cache_dir, 0o700)

    def _fetch_branch(self, branch_name: str) -> str:
        remote_ref = f"refs/remotes/source/{branch_name}"
        self._git_dir(
            "fetch",
            "--force",
            "--no-tags",
            self.repository,
            f"refs/heads/{branch_name}:{remote_ref}",
        )
        return remote_ref

    def _load_locked(
        self,
        project: str,
        branch_name: str,
        starter_sha: str,
    ) -> tuple[SourceFile, ...]:
        remote_ref = self._fetch_branch(branch_name)
        fetched_sha = self._git_dir("rev-parse", "--verify", remote_ref).decode().strip()
        if fetched_sha != starter_sha:
            raise RuntimeError("task branch moved away from the pinned starter commit")
        prefix = f"projects/{project}/"
        output = self._tree(starter_sha, prefix)
        files: list[SourceFile] = []
        total_bytes = 0
        for mode, path in _tree_records(output):
            if mode == "120000" or not path.startswith(prefix):
                continue
            relative = path[len(prefix) :]
            if not self._include(relative):
                continue
            content = self._git_dir("cat-file", "blob", f"{starter_sha}:{path}")
            if len(content) > self.max_file_bytes:
                continue
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
            total_bytes += len(content)
            if total_bytes > self.max_total_bytes:
                raise RuntimeError("project source exceeds the authoring byte limit")
            files.append(SourceFile(relative, text))
            if len(files) > self.max_files:
                raise RuntimeError("project has too many author-visible source files")
        if not files:
            raise RuntimeError("project has no bounded author-visible source files")
        return tuple(files)

    def _tree(self, commit_sha: str, prefix: str) -> bytes:
        return self._git_dir(
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            commit_sha,
            "--",
            prefix,
        )

    def _execution_files(self, commit_sha: str, project: str) -> tuple[SourceFile, ...]:
        prefix = f"projects/{project}/"
        records: list[SourceFile] = []
        total_bytes = 0
        for mode, path in _tree_records(self._tree(commit_sha, prefix)):
            if not path.startswith(prefix):
                raise RuntimeError("Git returned a blob outside the project prefix")
            relative = path[len(prefix) :]
            if mode == "120000":
                raise RuntimeError("execution source must not contain symlinks")
            if not _safe_execution_path(relative):
                raise RuntimeError("execution source contains an unsupported path")
            content = self._git_dir("cat-file", "blob", f"{commit_sha}:{path}")
            if len(content) > self.max_file_bytes:
                raise RuntimeError("execution source file exceeds the byte limit")
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise RuntimeError("execution source must be UTF-8 text") from error
            total_bytes += len(content)
            if total_bytes > self.max_total_bytes:
                raise RuntimeError("execution source exceeds the total byte limit")
            records.append(SourceFile(relative, text))
            if len(records) > self.max_files:
                raise RuntimeError("execution source contains too many files")
        if not records:
            raise RuntimeError("execution project contains no source files")
        return tuple(records)

    def _include(self, relative: str) -> bool:
        if not relative or len(relative) > 200 or not _SAFE_PATH.fullmatch(relative):
            return False
        path = PurePosixPath(relative)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            return False
        lowered = tuple(part.lower() for part in path.parts)
        if any(part.startswith(".") or part in _EXCLUDED_PARTS for part in lowered):
            return False
        name = lowered[-1]
        if name.startswith("test_") or name.endswith("_test.py"):
            return False
        return path.suffix.lower() in _ALLOWED_SUFFIXES

    def _git_dir(self, *args: str) -> bytes:
        return self._run(f"--git-dir={self.cache_dir}", *args)

    @staticmethod
    def _run(*args: str) -> bytes:
        environment = {
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        }
        try:
            return subprocess.run(
                ["git", *args],
                check=True,
                capture_output=True,
                env=environment,
                timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("safe Git source operation failed") from error


class GitProjectExporter(GitSourceLoader):
    def __init__(
        self,
        repository: str | Path,
        cache_dir: str | Path,
        *,
        max_files: int = 100,
        max_file_bytes: int = 256_000,
        max_total_bytes: int = 2_000_000,
    ) -> None:
        super().__init__(
            repository,
            cache_dir,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )

    def export(
        self,
        project: str,
        branch_name: str,
        starter_sha: str,
        commit_sha: str,
        destination: str | Path,
    ) -> Path:
        self._validate_request(project, branch_name, starter_sha)
        if not _SHA.fullmatch(commit_sha):
            raise ValueError("commit SHA must be a lowercase Git SHA-1")
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise ValueError("grading destination must be fresh")
        if destination.parent.is_symlink() or not destination.parent.is_dir():
            raise ValueError("grading destination parent must be a real directory")
        with self._cache_lock():
            self._ensure_bare_repository()
            remote_ref = self._fetch_branch(branch_name)
            try:
                self._git_dir("merge-base", "--is-ancestor", starter_sha, commit_sha)
            except RuntimeError as error:
                raise RuntimeError(
                    "grading commit does not descend from the pinned starter"
                ) from error
            try:
                self._git_dir("merge-base", "--is-ancestor", commit_sha, remote_ref)
            except RuntimeError as error:
                raise RuntimeError(
                    "grading commit is not reachable from the task branch"
                ) from error
            records = self._execution_files(commit_sha, project)
            destination.mkdir(mode=0o700)
            for source_file in records:
                output_path = destination / PurePosixPath(source_file.path)
                output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                output_path.write_text(source_file.content, encoding="utf-8")
                os.chmod(output_path, 0o600)
            return destination


class _FileLock:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor

    def __enter__(self):
        fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        os.close(self.descriptor)


def _tree_records(output: bytes):
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or fields[1] != b"blob":
            raise RuntimeError("Git returned malformed tree data")
        try:
            path = raw_path.decode("ascii")
            mode = fields[0].decode("ascii")
        except UnicodeDecodeError as error:
            raise RuntimeError("Git returned a non-ASCII project path") from error
        yield mode, path


def _safe_execution_path(relative: str) -> bool:
    if not relative or len(relative) > 200 or not _SAFE_PATH.fullmatch(relative):
        return False
    path = PurePosixPath(relative)
    if path.is_absolute() or any(
        part in {"", ".", ".."} or part.startswith(".") for part in path.parts
    ):
        return False
    return True
