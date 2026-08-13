import base64
import io
import json

import pytest

from grader.github_app import (
    GitHubAppTokenProvider,
    GitHubHttpChecksTransport,
    StaticGitHubTokenProvider,
)


class FakeResponse:
    def __init__(self, payload) -> None:
        self.stream = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def read(self, size: int) -> bytes:
        return self.stream.read(size)


class FakeOpener:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        return FakeResponse(self.responses.pop(0))


def _decode_part(value: str):
    padding = "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(value + padding))


def test_github_app_provider_builds_short_jwt_and_caches_installation_token(tmp_path) -> None:
    key = tmp_path / "app.pem"
    key.write_text("synthetic private key")
    key.chmod(0o600)
    signed = []
    opener = FakeOpener(
        [
            {
                "token": "ghs_synthetic_installation_token",
                "expires_at": "2026-08-11T18:00:00Z",
            }
        ]
    )
    provider = GitHubAppTokenProvider(
        app_id=123,
        installation_id=456,
        private_key_path=key,
        opener=opener,
        signer=lambda path, value: signed.append((path, value)) or b"signature",
        clock=lambda: 1_786_469_400,
    )

    assert provider.token() == "ghs_synthetic_installation_token"
    assert provider.token() == "ghs_synthetic_installation_token"

    assert len(opener.requests) == 1
    request, timeout = opener.requests[0]
    jwt = request.headers["Authorization"].removeprefix("Bearer ")
    header, payload, _ = jwt.split(".")
    assert _decode_part(header) == {"alg": "RS256", "typ": "JWT"}
    assert _decode_part(payload) == {"iat": 1_786_469_340, "exp": 1_786_469_940, "iss": "123"}
    assert signed[0][0] == key
    assert timeout == 20


def test_github_checks_transport_uses_installation_token_and_bounded_json() -> None:
    class Tokens:
        def token(self):
            return "ghs_synthetic_installation_token"

    opener = FakeOpener([{"check_runs": []}])
    transport = GitHubHttpChecksTransport(Tokens(), opener=opener)

    assert transport.request("GET", "/repos/Joskmo/menti-daniil/check-runs") == {
        "check_runs": []
    }

    request, timeout = opener.requests[0]
    assert request.headers["Authorization"] == "Bearer ghs_synthetic_installation_token"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert timeout == 20


def test_static_github_token_provider_supports_existing_oauth_token() -> None:
    provider = StaticGitHubTokenProvider("gho_synthetic_existing_oauth_token")

    assert provider.token() == "gho_synthetic_existing_oauth_token"

    with pytest.raises(ValueError, match="invalid"):
        StaticGitHubTokenProvider("short")
