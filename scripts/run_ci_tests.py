#!/usr/bin/env python3
"""Run the unittest suite and expose failures as GitHub Check annotations."""

from __future__ import annotations

import os
import sys
import traceback
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
    suite = unittest.defaultTestLoader.discover(str(ROOT))
    result = unittest.TextTestRunner(
        verbosity=2,
        resultclass=AnnotatingTestResult,
    ).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
