import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from grader.contracts import SuiteDraft
from grader.executor import SuiteResult

_KEY = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_TASK = re.compile(r"BENCH-[0-9]{3,}")


@dataclass(frozen=True, slots=True)
class BenchmarkFixture:
    fixture_id: str
    tier: str
    task_id: str
    project: str
    title: str
    description: str
    expected_status: str
    minimum_mutation_score: float
    root: Path
    starter: Path
    references: tuple[Path, ...]
    mutants: tuple[Path, ...]

    @classmethod
    def load(cls, root: str | Path) -> "BenchmarkFixture":
        root = Path(root).resolve()
        if not root.is_dir() or root.is_symlink():
            raise ValueError("benchmark fixture must be a real directory")
        _reject_symlinks(root)
        metadata_path = root / "fixture.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("benchmark fixture metadata is invalid") from error
        expected_fields = {
            "schema_version",
            "id",
            "tier",
            "task_id",
            "project",
            "title",
            "description",
            "expected_status",
            "minimum_mutation_score",
        }
        if not isinstance(metadata, dict) or set(metadata) != expected_fields:
            raise ValueError("benchmark fixture metadata has unexpected fields")
        if metadata["schema_version"] != 1:
            raise ValueError("unsupported benchmark fixture schema")
        fixture_id = _key(metadata["id"], "fixture id")
        project = _key(metadata["project"], "project")
        tier = metadata["tier"]
        if tier not in {"development", "holdout"}:
            raise ValueError("fixture tier must be development or holdout")
        task_id = metadata["task_id"]
        if not isinstance(task_id, str) or not _TASK.fullmatch(task_id):
            raise ValueError("fixture task_id is invalid")
        title = _text(metadata["title"], "title", 200)
        description = _text(metadata["description"], "description", 20_000)
        expected_status = metadata["expected_status"]
        if expected_status not in {"ready", "clarification_required"}:
            raise ValueError("fixture expected_status is invalid")
        minimum = metadata["minimum_mutation_score"]
        if (
            isinstance(minimum, bool)
            or not isinstance(minimum, (int, float))
            or not math.isfinite(minimum)
            or not 0 <= minimum <= 1
        ):
            raise ValueError("fixture minimum_mutation_score is invalid")
        starter = _project_directory(root / "starter", "starter")
        references = _variant_directories(root / "references")
        mutants = _variant_directories(root / "mutants")
        if expected_status == "ready" and (len(references) < 2 or not mutants):
            raise ValueError("ready fixture requires two references and at least one mutant")
        if expected_status == "clarification_required" and (references or mutants):
            raise ValueError("clarification fixture must not contain evaluator variants")
        return cls(
            fixture_id,
            tier,
            task_id,
            project,
            title,
            description,
            expected_status,
            float(minimum),
            root,
            starter,
            references,
            mutants,
        )


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    fixture_id: str
    status_match: bool
    reference_pass_rate: float
    starter_failed: bool
    mutation_score: float
    deterministic: bool
    release_passed: bool


class BenchmarkExecutor(Protocol):
    def evaluate(self, suite: SuiteDraft, project_directory: str | Path) -> SuiteResult: ...


class BenchmarkRunner:
    def __init__(self, executor: BenchmarkExecutor) -> None:
        self.executor = executor

    def evaluate(self, fixture: BenchmarkFixture, suite: SuiteDraft) -> BenchmarkResult:
        status_match = suite.status == fixture.expected_status
        if not status_match:
            return BenchmarkResult(
                fixture.fixture_id,
                False,
                0.0,
                False,
                0.0,
                False,
                False,
            )
        if suite.status == "clarification_required":
            return BenchmarkResult(
                fixture.fixture_id,
                True,
                1.0,
                True,
                1.0,
                True,
                True,
            )

        deterministic = True

        def repeated(directory: Path) -> SuiteResult:
            nonlocal deterministic
            first = self.executor.evaluate(suite, directory)
            second = self.executor.evaluate(suite, directory)
            if first != second:
                deterministic = False
            return first

        reference_results = [repeated(directory) for directory in fixture.references]
        starter_result = repeated(fixture.starter)
        mutant_results = [repeated(directory) for directory in fixture.mutants]
        reference_pass_rate = sum(result.passed for result in reference_results) / len(
            reference_results
        )
        starter_failed = not starter_result.passed
        mutation_score = sum(not result.passed for result in mutant_results) / len(mutant_results)
        release_passed = (
            deterministic
            and reference_pass_rate == 1.0
            and starter_failed
            and mutation_score >= fixture.minimum_mutation_score
        )
        return BenchmarkResult(
            fixture.fixture_id,
            True,
            reference_pass_rate,
            starter_failed,
            mutation_score,
            deterministic,
            release_passed,
        )


def _variant_directories(root: Path) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("benchmark variants must be a real directory")
    variants: list[Path] = []
    for candidate in sorted(root.iterdir(), key=lambda path: path.name):
        if candidate.is_symlink() or not candidate.is_dir() or not _KEY.fullmatch(candidate.name):
            raise ValueError("benchmark variant has an invalid directory name")
        variants.append(_project_directory(candidate, "variant"))
    return tuple(variants)


def _project_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"benchmark {label} must be a real directory")
    if not any(candidate.is_file() for candidate in path.rglob("*")):
        raise ValueError(f"benchmark {label} is empty")
    return path


def _reject_symlinks(root: Path) -> None:
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            raise ValueError("benchmark fixtures must not contain symlinks")


def _key(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) > 64 or not _KEY.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase ASCII key")
    return value


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"benchmark {label} is invalid")
    return value.strip()
