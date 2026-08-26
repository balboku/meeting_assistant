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
    def test_startup_opens_isolated_chrome_with_audio_defaults_for_recording(self) -> None:
        launched = mock.Mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            chrome_path = tmp_path / "chrome.exe"
            profile_path = tmp_path / "recording-profile"
            chrome_path.touch()
            with mock.patch.object(start.platform, "system", return_value="Windows"), \
                 mock.patch.dict(start.os.environ, {
                     "MEETING_ASSISTANT_FORCE_SCREEN_AUDIO_DEFAULT": "1",
                     "MEETING_ASSISTANT_CHROME_PATH": str(chrome_path),
                     "MEETING_ASSISTANT_CHROME_PROFILE_DIR": str(profile_path),
                 }, clear=False), \
                 mock.patch("builtins.print"):
                opened = start.open_recording_chrome(
                    "http://127.0.0.1:8001/history",
                    process_factory=launched,
                )

        self.assertTrue(opened)
        command = launched.call_args.args[0]
        self.assertEqual(command[0], str(chrome_path))
        self.assertIn(f"--user-data-dir={profile_path}", command)
        self.assertIn("--enable-features=GetDisplayMediaAudioSelection", command)
        self.assertIn("--system-audio-capture-default_checked", command)
        self.assertEqual(command[-2:], ["--new-window", "http://127.0.0.1:8001/history"])

    def test_startup_falls_back_to_default_browser_when_recording_chrome_is_unavailable(self) -> None:
        with mock.patch.object(start.time, "sleep"), \
             mock.patch.object(start, "open_recording_chrome", return_value=False), \
             mock.patch.object(start.webbrowser, "open") as browser_open, \
             mock.patch("builtins.print"):
            start.open_browser()

        browser_open.assert_called_once_with("http://127.0.0.1:8001/history")

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

if __name__ == "__main__":
    unittest.main()
