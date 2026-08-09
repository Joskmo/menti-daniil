from pathlib import Path


def test_required_repository_files_exist() -> None:
    required_paths = [
        Path("README.md"),
        Path("CONTRIBUTING.md"),
        Path("projects"),
        Path("pyproject.toml"),
    ]

    assert all(path.exists() for path in required_paths)
