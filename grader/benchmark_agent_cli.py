import argparse
import json
import os
import stat
from dataclasses import replace
from pathlib import Path, PurePosixPath

from grader.author import AssignmentContext, HermesTestAuthor, SourceFile
from grader.benchmark import BenchmarkFixture, BenchmarkRunner
from grader.contracts import SuiteDraft
from grader.critic import HermesTestCritic
from grader.executor import LocalSuiteExecutor
from grader.llm_broker import UnixLlmBrokerClient

_ALLOWED_SOURCE_SUFFIXES = {".py", ".md", ".txt", ".toml", ".yaml", ".yml"}


def run(root: Path, output_root: Path, socket_path: Path) -> dict:
    _safe_directory(root, create=False)
    _safe_directory(output_root, create=True)
    broker = UnixLlmBrokerClient(socket_path)
    author = HermesTestAuthor(broker=broker)
    critic = HermesTestCritic(broker=broker)
    runner = BenchmarkRunner(LocalSuiteExecutor(timeout_seconds=2))
    reports = []
    for candidate in sorted(root.iterdir(), key=lambda path: path.name):
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        fixture = BenchmarkFixture.load(candidate)
        context = _context(fixture)
        feedback = None
        verdict_payload = None
        approved = False
        attempts = 0
        suite = None
        for attempt in range(1, 4):
            attempts = attempt
            suite = author.create(context, critic_feedback=feedback)
            if suite.status == "clarification_required":
                approved = True
                break
            verdict = critic.review(context, suite)
            verdict_payload = verdict.to_payload()
            if verdict.status == "approved":
                approved = True
                break
            if verdict.status == "clarification_required":
                suite = _clarification_suite(verdict.summary, verdict.clarification)
                approved = True
                break
            feedback = verdict_payload
        assert suite is not None
        benchmark_result = runner.evaluate(fixture, suite)
        if not approved:
            benchmark_result = replace(benchmark_result, release_passed=False)
        report = {
            "schema_version": 1,
            "fixture_id": fixture.fixture_id,
            "attempts": attempts,
            "agent_approved": approved,
            "benchmark": {
                "status_match": benchmark_result.status_match,
                "reference_pass_rate": benchmark_result.reference_pass_rate,
                "starter_failed": benchmark_result.starter_failed,
                "mutation_score": benchmark_result.mutation_score,
                "deterministic": benchmark_result.deterministic,
                "release_passed": benchmark_result.release_passed,
            },
            "suite": suite.to_payload(),
            "critic": verdict_payload,
        }
        _write_private_json(output_root / f"{fixture.fixture_id}.json", report)
        reports.append(report)
    if not reports:
        raise ValueError("benchmark root has no fixtures")
    aggregate = {
        "schema_version": 1,
        "fixture_count": len(reports),
        "passed_count": sum(item["benchmark"]["release_passed"] for item in reports),
        "release_passed": all(item["benchmark"]["release_passed"] for item in reports),
    }
    _write_private_json(output_root / "summary.json", aggregate)
    return aggregate


def _context(fixture: BenchmarkFixture) -> AssignmentContext:
    source_files = []
    for candidate in sorted(fixture.starter.rglob("*")):
        if candidate.is_symlink():
            raise ValueError("benchmark starter must not contain symlinks")
        if not candidate.is_file() or candidate.suffix.lower() not in _ALLOWED_SOURCE_SUFFIXES:
            continue
        relative = candidate.relative_to(fixture.starter).as_posix()
        path = PurePosixPath(relative)
        lowered = tuple(part.lower() for part in path.parts)
        if any(part.startswith(".") or part in {"test", "tests", "fixtures"} for part in lowered):
            continue
        if lowered[-1].startswith("test_") or lowered[-1].endswith("_test.py"):
            continue
        if candidate.stat().st_size > 64_000:
            continue
        source_files.append(SourceFile(relative, candidate.read_text(encoding="utf-8")))
    if not source_files:
        raise ValueError("benchmark starter has no author-visible source")
    benchmark_number = int(fixture.task_id.removeprefix("BENCH-"))
    return AssignmentContext(
        task_id=f"PY-9{benchmark_number:05d}",
        project=fixture.project,
        title=fixture.title,
        description=fixture.description,
        source_files=tuple(source_files),
    )


def _clarification_suite(summary: str, question: str | None) -> SuiteDraft:
    if question is None:
        raise ValueError("critic clarification verdict has no question")
    return SuiteDraft.from_cli_output(
        json.dumps(
            {
                "schema_version": 1,
                "status": "clarification_required",
                "summary": summary,
                "clarification": question,
                "rubric": [],
                "cases": [],
            },
            ensure_ascii=False,
        )
    )


def _safe_directory(path: Path, *, create: bool) -> None:
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("benchmark path must be a real directory")
    if path.stat().st_uid != os.getuid():
        raise ValueError("benchmark path must be owned by the current user")
    if create:
        os.chmod(path, 0o700)


def _write_private_json(path: Path, payload: dict) -> None:
    if path.exists():
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ValueError("refusing to replace unsafe benchmark report")
    data = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark broker-only TestAuthor and TestCritic")
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        result = run(arguments.root, arguments.output, arguments.socket)
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["release_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
