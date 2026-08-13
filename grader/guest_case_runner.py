import base64
import binascii
import ctypes
import importlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_STUDENT_STARTED = "MENTI_STUDENT_STARTED\n"
_MAX_OBSERVATION_BYTES = 450_000
_MAX_PROCESS_OUTPUT_BYTES = 1_000_000
_PR_SET_CHILD_SUBREAPER = 36
_PROTOCOL_NONCE = re.compile(r"[0-9a-f]{64}")
_PROTOCOL_PREFIX = b"MENTI_OBSERVATION:"


def main() -> int:
    if sys.argv[1:] == ["--python-call-child"]:
        return _python_call_child_main()
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or set(request) != {"adapter", "target", "input"}:
            return 2
        if request["adapter"] == "python_call":
            observation = _isolated_python_call(request["target"], request["input"])
        elif request["adapter"] == "cli":
            observation = _cli(request["target"], request["input"])
        else:
            return 2
        observation = _bounded_observation(observation)
        sys.stdout.write(
            json.dumps(
                observation,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
        return 0
    except BaseException:  # noqa: BLE001 - broken trusted runner is infrastructure failure
        return 2


def _python_call_child_main() -> int:
    nonce: str | None = None
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or set(request) != {"target", "input", "nonce"}:
            return 2
        nonce = request["nonce"]
        if not isinstance(nonce, str) or not _PROTOCOL_NONCE.fullmatch(nonce):
            return 2
        sys.stderr.write(_STUDENT_STARTED)
        sys.stderr.flush()
        observation = _captured_python_call(request["target"], request["input"])
        payload = json.dumps(
            observation,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except BaseException:  # noqa: BLE001 - student failures must stay bounded
        if nonce is None:
            return 2
        payload = json.dumps(_student_process_failure(), separators=(",", ":"))
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    sys.stdout.write(f"{_PROTOCOL_PREFIX.decode()}{nonce}:{encoded}")
    return 0


def _captured_python_call(target: str, input_spec: dict[str, Any]) -> dict[str, Any]:
    _enable_subreaper()
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        sys.stdout.flush()
        sys.stderr.flush()
        protocol_stdout = os.dup(sys.stdout.fileno())
        protocol_stderr = os.dup(sys.stderr.fileno())
        try:
            os.dup2(stdout_file.fileno(), sys.stdout.fileno())
            os.dup2(stderr_file.fileno(), sys.stderr.fileno())
            observation = _python_call(target, input_spec)
            _terminate_descendants()
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os.dup2(protocol_stdout, sys.stdout.fileno())
            os.dup2(protocol_stderr, sys.stderr.fileno())
            os.close(protocol_stdout)
            os.close(protocol_stderr)
        stdout = _read_bounded(stdout_file)
        stderr = _read_bounded(stderr_file)
    if len(stdout) > _MAX_PROCESS_OUTPUT_BYTES or len(stderr) > _MAX_PROCESS_OUTPUT_BYTES:
        return _student_process_failure()
    try:
        observation["stdout"] = stdout.decode("utf-8")
        observation["stderr"] = stderr.decode("utf-8")
    except UnicodeDecodeError:
        return _student_process_failure()
    return _bounded_observation(observation)


def _isolated_python_call(target: str, input_spec: dict[str, Any]) -> dict[str, Any]:
    nonce = secrets.token_hex(32)
    request = json.dumps(
        {"target": target, "input": input_spec, "nonce": nonce},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return_code, stdout, stderr, oversized = _bounded_process(
        [sys.executable, str(Path(__file__).resolve()), "--python-call-child"],
        input_bytes=request.encode("utf-8"),
        environment=os.environ.copy(),
    )
    if oversized:
        if _student_started(stderr):
            return _student_process_failure()
        raise RuntimeError("student child exceeded trusted output limits")
    if return_code != 0:
        if _student_started(stderr):
            return _student_process_failure()
        raise RuntimeError("student child failed before invocation")
    try:
        observation = _decode_protocol_observation(stdout, nonce)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        if _student_started(stderr):
            return _student_process_failure()
        raise
    if not isinstance(observation, dict):
        raise RuntimeError("student child returned invalid observation")
    return observation


def _decode_protocol_observation(payload: bytes, nonce: str) -> Any:
    prefix = _PROTOCOL_PREFIX + nonce.encode("ascii") + b":"
    if not payload.startswith(prefix):
        raise ValueError("student child returned no trusted frame")
    encoded = payload[len(prefix) :]
    if not encoded or b":" in encoded or any(
        character in b" \t\r\n" for character in encoded
    ):
        raise ValueError("student child returned an invalid trusted frame")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except binascii.Error as error:
        raise ValueError("student child returned an invalid trusted frame") from error
    return json.loads(decoded.decode("utf-8"))


def _python_call(target: str, input_spec: dict[str, Any]) -> dict[str, Any]:
    module_name, function_name = target.split(":", 1)
    result: Any = None
    exception: str | None = None
    args_after: list[Any] | None = input_spec["args"]
    try:
        sys.path.insert(0, os.getcwd())
        module = importlib.import_module(module_name)
        function = getattr(module, function_name)
        result = function(*input_spec["args"], **input_spec["kwargs"])
    except BaseException as error:  # noqa: BLE001 - student failures are observations
        error_type = type(error)
        exception = f"{error_type.__module__}.{error_type.__qualname__}"
        result = None
    else:
        try:
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise RuntimeError("student return value is not canonical JSON") from error
    try:
        json.dumps(args_after, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        args_after = None
    return {
        "return": result,
        "exception": exception,
        "stdout": "",
        "stderr": "",
        "exit_code": 0,
        "args_after": args_after,
    }


def _cli(target: str, input_spec: dict[str, Any]) -> dict[str, Any]:
    environment = {
        "HOME": os.getcwd(),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        "TZ": "UTC",
    }
    return_code, stdout, stderr, oversized = _bounded_process(
        [sys.executable, target, *input_spec["argv"]],
        input_bytes=input_spec["stdin"].encode("utf-8"),
        environment=environment,
    )
    if oversized or return_code < 0:
        return _student_process_failure()
    try:
        decoded_stdout = stdout.decode("utf-8")
        decoded_stderr = stderr.decode("utf-8")
    except UnicodeDecodeError:
        return _student_process_failure()
    return {
        "return": None,
        "exception": None,
        "stdout": decoded_stdout,
        "stderr": decoded_stderr,
        "exit_code": return_code,
        "args_after": None,
    }


def _student_started(stderr: str | bytes | None) -> bool:
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="ignore")
    return isinstance(stderr, str) and stderr.startswith(_STUDENT_STARTED)


def _bounded_process(
    command: list[str],
    *,
    input_bytes: bytes,
    environment: dict[str, str],
) -> tuple[int, bytes, bytes, bool]:
    _enable_subreaper()
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
            env=environment,
        )
        try:
            process.communicate(input_bytes, timeout=_student_timeout())
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            process.wait()
        else:
            _kill_process_group(process)
        _terminate_descendants()
        stdout = _read_bounded(stdout_file)
        stderr = _read_bounded(stderr_file)
    oversized = len(stdout) > _MAX_PROCESS_OUTPUT_BYTES or len(stderr) > _MAX_PROCESS_OUTPUT_BYTES
    return (
        process.returncode,
        stdout[:_MAX_PROCESS_OUTPUT_BYTES],
        stderr[:_MAX_PROCESS_OUTPUT_BYTES],
        oversized,
    )


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _enable_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _terminate_descendants() -> None:
    deadline = time.monotonic() + 0.5
    while True:
        children = _direct_children(os.getpid())
        if not children:
            _reap_children()
            return
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        _reap_children()
        if time.monotonic() >= deadline:
            raise RuntimeError("student descendants did not terminate")
        time.sleep(0.005)


def _direct_children(parent_pid: int) -> list[int]:
    children = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for line in status.splitlines():
            if line.startswith("PPid:") and int(line.split()[1]) == parent_pid:
                children.append(int(entry.name))
                break
    return children


def _reap_children() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _read_bounded(stream) -> bytes:
    stream.seek(0)
    return stream.read(_MAX_PROCESS_OUTPUT_BYTES + 1)


def _bounded_observation(observation: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.dumps(
            observation,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return _student_process_failure()
    if len(payload) > _MAX_OBSERVATION_BYTES:
        return _student_process_failure()
    for field in ("stdout", "stderr"):
        value = observation.get(field)
        if not isinstance(value, str) or "\x00" in value:
            return _student_process_failure()
    return observation


def _student_process_failure() -> dict[str, Any]:
    return {
        "return": None,
        "exception": "menti.StudentProcessFailure",
        "stdout": "",
        "stderr": "",
        "exit_code": 1,
        "args_after": None,
    }


def _student_timeout() -> float:
    try:
        value = float(os.environ.get("MENTI_STUDENT_TIMEOUT_SECONDS", "5"))
    except ValueError:
        return 5
    return value if 0 < value <= 5 else 5


if __name__ == "__main__":
    raise SystemExit(main())
