import argparse
import json
from pathlib import Path

from grader.benchmark import BenchmarkFixture, BenchmarkRunner
from grader.contracts import SuiteDraft
from grader.executor import LocalSuiteExecutor


def run(root: Path, *, details: bool = False) -> dict:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("benchmark root must be a real directory")
    runner = BenchmarkRunner(LocalSuiteExecutor(timeout_seconds=2))
    results = []
    for candidate in sorted(root.iterdir(), key=lambda path: path.name):
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        fixture = BenchmarkFixture.load(candidate)
        suite_path = candidate / "suite.json"
        suite = SuiteDraft.from_cli_output(suite_path.read_text(encoding="utf-8"))
        result = runner.evaluate(fixture, suite)
        item = {
            "release_passed": result.release_passed,
            "reference_pass_rate": result.reference_pass_rate,
            "starter_failed": result.starter_failed,
            "mutation_score": result.mutation_score,
            "deterministic": result.deterministic,
        }
        if details:
            item["fixture_id"] = result.fixture_id
        results.append(item)
    if not results:
        raise ValueError("benchmark root has no fixtures")
    return {
        "schema_version": 1,
        "fixture_count": len(results),
        "passed_count": sum(item["release_passed"] for item in results),
        "release_passed": all(item["release_passed"] for item in results),
        "results": results if details else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run frozen hidden-grader benchmarks")
    parser.add_argument("root", type=Path)
    parser.add_argument("--details", action="store_true")
    arguments = parser.parse_args()
    try:
        result = run(arguments.root, details=arguments.details)
    except (OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["release_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
