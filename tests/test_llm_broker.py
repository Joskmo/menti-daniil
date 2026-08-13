import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from grader.llm_broker import (
    BrokerProtocolError,
    UnixLlmBrokerClient,
    UnixLlmBrokerServer,
    validate_broker_request,
)


def test_broker_request_contract_accepts_only_typed_bounded_payload() -> None:
    request = {
        "version": 1,
        "request_id": "a" * 32,
        "purpose": "test-author",
        "prompt": "Create one declarative suite.",
    }

    validated = validate_broker_request(request)

    assert validated == request


@pytest.mark.parametrize(
    "payload",
    [
        {
            "version": 1,
            "request_id": "a" * 32,
            "purpose": "shell",
            "prompt": "ignored",
        },
        {
            "version": 1,
            "request_id": "a" * 32,
            "purpose": "test-author",
            "prompt": "ignored",
            "tools": ["terminal"],
        },
        {
            "version": 1,
            "request_id": "../escape",
            "purpose": "test-author",
            "prompt": "ignored",
        },
    ],
)
def test_broker_request_contract_rejects_unknown_actions_and_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises(BrokerProtocolError):
        validate_broker_request(payload)


def test_unix_broker_round_trip_keeps_model_adapter_server_side(tmp_path: Path) -> None:
    socket_path = tmp_path / "broker.sock"
    received: list[tuple[str, str]] = []

    def complete(prompt: str, purpose: str) -> str:
        received.append((purpose, prompt))
        return json.dumps({"status": "ok"})

    server = UnixLlmBrokerServer(socket_path, complete=complete, allowed_uids={os.getuid()})
    socket_mode = socket_path.stat().st_mode & 0o777
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    try:
        output = UnixLlmBrokerClient(socket_path, timeout_seconds=2).complete(
            "untrusted assignment payload",
            purpose="test-author",
        )
    finally:
        thread.join(timeout=2)
        server.server_close()

    assert output == '{"status": "ok"}'
    assert received == [("test-author", "untrusted assignment payload")]
    assert socket_mode == 0o600


def test_broker_process_removes_socket_on_sigterm(tmp_path: Path) -> None:
    socket_path = tmp_path / "broker.sock"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "grader.llm_broker",
            "--socket",
            str(socket_path),
        ]
    )
    deadline = time.monotonic() + 2
    while not socket_path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert socket_path.exists()

    process.terminate()
    process.wait(timeout=2)

    assert process.returncode == 0
    assert not socket_path.exists()


def test_broker_returns_generic_error_without_echoing_prompt(tmp_path: Path) -> None:
    socket_path = tmp_path / "broker.sock"
    secret_prompt = "hidden-case-canary"

    def fail(prompt: str, purpose: str) -> str:
        raise RuntimeError(f"provider exploded while processing {prompt}")

    server = UnixLlmBrokerServer(socket_path, complete=fail, allowed_uids={os.getuid()})
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    try:
        with pytest.raises(BrokerProtocolError) as raised:
            UnixLlmBrokerClient(socket_path, timeout_seconds=2).complete(
                secret_prompt,
                purpose="test-critic",
            )
    finally:
        thread.join(timeout=2)
        server.server_close()

    assert secret_prompt not in str(raised.value)
