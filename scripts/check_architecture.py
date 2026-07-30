#!/usr/bin/env python3
"""Fail CI when known monoliths grow or core dependency boundaries regress."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Existing debt is frozen at a narrow ceiling. New work must move into focused
# modules instead of extending these composition roots and legacy bundles.
LEGACY_LINE_BUDGETS = {
    "backend/tasks.py": 11000,
    "backend/main.py": 4450,
    "backend/database.py": 4380,
    "static/index.html": 7510,
    "tests/test_regressions.py": 20350,
}

FORBIDDEN_IMPORTS = {
    "backend/database.py": {
        "backend.main",
        "backend.job_queue",
        "backend.tasks",
    },
    "backend/schema_migrations.py": {
        "backend.database",
        "backend.main",
    },
    "backend/auth.py": {
        "backend.main",
    },
    "backend/maintenance.py": {
        "backend.main",
    },
}

FUNCTION_LINE_BUDGETS = {
    ("backend/tasks.py", "process_audio_task"): 2050,
    ("backend/tasks.py", "_transcribe_segment_with_recovery"): 750,
    ("backend/main.py", "get_meeting_detail"): 200,
    ("backend/main.py", "rerun_meeting_record"): 190,
    ("backend/database.py", "apply_quality_preview_fields"): 370,
}


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _function_lines(path: Path) -> dict[str, int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name: int(node.end_lineno - node.lineno + 1)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.end_lineno is not None
    }


def _backend_dependency_cycles() -> list[tuple[str, ...]]:
    backend_dir = ROOT / "backend"
    modules = {
        f"backend.{path.stem}": path
        for path in backend_dir.glob("*.py")
        if path.name != "__init__.py"
    }
    graph: dict[str, set[str]] = {module: set() for module in modules}
    for module, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in modules:
                        graph[module].add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module in modules:
                    graph[module].add(node.module)
                elif node.module == "backend":
                    for alias in node.names:
                        dependency = f"backend.{alias.name}"
                        if dependency in modules:
                            graph[module].add(dependency)

    cycles: set[tuple[str, ...]] = set()

    def visit(start: str, current: str, path: list[str]) -> None:
        for dependency in graph[current]:
            if dependency == start and len(path) > 1:
                rotations = [
                    tuple(path[index:] + path[:index])
                    for index in range(len(path))
                ]
                cycles.add(min(rotations))
            elif dependency not in path:
                visit(start, dependency, [*path, dependency])

    for module in graph:
        visit(module, module, [module])
    return sorted(cycles)


def main() -> int:
    failures: list[str] = []
    for relative, maximum in LEGACY_LINE_BUDGETS.items():
        path = ROOT / relative
        actual = _line_count(path)
        if actual > maximum:
            failures.append(
                f"{relative}: {actual} lines exceeds frozen budget {maximum}"
            )

    for relative, forbidden in FORBIDDEN_IMPORTS.items():
        path = ROOT / relative
        imported = _imports(path)
        violations = sorted(
            dependency
            for dependency in forbidden
            if dependency in imported
        )
        if violations:
            failures.append(
                f"{relative}: forbidden imports {', '.join(violations)}"
            )

    for (relative, function_name), maximum in FUNCTION_LINE_BUDGETS.items():
        functions = _function_lines(ROOT / relative)
        actual = functions.get(function_name)
        if actual is None:
            failures.append(f"{relative}: missing guarded function {function_name}")
        elif actual > maximum:
            failures.append(
                f"{relative}:{function_name}: {actual} lines exceeds budget {maximum}"
            )

    for cycle in _backend_dependency_cycles():
        failures.append("backend dependency cycle: " + " -> ".join([*cycle, cycle[0]]))

    if failures:
        print("Architecture guard failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Architecture guard passed.")
    for relative, maximum in LEGACY_LINE_BUDGETS.items():
        actual = _line_count(ROOT / relative)
        print(f"- {relative}: {actual}/{maximum}")
    print(f"- guarded function sizes: {len(FUNCTION_LINE_BUDGETS)}")
    print("- backend dependency cycles: 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
