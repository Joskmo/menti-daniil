import base64
import json
import os
import re
import stat
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

_MAX_RESPONSE_BYTES = 1_000_000
_TOKEN = re.compile(r"[A-Za-z0-9_]{20,300}")


class GitHubAppError(RuntimeError):
    pass


class InstallationTokenProvider(Protocol):
    def token(self) -> str: ...


class StaticGitHubTokenProvider:
    """Token provider for a pre-existing GitHub OAuth/PAT credential."""

    def __init__(self, token: str) -> None:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("GitHub token is invalid")
        self._token = token

    def token(self) -> str:
        return self._token


class GitHubAppTokenProvider:
    def __init__(
        self,
        *,
        app_id: int,
        installation_id: int,
        private_key_path: str | Path,
        opener=urllib.request.urlopen,
        signer=None,
        clock=time.time,
    ) -> None:
        if (
            isinstance(app_id, bool)
            or not isinstance(app_id, int)
            or app_id <= 0
            or isinstance(installation_id, bool)
            or not isinstance(installation_id, int)
            or installation_id <= 0
        ):
            raise ValueError("GitHub App identity is invalid")
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key_path = Path(private_key_path)
        self._validate_key()
        self.opener = opener
        self.signer = signer or _openssl_sign
        self.clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        now = float(self.clock())
        if self._token is not None and now < self._expires_at - 300:
            return self._token
        jwt = self._jwt(int(now))
        request = urllib.request.Request(
            (
                "https://api.github.com/app/installations/"
                f"{self.installation_id}/access_tokens"
            ),
            data=b"{}",
            method="POST",
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "menti-hidden-grader",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        envelope = _open_json(self.opener, request)
        token = envelope.get("token") if isinstance(envelope, dict) else None
        expires = envelope.get("expires_at") if isinstance(envelope, dict) else None
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise GitHubAppError("GitHub App returned an invalid installation token")
        if not isinstance(expires, str):
            raise GitHubAppError("GitHub App returned an invalid token expiry")
        try:
            expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
        except ValueError as error:
            raise GitHubAppError("GitHub App returned an invalid token expiry") from error
        if expires_at <= now + 300:
            raise GitHubAppError("GitHub App returned a token with insufficient lifetime")
        self._token = token
        self._expires_at = expires_at
        return token

    def _jwt(self, now: int) -> str:
        header_json = json.dumps(
            {"alg": "RS256", "typ": "JWT"},
            separators=(",", ":"),
        ).encode()
        header = _base64url(header_json)
        payload = _base64url(
            json.dumps(
                {"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)},
                separators=(",", ":"),
            ).encode()
        )
        signing_input = f"{header}.{payload}".encode("ascii")
        try:
            signature = self.signer(self.private_key_path, signing_input)
        except (OSError, subprocess.SubprocessError) as error:
            raise GitHubAppError("GitHub App JWT signing failed") from error
        if not isinstance(signature, bytes) or not signature:
            raise GitHubAppError("GitHub App JWT signer returned an invalid signature")
        return f"{header}.{payload}.{_base64url(signature)}"

    def _validate_key(self) -> None:
        metadata = self.private_key_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("GitHub App private key must be an owned 0600 file")


class GitHubHttpChecksTransport:
    def __init__(
        self,
        tokens: InstallationTokenProvider,
        *,
        opener=urllib.request.urlopen,
    ) -> None:
        self.tokens = tokens
        self.opener = opener

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if method not in {"GET", "POST", "PATCH"}:
            raise ValueError("unsupported GitHub method")
        if (
            not isinstance(path, str)
            or not path.startswith("/repos/")
            or "://" in path
            or ".." in path
            or len(path) > 2_000
        ):
            raise ValueError("unsafe GitHub API path")
        body = None
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            if len(body) > 100_000:
                raise ValueError("GitHub payload is too large")
        request = urllib.request.Request(
            f"https://api.github.com{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.tokens.token()}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "menti-hidden-grader",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        result = _open_json(self.opener, request)
        if not isinstance(result, dict):
            raise GitHubAppError("GitHub API returned an invalid object")
        return result


def _openssl_sign(private_key_path: Path, value: bytes) -> bytes:
    environment = {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    return subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(private_key_path)],
        input=value,
        capture_output=True,
        check=True,
        timeout=10,
        env=environment,
    ).stdout


def _open_json(opener, request: urllib.request.Request) -> Any:
    try:
        with opener(request, timeout=20) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        raise GitHubAppError("GitHub API request failed") from error
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise GitHubAppError("GitHub API response is too large")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GitHubAppError("GitHub API returned invalid JSON") from error


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
