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

    if failures:
        print("Architecture guard failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Architecture guard passed.")
    for relative, maximum in LEGACY_LINE_BUDGETS.items():
        actual = _line_count(ROOT / relative)
        print(f"- {relative}: {actual}/{maximum}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
