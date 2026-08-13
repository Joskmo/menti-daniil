import contextlib
import importlib
import io
import json
import os
import sys
from typing import Any


def main() -> int:
    target = sys.argv[1]
    module_name, function_name = target.split(":", 1)
    request = json.loads(sys.stdin.read())
    sys.path.insert(0, os.getcwd())
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    result: Any = None
    exception: str | None = None
    exception_message: str | None = None
    try:
        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(
            captured_stderr
        ):
            module = importlib.import_module(module_name)
            function = getattr(module, function_name)
            result = function(*request["args"], **request["kwargs"])
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except BaseException as error:  # noqa: BLE001 - the envelope must capture student failures
        error_type = type(error)
        exception = f"{error_type.__module__}.{error_type.__qualname__}"
        exception_message = str(error)[:2_000]
        result = None
    args_after: list[Any] | None = request["args"]
    try:
        json.dumps(args_after, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        args_after = None
    envelope = {
        "return": result,
        "exception": exception,
        "exception_message": exception_message,
        "stdout": captured_stdout.getvalue(),
        "stderr": captured_stderr.getvalue(),
        "args_after": args_after,
    }
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
