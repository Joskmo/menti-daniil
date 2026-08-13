import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

_SCHEMA_KEYS = {"schema_version", "fixture_count", "fixture_directory_sha256"}
_HEX = frozenset("0123456789abcdef")


def verify_holdout_manifest(root: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    root = Path(root)
    manifest_path = Path(manifest_path)
    manifest = _read_manifest(manifest_path)
    schema_ok = _valid_manifest(manifest)
    expected = manifest.get("fixture_directory_sha256") if schema_ok else []
    fixture_count = manifest.get("fixture_count") if schema_ok else 0

    symlink_free = True
    permissions_ok = True
    fixture_hashes: list[str] = []
    root_metadata = root.lstat()
    if root.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
        symlink_free = not root.is_symlink()
        permissions_ok = False
    else:
        permissions_ok = _owned_mode(root_metadata, 0o700)
        for fixture in sorted(root.iterdir(), key=lambda item: item.name):
            metadata = fixture.lstat()
            if fixture.is_symlink():
                symlink_free = False
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                permissions_ok = False
                continue
            digest, fixture_permissions, fixture_symlinks = _directory_digest(fixture)
            fixture_hashes.append(digest)
            permissions_ok = permissions_ok and fixture_permissions
            symlink_free = symlink_free and fixture_symlinks

    count_ok = isinstance(fixture_count, int) and not isinstance(fixture_count, bool)
    count_ok = count_ok and fixture_count == len(fixture_hashes)
    aggregate_match = schema_ok and sorted(expected) == sorted(fixture_hashes)
    verified = schema_ok and count_ok and aggregate_match and permissions_ok and symlink_free
    return {
        "schema_version": 1,
        "fixture_count": len(fixture_hashes),
        "schema_ok": schema_ok,
        "aggregate_match": aggregate_match,
        "permissions_ok": permissions_ok,
        "symlink_free": symlink_free,
        "verified": verified,
    }


def _read_manifest(path: Path) -> dict[str, Any]:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 100_000:
        raise ValueError("holdout manifest path is unsafe")
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("holdout manifest is invalid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("holdout manifest must be an object")
    return value


def _valid_manifest(value: dict[str, Any]) -> bool:
    hashes = value.get("fixture_directory_sha256")
    count = value.get("fixture_count")
    return (
        set(value) == _SCHEMA_KEYS
        and value.get("schema_version") == 1
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count >= 0
        and isinstance(hashes, list)
        and len(hashes) == count
        and all(
            isinstance(item, str)
            and len(item) == 64
            and all(character in _HEX for character in item)
            for item in hashes
        )
    )


def _directory_digest(root: Path) -> tuple[str, bool, bool]:
    permissions_ok = True
    symlink_free = True
    files: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        metadata = directory.lstat()
        permissions_ok = permissions_ok and _owned_mode(metadata, 0o700)
        for child in sorted(directory.iterdir(), key=lambda item: item.name, reverse=True):
            metadata = child.lstat()
            if child.is_symlink():
                symlink_free = False
            elif stat.S_ISDIR(metadata.st_mode):
                pending.append(child)
            elif stat.S_ISREG(metadata.st_mode):
                permissions_ok = permissions_ok and _owned_mode(metadata, 0o600)
                files.append(child)
            else:
                permissions_ok = False
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest(), permissions_ok, symlink_free


def _owned_mode(metadata: os.stat_result, mode: int) -> bool:
    return metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == mode
