import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
from typing import Any


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or set(request) != {"adapter", "target", "input"}:
            return 2
        if request["adapter"] == "python_call":
            observation = _python_call(request["target"], request["input"])
        elif request["adapter"] == "cli":
            observation = _cli(request["target"], request["input"])
        else:
            return 2
        sys.stdout.write(
            json.dumps(
                observation,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
        return 0
    except BaseException:  # noqa: BLE001 - student failures must stay bounded
        observation = _student_process_failure()
        try:
            sys.stdout.write(
                json.dumps(
                    observation,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
        except BaseException:  # noqa: BLE001 - broken runner means infrastructure failure
            return 2
        return 0


def _python_call(target: str, input_spec: dict[str, Any]) -> dict[str, Any]:
    module_name, function_name = target.split(":", 1)
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    result: Any = None
    exception: str | None = None
    args_after: list[Any] | None = input_spec["args"]
    try:
        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(
            captured_stderr
        ):
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
        "stdout": captured_stdout.getvalue(),
        "stderr": captured_stderr.getvalue(),
        "exit_code": 0,
        "args_after": args_after,
    }


def _cli(target: str, input_spec: dict[str, Any]) -> dict[str, Any]:
    environment = {
        "HOME": os.getcwd(),
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    completed = subprocess.run(
        [sys.executable, target, *input_spec["argv"]],
        input=input_spec["stdin"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=environment,
    )
    return {
        "return": None,
        "exception": None,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
        "args_after": None,
    }


def _student_process_failure() -> dict[str, Any]:
    return {
        "return": None,
        "exception": "menti.StudentProcessFailure",
        "stdout": "",
        "stderr": "",
        "exit_code": 1,
        "args_after": None,
    }


if __name__ == "__main__":
    raise SystemExit(main())
