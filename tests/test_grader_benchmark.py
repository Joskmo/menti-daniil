import json
from pathlib import Path

from grader.benchmark import BenchmarkFixture, BenchmarkRunner
from grader.benchmark_agent_cli import _context
from grader.contracts import SuiteDraft
from grader.executor import LocalSuiteExecutor


def _write(root: Path, relative: str, content: str) -> None:
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content)


def _fixture(tmp_path: Path, *, expected_status: str = "ready") -> BenchmarkFixture:
    root = tmp_path / "fixture"
    metadata = {
        "schema_version": 1,
        "id": "next-id",
        "tier": "development",
        "task_id": "BENCH-001",
        "project": "json",
        "title": "Следующий ID",
        "description": "Вернуть максимальный числовой id плюс один, для пустого списка — 1.",
        "expected_status": expected_status,
        "minimum_mutation_score": 1.0,
    }
    _write(root, "fixture.json", json.dumps(metadata))
    _write(
        root,
        "starter/main.py",
        "def next_id(rows):\n    return rows[-1]['id'] + 1\n",
    )
    if expected_status == "ready":
        _write(
            root,
            "references/generator/main.py",
            "def next_id(rows):\n    return max((r['id'] for r in rows), default=0) + 1\n",
        )
        _write(
            root,
            "references/sorted/main.py",
            (
                "def next_id(rows):\n"
                "    ids = sorted(r['id'] for r in rows)\n"
                "    return ids[-1] + 1 if ids else 1\n"
            ),
        )
        _write(
            root,
            "mutants/length/main.py",
            "def next_id(rows):\n    return len(rows) + 1\n",
        )
    return BenchmarkFixture.load(root)


def _ready_suite() -> SuiteDraft:
    return SuiteDraft.from_cli_output(
        json.dumps(
            {
                "schema_version": 1,
                "status": "ready",
                "summary": "Проверяет ID.",
                "clarification": None,
                "rubric": [{"id": "next-id", "description": "ID.", "weight": 1}],
                "cases": [
                    {
                        "id": "unsorted",
                        "rubric_id": "next-id",
                        "adapter": "python_call",
                        "target": "main:next_id",
                        "input": {
                            "args": [[{"id": 8}, {"id": 2}]],
                            "kwargs": {},
                            "files": [],
                        },
                        "expect": {
                            "return": 9,
                            "exception": None,
                            "stdout": None,
                            "files": [],
                        },
                    },
                    {
                        "id": "empty",
                        "rubric_id": "next-id",
                        "adapter": "python_call",
                        "target": "main:next_id",
                        "input": {"args": [[]], "kwargs": {}, "files": []},
                        "expect": {
                            "return": 1,
                            "exception": None,
                            "stdout": None,
                            "files": [],
                        },
                    },
                ],
            }
        )
    )


def _clarification_suite() -> SuiteDraft:
    return SuiteDraft.from_cli_output(
        json.dumps(
            {
                "schema_version": 1,
                "status": "clarification_required",
                "summary": "Не определено пустое значение.",
                "clarification": "Что вернуть для пустого списка?",
                "rubric": [],
                "cases": [],
            }
        )
    )


def test_benchmark_agent_context_maps_fixture_id_to_valid_isolated_task_id(tmp_path) -> None:
    fixture = _fixture(tmp_path)

    context = _context(fixture)

    assert context.task_id == "PY-900001"
    assert fixture.task_id == "BENCH-001"


def test_benchmark_accepts_references_and_kills_starter_and_mutants(tmp_path) -> None:
    result = BenchmarkRunner(LocalSuiteExecutor(timeout_seconds=2)).evaluate(
        _fixture(tmp_path),
        _ready_suite(),
    )

    assert result.release_passed is True
    assert result.reference_pass_rate == 1.0
    assert result.starter_failed is True
    assert result.mutation_score == 1.0
    assert result.deterministic is True


def test_benchmark_accepts_required_clarification_without_execution(tmp_path) -> None:
    result = BenchmarkRunner(LocalSuiteExecutor(timeout_seconds=2)).evaluate(
        _fixture(tmp_path, expected_status="clarification_required"),
        _clarification_suite(),
    )

    assert result.release_passed is True
    assert result.status_match is True
