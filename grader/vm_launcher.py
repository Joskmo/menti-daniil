import json
import os
import re
import socket
import socketserver
import stat
import struct
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

_MAX_REQUEST_BYTES = 3_000_000
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_SOURCE_BYTES = 2_000_000
_REQUEST_ID = re.compile(r"[0-9a-f]{32}")
_TARGET = re.compile(r"[A-Za-z0-9_./:-]{1,200}")
class LauncherProtocolError(RuntimeError):
    """A VM launcher request or response violated the bounded protocol."""


class _StudentSourceFailure(RuntimeError):
    pass


VmBackend = Callable[[tuple[dict[str, str], ...], dict[str, Any]], dict[str, Any]]


def validate_launcher_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "request_id",
        "source_files",
        "execution",
    }:
        raise LauncherProtocolError("invalid launcher request fields")
    if value["version"] != 1 or not _valid_request_id(value["request_id"]):
        raise LauncherProtocolError("invalid launcher request identity")
    source_files = value["source_files"]
    if not isinstance(source_files, list) or not source_files or len(source_files) > 100:
        raise LauncherProtocolError("source_files must be a bounded non-empty list")
    seen = set()
    total = 0
    for source_file in source_files:
        if not isinstance(source_file, dict) or set(source_file) != {"path", "content"}:
            raise LauncherProtocolError("invalid source file fields")
        path = _safe_path(source_file["path"], "source")
        if path in seen:
            raise LauncherProtocolError("invalid or duplicate source path")
        content = source_file["content"]
        if not isinstance(content, str) or "\x00" in content:
            raise LauncherProtocolError("source content must be UTF-8 text")
        size = len(content.encode("utf-8"))
        if size > 256_000:
            raise LauncherProtocolError("source file is too large")
        total += size
        seen.add(path)
    if total > _MAX_SOURCE_BYTES:
        raise LauncherProtocolError("source snapshot is too large")
    _validate_execution(value["execution"], value["request_id"])
    return value


class UnixVmLauncherClient:
    def __init__(self, socket_path: str | Path, *, timeout_seconds: float = 30) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.socket_path = Path(socket_path)
        self.timeout_seconds = timeout_seconds

    def execute(self, source_directory: Path, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("request_id") if isinstance(request, dict) else None
        try:
            source_files = _read_source(source_directory)
        except _StudentSourceFailure:
            return _student_failure_response(request_id)
        value = validate_launcher_request(
            {
                "version": 1,
                "request_id": request_id,
                "source_files": list(source_files),
                "execution": request,
            }
        )
        if len(_encode_frame(value)) > _MAX_REQUEST_BYTES:
            return _student_failure_response(request_id)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout_seconds)
            try:
                connection.connect(str(self.socket_path))
                _send_frame(connection, value, _MAX_REQUEST_BYTES)
                response = _receive_frame(connection, _MAX_RESPONSE_BYTES)
            except (OSError, TimeoutError) as error:
                raise LauncherProtocolError("VM launcher is unavailable") from error
        if not isinstance(response, dict):
            raise LauncherProtocolError("VM launcher returned an invalid response")
        return response


class _LauncherRequestHandler(socketserver.BaseRequestHandler):
    server: "UnixVmLauncherServer"

    def handle(self) -> None:
        request_id = "0" * 32
        try:
            if _peer_uid(self.request) not in self.server.allowed_uids:
                raise LauncherProtocolError("unauthorized launcher peer")
            request = validate_launcher_request(
                _receive_frame(self.request, _MAX_REQUEST_BYTES)
            )
            request_id = request["request_id"]
            response = self.server.backend(
                tuple(request["source_files"]),
                request["execution"],
            )
            if not isinstance(response, dict):
                raise LauncherProtocolError("VM backend response is invalid")
        except Exception:  # noqa: BLE001 - never leak launcher internals
            response = {
                "version": 1,
                "request_id": request_id,
                "status": "error",
                "observation": None,
            }
        try:
            _send_frame(self.request, response, _MAX_RESPONSE_BYTES)
        except OSError:
            pass


class UnixVmLauncherServer(socketserver.UnixStreamServer):
    allow_reuse_address = False

    def __init__(
        self,
        socket_path: str | Path,
        *,
        backend: VmBackend,
        allowed_uids: set[int],
    ) -> None:
        if not allowed_uids or any(
            isinstance(uid, bool) or not isinstance(uid, int) or uid < 0 for uid in allowed_uids
        ):
            raise ValueError("allowed_uids must contain numeric Unix user IDs")
        path = Path(socket_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.path.lexists(path):
            metadata = path.lstat()
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise LauncherProtocolError("refusing to replace unsafe launcher socket")
            path.unlink()
        previous_umask = os.umask(0o177)
        try:
            super().__init__(str(path), _LauncherRequestHandler)
        finally:
            os.umask(previous_umask)
        os.chmod(path, 0o600)
        self.socket_path = path
        self.backend = backend
        self.allowed_uids = frozenset(allowed_uids)

    def server_close(self) -> None:
        super().server_close()
        try:
            metadata = self.socket_path.lstat()
            if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.getuid():
                self.socket_path.unlink()
        except FileNotFoundError:
            pass


def _read_source(root: Path) -> tuple[dict[str, str], ...]:
    root = root.resolve()
    if root.is_symlink() or not root.is_dir():
        raise LauncherProtocolError("source root must be a real directory")
    files = []
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink():
            raise LauncherProtocolError("source snapshot must not contain symlinks")
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root).as_posix()
        _safe_path(relative, "source")
        try:
            raw_content = candidate.read_bytes()
        except OSError as error:
            raise LauncherProtocolError("source file must be bounded UTF-8 text") from error
        try:
            content = raw_content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise _StudentSourceFailure("source is not UTF-8") from error
        if "\x00" in content:
            raise _StudentSourceFailure("source contains NUL")
        files.append({"path": relative, "content": content})
    return tuple(files)


def _validate_execution(value: Any, request_id: str) -> None:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "request_id",
        "adapter",
        "target",
        "input",
        "observe_files",
    }:
        raise LauncherProtocolError("invalid execution fields")
    if value["version"] != 1 or value["request_id"] != request_id:
        raise LauncherProtocolError("execution identity mismatch")
    if value["adapter"] not in {"python_call", "cli"}:
        raise LauncherProtocolError("unsupported execution adapter")
    if not isinstance(value["target"], str) or not _TARGET.fullmatch(value["target"]):
        raise LauncherProtocolError("invalid execution target")
    if not isinstance(value["input"], dict):
        raise LauncherProtocolError("execution input must be an object")
    try:
        encoded = json.dumps(value["input"], ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise LauncherProtocolError("execution input must be JSON-safe") from error
    if len(encoded) > 500_000:
        raise LauncherProtocolError("execution input is too large")
    observe_files = value["observe_files"]
    if not isinstance(observe_files, list) or len(observe_files) > 20:
        raise LauncherProtocolError("observe_files must be a bounded list")
    paths = [_safe_path(path, "observed") for path in observe_files]
    if len(set(paths)) != len(paths):
        raise LauncherProtocolError("observe_files paths must be unique")


def _safe_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200 or "\x00" in value:
        raise LauncherProtocolError(f"invalid {label} path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        part in {"", ".", ".."} or part.startswith(".") for part in path.parts
    ):
        raise LauncherProtocolError(f"unsafe {label} path")
    return value


def _valid_request_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_REQUEST_ID.fullmatch(value))


def _peer_uid(connection: socket.socket) -> int:
    if not hasattr(socket, "SO_PEERCRED"):
        raise LauncherProtocolError("peer credentials are unavailable")
    credentials = connection.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize("3i"),
    )
    _, uid, _ = struct.unpack("3i", credentials)
    return uid


def _send_frame(connection: socket.socket, value: Any, maximum: int) -> None:
    payload = _encode_frame(value)
    if not payload or len(payload) > maximum:
        raise LauncherProtocolError("launcher frame has invalid size")
    connection.sendall(struct.pack("!I", len(payload)) + payload)


def _encode_frame(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise LauncherProtocolError("launcher frame is not valid JSON") from error


def _student_failure_response(request_id: Any) -> dict[str, Any]:
    if not _valid_request_id(request_id):
        raise LauncherProtocolError("invalid launcher request identity")
    return {
        "version": 1,
        "request_id": request_id,
        "status": "ok",
        "observation": {
            "return": None,
            "exception": "menti.StudentProcessFailure",
            "stdout": "",
            "stderr": "",
            "exit_code": 1,
            "args_after": None,
            "files": [],
        },
    }


def _receive_frame(connection: socket.socket, maximum: int) -> Any:
    header = _receive_exact(connection, 4)
    size = struct.unpack("!I", header)[0]
    if size <= 0 or size > maximum:
        raise LauncherProtocolError("launcher frame has invalid size")
    payload = _receive_exact(connection, size)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LauncherProtocolError("launcher frame is not valid JSON") from error


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise LauncherProtocolError("launcher connection closed mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
