import hashlib
import json

from grader.holdout_manifest import verify_holdout_manifest


def test_holdout_manifest_verifier_checks_hashes_permissions_and_symlinks(tmp_path) -> None:
    root = tmp_path / "holdout"
    fixture = root / "sealed"
    fixture.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    source = fixture / "main.py"
    source.write_bytes(b"VALUE = 1\n")
    source.chmod(0o600)
    digest = hashlib.sha256()
    digest.update(b"main.py")
    digest.update(b"\0")
    digest.update(b"VALUE = 1\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fixture_count": 1,
                "fixture_directory_sha256": [digest.hexdigest()],
            }
        )
    )

    assert verify_holdout_manifest(root, manifest) == {
        "schema_version": 1,
        "fixture_count": 1,
        "schema_ok": True,
        "aggregate_match": True,
        "permissions_ok": True,
        "symlink_free": True,
        "verified": True,
    }

    source.write_bytes(b"VALUE = 2\n")
    source.chmod(0o600)
    assert not verify_holdout_manifest(root, manifest)["verified"]
