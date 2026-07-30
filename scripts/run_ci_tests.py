#!/usr/bin/env python3
"""Run the unittest suite and expose failures as GitHub Check annotations."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import traceback
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The suite explicitly enables RBAC in focused tests. Keep general endpoint
# fixtures independent from a developer machine's live .env configuration.
os.environ["MEETING_AUTH_ENABLED"] = "0"
os.environ["MEETING_OFFSITE_BACKUP_DIR"] = ""

TEST_ATTACHMENT_SHA256 = (
    "ea80334363eed145dfeee51ebae7dc3f1cd7d0c7879f8bfd2070c061d3c33f56"
)


def _leaked_test_attachments() -> set[Path]:
    """Find the exact 9-byte fixture that once leaked into live attachments."""
    attachment_root = ROOT / "output" / "attachments"
    if not attachment_root.is_dir():
        return set()
    matches: set[Path] = set()
    for path in attachment_root.rglob("quote*.png"):
        if not path.is_file() or path.stat().st_size != 9:
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() == TEST_ATTACHMENT_SHA256:
            matches.add(path.resolve())
    return matches


def _workflow_escape(value: str) -> str:
    return (
        str(value)
        .replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


class AnnotatingTestResult(unittest.TextTestResult):
    def _emit_annotation(
        self,
        test: unittest.case.TestCase,
        error: tuple[type[BaseException], BaseException, object],
        *,
        title: str,
    ) -> None:
        if os.getenv("GITHUB_ACTIONS", "").strip().lower() != "true":
            return

        frames = traceback.extract_tb(error[2])
        location = ""
        for frame in reversed(frames):
            path = Path(frame.filename).resolve()
            try:
                relative_path = path.relative_to(ROOT).as_posix()
            except ValueError:
                continue
            location = f"file={relative_path},line={max(1, int(frame.lineno))},"
            break

        detail = self._exc_info_to_string(error, test)
        message = f"{test.id()}\n{detail}"[-12000:]
        print(
            f"::error {location}title={_workflow_escape(title)}::"
            f"{_workflow_escape(message)}",
            flush=True,
        )

    def addFailure(self, test, err):  # noqa: N802 - unittest API
        super().addFailure(test, err)
        self._emit_annotation(test, err, title="Unit test failure")

    def addError(self, test, err):  # noqa: N802 - unittest API
        super().addError(test, err)
        self._emit_annotation(test, err, title="Unit test error")

    def addUnexpectedSuccess(self, test):  # noqa: N802 - unittest API
        super().addUnexpectedSuccess(test)
        if os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true":
            print(
                f"::error title=Unexpected test success::{_workflow_escape(test.id())}",
                flush=True,
            )


def main() -> int:
    leaked_before = _leaked_test_attachments()
    with tempfile.TemporaryDirectory(prefix="meeting-assistant-ci-") as tmpdir:
        isolated_db_path = Path(tmpdir) / "meetings.db"
        os.environ["DB_PATH"] = str(isolated_db_path)

        # The CI checkout intentionally has no live database. Initialize a
        # disposable schema so tests cannot depend on a developer machine's
        # meetings.db or on another test's import/order side effects.
        from backend import database

        database.DB_PATH = isolated_db_path
        database.init_db()

        suite = unittest.defaultTestLoader.discover(str(ROOT))
        result = unittest.TextTestRunner(
            verbosity=2,
            resultclass=AnnotatingTestResult,
        ).run(suite)
    leaked_after = _leaked_test_attachments()
    newly_leaked = sorted(leaked_after - leaked_before)
    if newly_leaked:
        detail = "\n".join(str(path.relative_to(ROOT)) for path in newly_leaked)
        print(
            "Workspace isolation failed: tests wrote fixture attachments into "
            f"the live output directory:\n{detail}",
            file=sys.stderr,
        )
        if os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true":
            print(
                "::error title=Workspace isolation failure::"
                f"{_workflow_escape(detail)}",
                flush=True,
            )
    return 0 if result.wasSuccessful() and not newly_leaked else 1


if __name__ == "__main__":
    sys.exit(main())
