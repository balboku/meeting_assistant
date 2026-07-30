from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import start


class _FakeProcess:
    def __init__(self, return_code: int):
        self.return_code = return_code
        self.terminated = False

    def wait(self, timeout=None) -> int:
        return self.return_code

    def poll(self) -> int:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


class StartSupervisorTests(unittest.TestCase):
    def test_supervisor_restarts_failed_child_with_bounded_backoff(self) -> None:
        return_codes = iter([1, 0])
        launches: list[list[str]] = []
        delays: list[int] = []

        def factory(command):
            launches.append(list(command))
            return _FakeProcess(next(return_codes))

        with mock.patch.dict(
            start.os.environ,
            {
                "MEETING_ASSISTANT_AUTO_RESTART": "1",
                "MEETING_ASSISTANT_MAX_RESTARTS": "3",
                "MEETING_ASSISTANT_RESTART_DELAY_SECONDS": "2",
            },
        ):
            result = start.run_server_supervisor(
                ["python", "-m", "uvicorn"],
                process_factory=factory,
                sleep=delays.append,
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(launches), 2)
        self.assertEqual(delays, [2])

    def test_supervisor_does_not_restart_after_token_ownership_changes(self) -> None:
        ownership = iter([True, False])
        launches = 0

        def factory(_command):
            nonlocal launches
            launches += 1
            return _FakeProcess(1)

        result = start.run_server_supervisor(
            ["uvicorn"],
            owns_supervisor=lambda: next(ownership, False),
            process_factory=factory,
            sleep=lambda _delay: None,
        )

        self.assertEqual(result, 1)
        self.assertEqual(launches, 1)

    def test_running_child_is_terminated_when_new_supervisor_takes_ownership(self) -> None:
        ownership = iter([True, True, False])
        process = _FakeProcess(0)
        process.poll = mock.Mock(return_value=None)

        result = start.run_server_supervisor(
            ["uvicorn"],
            owns_supervisor=lambda: next(ownership, False),
            process_factory=lambda _command: process,
            sleep=lambda _delay: None,
        )

        self.assertEqual(result, 0)
        self.assertTrue(process.terminated)

    def test_log_rotation_preserves_bounded_generations(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "ngrok.log"
            path.write_bytes(b"0123456789")
            (Path(tmpdir) / "ngrok.log.1").write_bytes(b"older")

            rotated = start._rotate_log_file(path, max_bytes=5, keep=2)

            self.assertTrue(rotated)
            self.assertFalse(path.exists())
            self.assertEqual((Path(tmpdir) / "ngrok.log.1").read_bytes(), b"0123456789")
            self.assertEqual((Path(tmpdir) / "ngrok.log.2").read_bytes(), b"older")


if __name__ == "__main__":
    unittest.main()
