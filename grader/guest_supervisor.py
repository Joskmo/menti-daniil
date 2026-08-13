import base64
import json
import os
import re
import resource
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

_REQUEST_ID = re.compile(r"[0-9a-f]{32}")
_MAX_COMPLETE_OBSERVATION_BYTES = 500_000
_MAX_OBSERVED_FILE_BYTES = 400_000


class GuestSupervisor:
    def __init__(
        self,
        *,
        runner: str | Path,
        student_uid: int = 65534,
        student_gid: int = 65534,
        timeout_seconds: float = 6,
    ) -> None:
        self.runner = Path(runner).resolve()
        if self.runner.is_symlink() or not self.runner.is_file():
            raise ValueError("guest case runner must be a regular file")
        if student_uid < 0 or student_gid < 0:
            raise ValueError("student uid and gid must not be negative")
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("timeout_seconds must be from 0 to 30")
        self.student_uid = student_uid
        self.student_gid = student_gid
        self.timeout_seconds = timeout_seconds

    def execute(
        self,
        input_root: str | Path,
        workspace: str | Path,
        execution: Any,
    ) -> dict[str, Any]:
        request_id = (
            execution.get("request_id")
            if isinstance(execution, dict) and _valid_request_id(execution.get("request_id"))
            else "0" * 32
        )
        try:
            _validate_execution(execution)
            input_root = Path(input_root).resolve()
            source = input_root / "source"
            if source.is_symlink() or not source.is_dir():
                raise ValueError("input source must be a real directory")
            workspace = Path(workspace)
            if workspace.exists() or workspace.is_symlink():
                raise ValueError("workspace must be fresh")
            workspace.mkdir(parents=True, mode=0o700)
            _copy_source(source, workspace)
            for fixture in execution["input"]["files"]:
                _write_fixture(workspace, fixture["path"], fixture["content"])
            self._set_workspace_owner(workspace)
            child_request = {
                "adapter": execution["adapter"],
                "target": execution["target"],
                "input": {
                    key: value
                    for key, value in execution["input"].items()
                    if key != "files"
                },
            }
            completed = subprocess.run(
                [sys.executable, str(self.runner)],
                cwd=workspace,
                input=json.dumps(
                    child_request,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ),
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
                env={
                    "HOME": str(workspace),
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "MENTI_STUDENT_TIMEOUT_SECONDS": str(
                        min(5.0, max(0.1, self.timeout_seconds - 1.0))
                    ),
                    "PATH": "/usr/bin:/bin",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONHASHSEED": "0",
                    "PYTHONIOENCODING": "utf-8",
                    "TZ": "UTC",
                },
                preexec_fn=self._sandbox_child,
            )
            if completed.returncode != 0:
                raise RuntimeError("trusted student runner exited unsuccessfully")
            if len(completed.stdout.encode("utf-8")) > 1_000_000:
                raise RuntimeError("student child produced an oversized observation")
            observation = json.loads(completed.stdout)
            _validate_child_observation(observation)
            observation["files"] = [
                _observe_file(workspace, path) for path in execution["observe_files"]
            ]
            if not _valid_complete_observation(observation):
                observation = _student_process_failure()
                observation["files"] = []
            return {
                "version": 1,
                "request_id": request_id,
                "status": "ok",
                "observation": observation,
            }
        except Exception:  # noqa: BLE001 - guest must return a generic bounded failure
            return {
                "version": 1,
                "request_id": request_id,
                "status": "error",
                "observation": None,
            }

    def _set_workspace_owner(self, workspace: Path) -> None:
        if os.geteuid() != 0:
            if os.getuid() != self.student_uid or os.getgid() != self.student_gid:
                raise PermissionError("non-root supervisor cannot switch student identity")
            return
        for candidate in [workspace, *workspace.rglob("*")]:
            os.chown(candidate, self.student_uid, self.student_gid, follow_symlinks=False)

    def _sandbox_child(self) -> None:
        os.setsid()
        os.umask(0o077)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        if os.geteuid() == 0:
            resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
            os.setgroups([])
            os.setgid(self.student_gid)
            os.setuid(self.student_uid)


def _validate_execution(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "request_id",
        "adapter",
        "target",
        "input",
        "observe_files",
    }:
        raise ValueError("invalid execution fields")
    if value["version"] != 1 or not _valid_request_id(value["request_id"]):
        raise ValueError("invalid execution identity")
    if value["adapter"] not in {"python_call", "cli"}:
        raise ValueError("invalid adapter")
    if not isinstance(value["target"], str) or len(value["target"]) > 200:
        raise ValueError("invalid target")
    input_spec = value["input"]
    if not isinstance(input_spec, dict) or "files" not in input_spec:
        raise ValueError("invalid input")
    files = input_spec["files"]
    if not isinstance(files, list) or len(files) > 20:
        raise ValueError("invalid fixture files")
    for fixture in files:
        if not isinstance(fixture, dict) or set(fixture) != {"path", "content"}:
            raise ValueError("invalid fixture")
        _safe_path(fixture["path"])
        if not isinstance(fixture["content"], str) or len(fixture["content"].encode()) > 65_536:
            raise ValueError("invalid fixture content")
    observe_files = value["observe_files"]
    if not isinstance(observe_files, list) or len(observe_files) > 20:
        raise ValueError("invalid observed file list")
    for path in observe_files:
        _safe_path(path)


def _copy_source(source: Path, workspace: Path) -> None:
    for candidate in sorted(source.rglob("*")):
        if candidate.is_symlink():
            raise ValueError("source must not contain symlinks")
        relative = candidate.relative_to(source)
        destination = workspace / relative
        if candidate.is_dir():
            destination.mkdir(exist_ok=True, mode=0o700)
        elif candidate.is_file():
            if candidate.stat().st_size > 256_000:
                raise ValueError("source file too large")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(candidate, destination, follow_symlinks=False)
            os.chmod(destination, 0o600)


def _write_fixture(workspace: Path, relative: str, content: str) -> None:
    destination = workspace / _safe_path(relative)
    if destination.exists() or destination.is_symlink():
        raise ValueError("fixture collides with project source")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.write_text(content, encoding="utf-8")
    os.chmod(destination, 0o600)


def _observe_file(workspace: Path, relative: str) -> dict[str, Any]:
    path = workspace / _safe_path(relative)
    if path.is_symlink() or not path.is_file():
        return {"path": relative, "present": False, "content": None, "truncated": False}
    if path.stat().st_size > _MAX_OBSERVED_FILE_BYTES:
        return {"path": relative, "present": True, "content": None, "truncated": True}
    try:
        content = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return {"path": relative, "present": True, "content": None, "truncated": True}
    return {"path": relative, "present": True, "content": content, "truncated": False}


def _validate_child_observation(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "return",
        "exception",
        "stdout",
        "stderr",
        "exit_code",
        "args_after",
    }:
        raise ValueError("invalid child observation")
    json.dumps(value["return"], ensure_ascii=False, allow_nan=False)
    json.dumps(value["args_after"], ensure_ascii=False, allow_nan=False)
    if value["args_after"] is not None and not isinstance(value["args_after"], list):
        raise ValueError("invalid child arguments")
    if value["exception"] is not None and not isinstance(value["exception"], str):
        raise ValueError("invalid child exception")
    if not isinstance(value["stdout"], str) or not isinstance(value["stderr"], str):
        raise ValueError("invalid child output")
    if isinstance(value["exit_code"], bool) or not isinstance(value["exit_code"], int):
        raise ValueError("invalid child exit code")


def _valid_complete_observation(value: dict[str, Any]) -> bool:
    files = value.get("files")
    if not isinstance(files, list):
        return False
    for item in files:
        if not isinstance(item, dict):
            return False
        content = item.get("content")
        if content is not None and (not isinstance(content, str) or "\x00" in content):
            return False
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return False
    return len(encoded) <= _MAX_COMPLETE_OBSERVATION_BYTES


def _student_process_failure() -> dict[str, Any]:
    return {
        "return": None,
        "exception": "menti.StudentProcessFailure",
        "stdout": "",
        "stderr": "",
        "exit_code": 1,
        "args_after": None,
    }


def _safe_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or len(value) > 200 or "\x00" in value:
        raise ValueError("invalid relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        part in {"", ".", ".."} or part.startswith(".") for part in path.parts
    ):
        raise ValueError("unsafe relative path")
    return path


def _valid_request_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_REQUEST_ID.fullmatch(value))


def main() -> int:
    request_id = "0" * 32
    try:
        request_path = Path(sys.argv[1] if len(sys.argv) > 1 else "/input/request.json")
        execution = json.loads(request_path.read_text(encoding="utf-8"))
        if isinstance(execution, dict) and _valid_request_id(execution.get("request_id")):
            request_id = execution["request_id"]
        supervisor = GuestSupervisor(
            runner="/opt/menti/guest_case_runner.py",
            student_uid=65534,
            student_gid=65534,
        )
        response = supervisor.execute(request_path.parent, "/work/workspace", execution)
    except Exception:  # noqa: BLE001 - never emit guest internals on serial
        response = {
            "version": 1,
            "request_id": request_id,
            "status": "error",
            "observation": None,
        }
    payload = base64.b64encode(
        json.dumps(
            response,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    print(f"MENTI_RESULT:{payload}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
