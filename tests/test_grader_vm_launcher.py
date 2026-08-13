import os

import pytest

from grader.vm_launcher import (
    LauncherProtocolError,
    UnixVmLauncherClient,
    UnixVmLauncherServer,
    validate_launcher_request,
)


def _execution() -> dict:
    return {
        "version": 1,
        "request_id": "a" * 32,
        "adapter": "python_call",
        "target": "main:double",
        "input": {"args": [4], "kwargs": {}, "files": []},
        "observe_files": [],
    }


def _response() -> dict:
    return {
        "version": 1,
        "request_id": "a" * 32,
        "status": "ok",
        "observation": {
            "return": 8,
            "exception": None,
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "files": [],
        },
    }


def test_launcher_request_rejects_expected_values() -> None:
    execution = {**_execution(), "expect": {"return": 8}}
    request = {
        "version": 1,
        "request_id": "a" * 32,
        "source_files": [{"path": "main.py", "content": "def double(x): return x * 2\n"}],
        "execution": execution,
    }

    with pytest.raises(LauncherProtocolError, match="execution fields"):
        validate_launcher_request(request)


def test_launcher_client_serializes_source_and_round_trips_one_execution(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("def double(x): return x * 2\n")
    (source / "data.csv").write_text("value\n4\n")
    captured = []

    def backend(source_files: tuple[dict, ...], execution: dict) -> dict:
        captured.append((source_files, execution))
        return _response()

    socket_path = tmp_path / "launcher.sock"
    with UnixVmLauncherServer(
        socket_path,
        backend=backend,
        allowed_uids={os.getuid()},
    ) as server:
        import threading

        thread = threading.Thread(target=server.handle_request)
        thread.start()
        result = UnixVmLauncherClient(socket_path, timeout_seconds=2).execute(
            source,
            _execution(),
        )
        thread.join(timeout=2)

    assert result == _response()
    assert captured == [
        (
            (
                {"path": "data.csv", "content": "value\n4\n"},
                {"path": "main.py", "content": "def double(x): return x * 2\n"},
            ),
            _execution(),
        )
    ]
    assert not socket_path.exists()


def test_launcher_client_rejects_source_symlink(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = True\n")
    (source / "linked.py").symlink_to(outside)

    with pytest.raises(LauncherProtocolError, match="symlink"):
        UnixVmLauncherClient(tmp_path / "missing.sock").execute(source, _execution())
