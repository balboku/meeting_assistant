import asyncio
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest import mock

import httpx


ROOT = Path(__file__).resolve().parents[1]


def asgi_request(app, method: str, path: str, asgi_client=("127.0.0.1", 123), **kwargs):
    async def _request():
        transport = httpx.ASGITransport(app=app, client=asgi_client)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(_request())


class SecurityRegressionTests(unittest.TestCase):
    def test_remote_forwarded_requests_are_rejected_without_api_key(self):
        from backend.main import app

        response = asgi_request(app, "GET", "/health", headers={"X-Forwarded-For": "203.0.113.8"})

        self.assertEqual(response.status_code, 403)

    def test_remote_browser_api_key_query_sets_cookie_for_followup_requests(self):
        import backend.main as main

        original_key = main.APP_API_KEY
        main.APP_API_KEY = "mobile-secret"
        try:
            first_response = asgi_request(
                main.app,
                "GET",
                "/history?api_key=mobile-secret",
                headers={"X-Forwarded-For": "203.0.113.8"},
            )
            self.assertIn(first_response.status_code, (307, 308))
            self.assertIn(
                "meeting_assistant_api_key=mobile-secret",
                first_response.headers.get("set-cookie", ""),
            )

            followup_response = asgi_request(
                main.app,
                "GET",
                "/health",
                headers={"X-Forwarded-For": "203.0.113.8"},
                cookies={"meeting_assistant_api_key": "mobile-secret"},
            )
        finally:
            main.APP_API_KEY = original_key

        self.assertEqual(followup_response.status_code, 200)

    def test_same_network_browser_requests_are_allowed_without_api_key(self):
        from backend.main import app

        for client_ip in ("192.168.1.50", "10.0.0.8", "100.84.193.112"):
            response = asgi_request(
                app,
                "GET",
                "/health",
                headers={"X-Forwarded-For": client_ip},
            )
            self.assertEqual(response.status_code, 200, msg=client_ip)

    def test_forwarded_for_is_ignored_from_non_loopback_clients(self):
        from backend.main import app

        response = asgi_request(
            app,
            "GET",
            "/health",
            asgi_client=("198.51.100.9", 456),
            headers={"X-Forwarded-For": "192.168.1.50"},
        )

        self.assertEqual(response.status_code, 403)

    def test_direct_same_network_client_is_allowed_without_forwarded_header(self):
        from backend.main import app

        response = asgi_request(
            app,
            "GET",
            "/health",
            asgi_client=("192.168.1.50", 456),
        )

        self.assertEqual(response.status_code, 200)

    def test_upload_rejects_files_larger_than_configured_limit(self):
        import backend.main as main

        original_limit = main.MAX_UPLOAD_BYTES
        main.MAX_UPLOAD_BYTES = 10
        try:
            response = asgi_request(
                main.app,
                "POST",
                "/upload-audio",
                files={"file": ("large.mp3", BytesIO(b"0" * 12), "audio/mpeg")},
            )
        finally:
            main.MAX_UPLOAD_BYTES = original_limit

        self.assertEqual(response.status_code, 413)

    def test_markdown_rendering_is_sanitized_before_inner_html(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn("DOMPurify.sanitize", html)
        self.assertIn("function normalizeMeetingMarkdown", html)
        self.assertIn("DOMPurify.sanitize(marked.parse(normalizeMeetingMarkdown(md)))", html)

    def test_web_ui_uses_local_static_assets_without_cdn_dependencies(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

        self.assertNotIn("cdn.jsdelivr.net", html)
        self.assertNotIn("fonts.googleapis.com", html)
        self.assertNotIn("fonts.gstatic.com", html)
        self.assertIn('/static/vendor/marked.min.js', html)
        self.assertIn('/static/vendor/purify.min.js', html)
        self.assertTrue((ROOT / "static" / "vendor" / "marked.min.js").is_file())
        self.assertTrue((ROOT / "static" / "vendor" / "purify.min.js").is_file())


class ConfigRegressionTests(unittest.TestCase):
    def test_database_and_runtime_paths_can_be_overridden_by_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            env = {
                **os.environ,
                "DB_PATH": str(tmp_path / "custom.db"),
                "MEETING_TEMP_DIR": str(tmp_path / "custom-temp"),
                "MEETING_OUTPUT_DIR": str(tmp_path / "custom-output"),
                "MEETING_SOURCE_AUDIO_DIR": str(tmp_path / "custom-source-audio"),
            }
            script = (
                "import json; "
                "import backend.database as database; "
                "import backend.main as main; "
                "print(json.dumps({"
                "'db': str(database.DB_PATH), "
                "'temp': str(main.TEMP_DIR), "
                "'output': str(main.OUTPUT_DIR), "
                "'source_audio': str(main.SOURCE_AUDIO_DIR)"
                "}, ensure_ascii=False))"
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["db"], str(tmp_path / "custom.db"))
        self.assertEqual(payload["temp"], str(tmp_path / "custom-temp"))
        self.assertEqual(payload["output"], str(tmp_path / "custom-output"))
        self.assertEqual(payload["source_audio"], str(tmp_path / "custom-source-audio"))

    def test_config_endpoint_reports_runtime_upload_limit_and_formats(self):
        import backend.main as main

        original_mb = main.MAX_UPLOAD_MB
        original_bytes = main.MAX_UPLOAD_BYTES
        main.MAX_UPLOAD_MB = 123
        main.MAX_UPLOAD_BYTES = 123 * 1024 * 1024
        try:
            response = asgi_request(main.app, "GET", "/config")
        finally:
            main.MAX_UPLOAD_MB = original_mb
            main.MAX_UPLOAD_BYTES = original_bytes

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["max_upload_mb"], 123)
        self.assertEqual(payload["max_upload_bytes"], 123 * 1024 * 1024)
        self.assertEqual(payload["model"], main.GEMINI_MODEL)
        self.assertEqual(payload["transcription_model"], main.GEMINI_MODEL)
        self.assertEqual(payload["summary_model"], main.SUMMARY_MODEL)
        self.assertEqual(payload["summary_fallback_model"], main.SUMMARY_FALLBACK_MODEL)
        self.assertIn(".mp3", payload["supported_extensions"])
        self.assertIn(".mp4", payload["supported_extensions"])


class ProjectGovernanceRegressionTests(unittest.TestCase):
    def test_gitignore_excludes_runtime_artifacts(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

        for pattern in (
            "__pycache__/",
            "*.pyc",
            "meetings.db",
            "meetings.db-*",
            "temp/",
            "output/",
            "backups/",
            ".env",
        ):
            self.assertIn(pattern, gitignore)

    def test_verify_script_covers_core_local_checks(self):
        verify_script = ROOT / "scripts" / "verify.sh"

        self.assertTrue(verify_script.is_file())
        self.assertTrue(os.access(verify_script, os.X_OK))

        script = verify_script.read_text(encoding="utf-8")
        for command in (
            ".venv/bin/python -m unittest discover -v",
            ".venv/bin/python -m compileall -q backend gui tests meeting_assistant.py start.py test_regex.py test_gemini.py",
            ".venv/bin/python scripts/security_scan.py",
            ".venv/bin/python -m pip check",
            "node --check static/index.html",
        ):
            self.assertIn(command, script)

    def test_ci_runs_unit_tests_and_security_scan(self):
        ci = ROOT / ".github" / "workflows" / "ci.yml"
        security_scan = ROOT / "scripts" / "security_scan.py"

        self.assertTrue(ci.is_file())
        self.assertTrue(security_scan.is_file())

        workflow = ci.read_text(encoding="utf-8")
        self.assertIn("python scripts/security_scan.py", workflow)
        self.assertIn("python -m unittest discover -v", workflow)
        self.assertIn("python -m pip check", workflow)

    def test_docs_describe_operational_knobs_events_and_fts(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")

        for env_name in (
            "DB_PATH",
            "MEETING_TEMP_DIR",
            "MEETING_OUTPUT_DIR",
            "MEETING_SOURCE_AUDIO_DIR",
            "MEETING_BACKUP_DIR",
            "DB_BACKUP_KEEP",
            "JOB_RETENTION_DAYS",
            "MEETING_ASSISTANT_NGROK",
            "MEETING_ASSISTANT_NGROK_URL",
        ):
            self.assertIn(env_name, readme)

        self.assertIn("job_events", architecture)
        self.assertIn("FTS5", architecture)


class MaintenanceRegressionTests(unittest.TestCase):
    def test_backup_database_creates_timestamped_sqlite_copy_and_prunes_old_backups(self):
        from backend.maintenance import backup_database

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "meetings.db"
            backup_dir = tmp_path / "backups"
            db_path.write_bytes(b"sqlite-content")

            old_backup = backup_dir / "meetings_20250101_000000.db"
            backup_dir.mkdir()
            old_backup.write_bytes(b"old")

            created = backup_database(
                db_path=db_path,
                backup_dir=backup_dir,
                now=datetime(2026, 7, 5, 14, 0, 0),
                keep=1,
            )

            self.assertEqual(created.name, "meetings_20260705_140000.db")
            self.assertEqual(created.read_bytes(), b"sqlite-content")
            self.assertFalse(old_backup.exists())

    def test_maintain_database_runs_checkpoint_and_vacuum(self):
        from backend.maintenance import maintain_database

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "meetings.db"
            import sqlite3
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, name TEXT)")
                conn.execute("INSERT INTO sample (name) VALUES ('demo')")
                conn.commit()
            finally:
                conn.close()

            result = maintain_database(db_path=db_path)

            self.assertTrue(result["wal_checkpoint"])
            self.assertTrue(result["vacuum"])

    def test_lifespan_runs_database_maintenance_before_worker_start(self):
        source = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        lifespan_body = source[
            source.index("async def lifespan") :
            source.index("app = FastAPI(")
        ]

        self.assertIn("run_startup_maintenance", lifespan_body)
        self.assertLess(
            lifespan_body.index("run_startup_maintenance"),
            lifespan_body.index("job_worker.start()"),
        )


class StartupHealthRegressionTests(unittest.TestCase):
    def test_startup_health_reports_missing_api_key_and_unwritable_paths(self):
        from backend.maintenance import run_startup_health_checks

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            checks = run_startup_health_checks(
                temp_dir=tmp_path / "missing-temp",
                output_dir=tmp_path / "missing-output",
                static_vendor_dir=tmp_path / "missing-vendor",
                env={},
            )

        names = {check["name"]: check for check in checks}
        self.assertEqual(names["gemini_api_key"]["status"], "failed")
        self.assertEqual(names["temp_dir"]["status"], "failed")
        self.assertEqual(names["output_dir"]["status"], "failed")
        self.assertEqual(names["static_vendor"]["status"], "failed")

    def test_health_endpoint_includes_startup_checks(self):
        import backend.main as main

        response = asgi_request(main.app, "GET", "/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("checks", payload)
        self.assertTrue(any(check["name"] == "database" for check in payload["checks"]))


class MediaValidationRegressionTests(unittest.TestCase):
    def test_magic_sniff_accepts_mp3_wav_m4a_and_rejects_fake_mp3(self):
        from backend.media_validation import validate_media_magic

        self.assertIsNone(validate_media_magic(".mp3", b"ID3\x04\x00\x00\x00\x00\x00\x21audio"))
        self.assertIsNone(validate_media_magic(".wav", b"RIFF\x24\x00\x00\x00WAVEfmt "))
        self.assertIsNone(validate_media_magic(".m4a", b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00"))
        self.assertIn("檔案內容", validate_media_magic(".mp3", b"<html>not audio</html>"))

    def test_upload_rejects_extension_that_does_not_match_magic(self):
        import backend.main as main

        response = asgi_request(
            main.app,
            "POST",
            "/upload-audio",
            files={"file": ("fake.mp3", BytesIO(b"<html>not audio</html>"), "audio/mpeg")},
        )

        self.assertEqual(response.status_code, 415)


class TaskRegressionTests(unittest.TestCase):
    def test_audio_processing_task_is_sync_so_background_tasks_use_threadpool(self):
        from backend.tasks import process_audio_task

        self.assertFalse(inspect.iscoroutinefunction(process_audio_task))

    def test_backend_prompts_preserve_multilingual_transcript_policy(self):
        from backend import tasks

        main_prompt = tasks.MEETING_PROMPT
        segment_prompt = tasks._build_segment_prompt(0, 2)

        for prompt in (main_prompt, segment_prompt):
            self.assertIn("英文發言請保留英文原文", prompt)
            self.assertIn("中文國語發言請以繁體中文轉寫", prompt)
            self.assertIn("台語發言請標記為 `[台語]`", prompt)
            self.assertIn("摘要、決議與待辦事項仍統一使用繁體中文", prompt)

    def test_cli_prompt_preserves_multilingual_transcript_policy(self):
        import meeting_assistant

        prompt = meeting_assistant.build_meeting_prompt()

        self.assertIn("英文發言請保留英文原文", prompt)
        self.assertIn("中文國語發言請以繁體中文轉寫", prompt)
        self.assertIn("台語發言請標記為 `[台語]`", prompt)
        self.assertIn("摘要、決議與待辦事項仍統一使用繁體中文", prompt)

    def test_backend_prompts_require_anonymous_speaker_differentiation(self):
        from backend import tasks

        main_prompt = tasks.MEETING_PROMPT
        segment_prompt = tasks._build_segment_prompt(0, 2)

        for prompt in (main_prompt, segment_prompt):
            self.assertIn("目標是分辨「不同聲音」", prompt)
            self.assertIn("同一個聲音再次出現時必須沿用相同標籤", prompt)
            self.assertIn("不要把不同人的發言合併成同一位", prompt)
            self.assertIn("**[多人重疊]**", prompt)

    def test_cli_prompt_requires_anonymous_speaker_differentiation(self):
        import meeting_assistant

        prompt = meeting_assistant.build_meeting_prompt()

        self.assertIn("目標是分辨「不同聲音」", prompt)
        self.assertIn("同一個聲音再次出現時必須沿用相同標籤", prompt)
        self.assertIn("不要把不同人的發言合併成同一位", prompt)
        self.assertIn("若只能辨識到匿名發言者", prompt)

    def test_backend_prompts_use_medical_device_rnd_analysis_policy(self):
        from backend import tasks

        prompts = (
            tasks.MEETING_PROMPT,
            tasks._build_segment_prompt(0, 2),
            tasks._build_summary_prompt("佳世達 IEC 62304 SRS SDS traceability matrix"),
        )

        self.assertIn("「佳世達」為正確名稱", prompts[0])
        self.assertIn("請勿寫成「加斯達」、「嘉士達」或 Jasta", prompts[0])
        self.assertIn("IEC 62304", prompts[1])
        self.assertIn("討論摘要需依「專案/議題」分組", prompts[2])
        self.assertIn("最終決議只放已確認", prompts[2])
        self.assertIn("待辦事項只放可驗收行動", prompts[2])

    def test_cli_prompt_uses_medical_device_rnd_analysis_policy(self):
        import meeting_assistant

        prompt = meeting_assistant.build_meeting_prompt()

        self.assertIn("「佳世達」為正確名稱", prompt)
        self.assertIn("久方生技 / Maxima Biotech", prompt)
        self.assertIn("討論摘要需依「專案/議題」分組", prompt)
        self.assertIn("不要把追蹤目標、背景說明或教學內容列為決議", prompt)

    def test_domain_term_normalization_and_quality_notice(self):
        from backend.tasks import (
            _normalize_domain_terms,
            _prepend_transcript_quality_notice,
        )

        normalized = _normalize_domain_terms(
            "加斯達、嘉士達與 Jasta 文件提到 IEC 6304 與 IC6304，"
            "放電字句、平保、平寶、氣械老化、頻率政府與內型固定塊。"
        )

        self.assertIn("佳世達", normalized)
        self.assertNotIn("加斯達", normalized)
        self.assertNotIn("嘉士達", normalized)
        self.assertNotIn("Jasta", normalized)
        self.assertIn("佳世達", normalized)
        self.assertNotIn("IEC 6304", normalized)
        self.assertIn("IEC 62304", normalized)
        self.assertIn("放電治具", normalized)
        self.assertIn("品保", normalized)
        self.assertIn("機械老化", normalized)
        self.assertIn("頻率振幅", normalized)
        self.assertIn("內徑固定塊", normalized)
        self.assertNotIn("平保", normalized)
        self.assertNotIn("平寶", normalized)

        content = "## 📋 一、討論摘要 (Discussion Summary)\n摘要\n\n## ✅ 二、最終決議 (Final Decisions)\n決議"
        transcript = "[系統提示：此處音檔包含無意義雜訊，已自動過濾後續重複內容]"
        with_notice = _prepend_transcript_quality_notice(content, transcript)

        self.assertIn("逐字稿品質註記", with_notice)
        self.assertIn("可能缺漏", with_notice)

    def test_summary_json_response_converts_to_readable_markdown(self):
        from backend.tasks import _summary_response_to_markdown

        response_text = json.dumps(
            {
                "discussion_summary": [
                    {
                        "id": "D1",
                        "topic": "佳世達測試",
                        "context": "會議討論 SRS 與 SDS 文件缺口。",
                        "summary": "**確認** SRS 與 SDS traceability matrix 需要補齊。",
                        "key_points": ["SRS 需求未完整對應", "SDS 設計文件需同步更新"],
                        "impact": "影響後續驗證追蹤。",
                        "evidence_timecodes": ["12:30"],
                    },
                    {
                        "id": "D2",
                        "topic": "驗證時程",
                        "summary": "測試排程需等缺口清單完成後再確認。",
                        "evidence_timecodes": ["18:20"],
                    },
                ],
                "final_decisions": [
                    {
                        "id": "R1",
                        "related_discussions": ["D1"],
                        "decision": "下次會議前先整理缺口清單。",
                        "basis": "逐字稿提到先做清單再回來討論。",
                        "status": "confirmed",
                    }
                ],
                "action_items": [
                    {
                        "id": "A1",
                        "related_discussions": ["D1"],
                        "related_decisions": ["R1"],
                        "task": "整理佳世達需求缺口",
                        "owner": "QA",
                        "due": "2026/07/10",
                        "priority": "高",
                    }
                ],
            },
            ensure_ascii=False,
        )

        markdown = _summary_response_to_markdown(response_text)

        self.assertIn("## 一、討論摘要 (Discussion Summary)", markdown)
        self.assertIn("## 二、最終決議 (Final Decisions)", markdown)
        self.assertIn("## 三、待辦事項 (Action Items)", markdown)
        self.assertIn("### D1. 佳世達測試", markdown)
        self.assertIn("### D2. 驗證時程", markdown)
        self.assertIn("| # | 關聯討論 | 決議 | 依據 | 狀態 |", markdown)
        self.assertIn("| R1 | D1 | 下次會議前先整理缺口清單。 | 逐字稿提到先做清單再回來討論。 | confirmed |", markdown)
        self.assertIn("| # | 關聯討論 | 關聯決議 | 任務描述 | 負責人 | 期限 | 優先級 |", markdown)
        self.assertIn("| A1 | D1 | R1 | 整理佳世達需求缺口 | QA | 2026/07/10 | 高 |", markdown)
        self.assertNotIn("**", markdown)

    def test_segment_transcript_timestamps_are_offset_to_global_time(self):
        from backend.tasks import _offset_transcript_timestamps

        transcript = "[00:00] **[發言者 A]**：第二段開始。\n[09:59] **[發言者 B]**：第二段結束。"

        adjusted = _offset_transcript_timestamps(transcript, offset_seconds=600)

        self.assertIn("[10:00] **[發言者 A]**：第二段開始。", adjusted)
        self.assertIn("[19:59] **[發言者 B]**：第二段結束。", adjusted)
        self.assertNotIn("[00:00]", adjusted)

    def test_segment_transcript_quality_rejects_incomplete_nonterminal_segments(self):
        from backend.tasks import _segment_transcript_quality_issues

        incomplete = (
            "[40:00] **[發言者 A]**：只轉出段落開頭。\n"
            "[系統提示：此處音檔包含無意義雜訊，已自動過濾後續重複內容]"
        )
        complete = (
            "[40:00] **[發言者 A]**：段落開始。\n"
            "[48:30] **[發言者 B]**：段落接近結尾。"
        )

        issues = _segment_transcript_quality_issues(
            incomplete,
            segment_index=4,
            total_segments=10,
            segment_minutes=10,
        )

        self.assertTrue(any("自動過濾" in issue for issue in issues))
        self.assertTrue(any("未接近段尾" in issue for issue in issues))
        self.assertEqual(
            _segment_transcript_quality_issues(
                complete,
                segment_index=4,
                total_segments=10,
                segment_minutes=10,
            ),
            [],
        )

    def test_segment_transcript_cache_round_trips_and_rejects_mismatched_context(self):
        from backend.tasks import (
            _load_segment_transcript_cache,
            _save_segment_transcript_cache,
            _segment_cache_context,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "output"
            audio_path = root / "meeting.webm"
            audio_path.write_bytes(b"audio")

            context = _segment_cache_context(
                audio_path=audio_path,
                model="gemini-test",
                total_segments=2,
                segment_minutes=10,
            )
            _save_segment_transcript_cache(
                output_dir=output_dir,
                job_id="resume-job",
                segment_index=0,
                context=context,
                transcript="[00:00] **[發言者 A]**：第一段開始。\n[09:30] **[發言者 A]**：已完成第一段。",
            )

            cached = _load_segment_transcript_cache(output_dir, "resume-job", 0, context)
            self.assertIn("已完成第一段", cached)

            mismatched = dict(context)
            mismatched["model"] = "another-model"
            self.assertIsNone(_load_segment_transcript_cache(output_dir, "resume-job", 0, mismatched))

    def test_segment_transcript_cache_rejects_incomplete_cached_segment(self):
        from backend.tasks import (
            _load_segment_transcript_cache,
            _save_segment_transcript_cache,
            _segment_cache_context,
            _segment_cache_file,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "output"
            audio_path = root / "meeting.webm"
            audio_path.write_bytes(b"audio")

            context = _segment_cache_context(
                audio_path=audio_path,
                model="gemini-test",
                total_segments=2,
                segment_minutes=10,
            )
            _save_segment_transcript_cache(
                output_dir=output_dir,
                job_id="resume-job",
                segment_index=0,
                context=context,
                transcript=(
                    "[00:00] **[發言者 A]**：只有開頭。\n"
                    "[系統提示：此處音檔包含無意義雜訊，已自動過濾後續重複內容]"
                ),
            )

            self.assertIsNone(_load_segment_transcript_cache(output_dir, "resume-job", 0, context))
            self.assertFalse(_segment_cache_file(output_dir, "resume-job", 0).exists())

    def test_segmented_audio_task_reuses_cached_transcripts(self):
        import backend.tasks as tasks

        summary_content = (
            "## 📋 一、討論摘要 (Discussion Summary)\n"
            "摘要。\n\n"
            "---\n\n"
            "## ✅ 二、最終決議 (Final Decisions)\n"
            "尚未決定。\n\n"
            "---\n\n"
            "## 📌 三、待辦事項 (Action Items)\n\n"
            "| # | 任務描述 | 負責人 | 期限 | 優先級 |\n"
            "|---|---------|--------|------|--------|\n"
            "| 1 | 確認後續事項 | 發言者 A | 未提及 | 中 |\n"
        )

        class FakeModels:
            def generate_content(self, **kwargs):
                return type("Response", (), {"text": summary_content})()

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.models = FakeModels()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "output"
            output_dir.mkdir()
            audio_path = root / "meeting.webm"
            audio_path.write_bytes(b"audio")
            seg1 = root / "_seg_meeting_000.mp3"
            seg2 = root / "_seg_meeting_001.mp3"
            seg1.write_bytes(b"seg1")
            seg2.write_bytes(b"seg2")

            context = tasks._segment_cache_context(
                audio_path=audio_path,
                model="gemini-test",
                total_segments=2,
                segment_minutes=tasks.SEGMENT_MINUTES,
            )
            tasks._save_segment_transcript_cache(
                output_dir=output_dir,
                job_id="resume-job",
                segment_index=0,
                context=context,
                transcript="[00:00] **[發言者 A]**：快取第一段。\n[09:30] **[發言者 A]**：第一段結束。",
            )

            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                 mock.patch.object(tasks.genai, "Client", side_effect=FakeClient), \
                 mock.patch.object(tasks, "_split_audio_to_segments", return_value=[seg1, seg2]), \
                 mock.patch.object(tasks, "_transcribe_segment", return_value="[00:00] **[發言者 B]**：新轉錄第二段。") as transcribe_mock, \
                 mock.patch.object(tasks, "is_job_cancel_requested", return_value=False), \
                 mock.patch.object(tasks, "update_job_status"), \
                 mock.patch.object(tasks, "save_meeting"):
                output_path = tasks.process_audio_task(
                    job_id="resume-job",
                    audio_path=audio_path,
                    output_dir=output_dir,
                    model="gemini-test",
                )

            self.assertIsNotNone(output_path)
            transcribe_mock.assert_called_once()
            self.assertEqual(transcribe_mock.call_args.args[2], 1)
            content = output_path.read_text(encoding="utf-8")
            self.assertIn("快取第一段", content)
            self.assertIn("[10:00] **[發言者 B]**：新轉錄第二段。", content)

    def test_single_segment_audio_task_uses_dual_model_pipeline(self):
        import backend.tasks as tasks

        summary_content = (
            "## 📋 一、討論摘要 (Discussion Summary)\n"
            "單段音檔也使用摘要模型。\n\n"
            "---\n\n"
            "## ✅ 二、最終決議 (Final Decisions)\n"
            "尚未決定。\n\n"
            "---\n\n"
            "## 📌 三、待辦事項 (Action Items)\n\n"
            "| # | 任務描述 | 負責人 | 期限 | 優先級 |\n"
            "|---|---------|--------|------|--------|\n"
            "| 1 | 確認後續事項 | 發言者 A | 未提及 | 中 |\n"
        )

        class FakeModels:
            def __init__(self):
                self.calls = []

            def generate_content(self, **kwargs):
                self.calls.append(kwargs)
                return type("Response", (), {"text": summary_content})()

        class FakeClient:
            def __init__(self):
                self.models = FakeModels()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "output"
            output_dir.mkdir()
            audio_path = root / "meeting.mp3"
            audio_path.write_bytes(b"audio")

            fake_client = FakeClient()
            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                 mock.patch.object(tasks.genai, "Client", return_value=fake_client), \
                 mock.patch.object(tasks, "_split_audio_to_segments", return_value=[audio_path]), \
                 mock.patch.object(tasks, "_transcribe_segment", return_value="[00:00] **[發言者 A]**：逐字稿內容。") as transcribe_mock, \
                 mock.patch.object(tasks, "is_job_cancel_requested", return_value=False), \
                 mock.patch.object(tasks, "update_job_status"), \
                 mock.patch.object(tasks, "save_meeting"):
                output_path = tasks.process_audio_task(
                    job_id="single-dual-model-job",
                    audio_path=audio_path,
                    output_dir=output_dir,
                    model="gemini-transcribe-test",
                    summary_model="gemma-4-31b-it",
                    summary_fallback_model="gemini-transcribe-test",
                )

            self.assertIsNotNone(output_path)
            transcribe_mock.assert_called_once()
            self.assertEqual(transcribe_mock.call_args.args[5], "gemini-transcribe-test")
            self.assertEqual([call["model"] for call in fake_client.models.calls], ["gemma-4-31b-it"])
            content = output_path.read_text(encoding="utf-8")
            self.assertIn("單段音檔也使用摘要模型", content)
            self.assertIn("### [Segment 1/1 | 00:00 - end]", content)
            self.assertIn("transcription_model: gemini-transcribe-test", content)
            self.assertIn("summary_model: gemma-4-31b-it", content)

    def test_segmented_audio_task_uses_summary_model_with_fallback(self):
        import backend.tasks as tasks

        summary_content = (
            "## 📋 一、討論摘要 (Discussion Summary)\n"
            "以備援模型完成摘要。\n\n"
            "---\n\n"
            "## ✅ 二、最終決議 (Final Decisions)\n"
            "尚未決定。\n\n"
            "---\n\n"
            "## 📌 三、待辦事項 (Action Items)\n\n"
            "| # | 任務描述 | 負責人 | 期限 | 優先級 |\n"
            "|---|---------|--------|------|--------|\n"
            "| 1 | 確認後續事項 | 發言者 A | 未提及 | 中 |\n"
        )

        class FakeModels:
            def __init__(self):
                self.calls = []

            def generate_content(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs["model"] == "gemma-4-31b-it":
                    raise RuntimeError("primary summary model unavailable")
                return type("Response", (), {"text": summary_content})()

        class FakeClient:
            def __init__(self):
                self.models = FakeModels()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "output"
            output_dir.mkdir()
            audio_path = root / "meeting.webm"
            audio_path.write_bytes(b"audio")
            seg1 = root / "_seg_meeting_000.mp3"
            seg2 = root / "_seg_meeting_001.mp3"
            seg1.write_bytes(b"seg1")
            seg2.write_bytes(b"seg2")

            context = tasks._segment_cache_context(
                audio_path=audio_path,
                model="gemini-test",
                total_segments=2,
                segment_minutes=tasks.SEGMENT_MINUTES,
            )
            tasks._save_segment_transcript_cache(
                output_dir=output_dir,
                job_id="dual-model-job",
                segment_index=0,
                context=context,
                transcript="[00:00] **[發言者 A]**：第一段開始。\n[09:30] **[發言者 A]**：第一段結束。",
            )
            tasks._save_segment_transcript_cache(
                output_dir=output_dir,
                job_id="dual-model-job",
                segment_index=1,
                context=context,
                transcript="[10:00] **[發言者 B]**：第二段內容。",
            )

            fake_client = FakeClient()
            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                 mock.patch.object(tasks.genai, "Client", return_value=fake_client), \
                 mock.patch.object(tasks, "_split_audio_to_segments", return_value=[seg1, seg2]), \
                 mock.patch.object(tasks, "_transcribe_segment") as transcribe_mock, \
                 mock.patch.object(tasks, "is_job_cancel_requested", return_value=False), \
                 mock.patch.object(tasks, "update_job_status"), \
                 mock.patch.object(tasks, "save_meeting"):
                output_path = tasks.process_audio_task(
                    job_id="dual-model-job",
                    audio_path=audio_path,
                    output_dir=output_dir,
                    model="gemini-test",
                    summary_model="gemma-4-31b-it",
                    summary_fallback_model="gemini-test",
                )

            self.assertIsNotNone(output_path)
            transcribe_mock.assert_not_called()
            self.assertEqual(
                [call["model"] for call in fake_client.models.calls],
                ["gemma-4-31b-it", "gemini-test"],
            )
            content = output_path.read_text(encoding="utf-8")
            self.assertIn("以備援模型完成摘要", content)
            self.assertIn("transcription_model: gemini-test", content)
            self.assertIn("summary_model: gemini-test", content)
            self.assertIn("summary_fallback_model: gemini-test", content)

    def test_segmented_audio_task_rejects_fresh_incomplete_segment(self):
        import backend.tasks as tasks

        class FakeModels:
            def generate_content(self, **kwargs):
                raise AssertionError("不應在缺段時生成摘要")

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.models = FakeModels()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "output"
            output_dir.mkdir()
            audio_path = root / "meeting.webm"
            audio_path.write_bytes(b"audio")
            seg1 = root / "_seg_meeting_000.mp3"
            seg2 = root / "_seg_meeting_001.mp3"
            seg1.write_bytes(b"seg1")
            seg2.write_bytes(b"seg2")

            incomplete = (
                "[00:00] **[發言者 A]**：只有開頭。\n"
                "[系統提示：此處音檔包含無意義雜訊，已自動過濾後續重複內容]"
            )

            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                 mock.patch.object(tasks.genai, "Client", side_effect=FakeClient), \
                 mock.patch.object(tasks, "_split_audio_to_segments", return_value=[seg1, seg2]), \
                 mock.patch.object(tasks, "_transcribe_segment", return_value=incomplete), \
                 mock.patch.object(tasks, "is_job_cancel_requested", return_value=False), \
                 mock.patch.object(tasks, "update_job_status"), \
                 mock.patch.object(tasks, "save_meeting") as save_mock:
                output_path = tasks.process_audio_task(
                    job_id="incomplete-job",
                    audio_path=audio_path,
                    output_dir=output_dir,
                    model="gemini-test",
                )

            self.assertIsNone(output_path)
            save_mock.assert_not_called()
            self.assertFalse(tasks._segment_cache_file(output_dir, "incomplete-job", 0).exists())

    def test_summary_preview_supports_discussion_summary_heading(self):
        from backend.tasks import _extract_summary_preview

        content = (
            "## 📋 一、討論摘要 (Discussion Summary)\n"
            "這是摘要內容。\n\n"
            "## ✅ 二、最終決議 (Final Decisions)\n"
            "決議內容"
        )

        self.assertEqual(_extract_summary_preview(content), "這是摘要內容。")

    def test_hallucination_cleanup_preserves_markdown_table_separator_rows(self):
        from backend.tasks import clean_hallucinated_loops

        content = (
            "## 📌 三、待辦事項 (Action Items)\n\n"
            "| # | 任務描述 | 負責人 | 期限 | 優先級 |\n"
            "|---|---------|--------|------|--------|\n"
            "| 1 | 整理產品需求 | 王經理 | 下週三 | 高 |\n"
            "| 2 | 確認測試計畫 | 李小姐 | 未提及 | 高 |\n"
        )

        self.assertEqual(clean_hallucinated_loops(content), content)

    def test_meeting_content_quality_detects_missing_sections_and_broken_table(self):
        from backend.tasks import _meeting_content_quality_issues

        broken_content = (
            "## 📋 一、討論摘要 (Discussion Summary)\n摘要\n\n"
            "## ✅ 二、最終決議 (Final Decisions)\n決議\n\n"
            "## 📌 三、待辦事項 (Action Items)\n\n"
            "| # | 任務描述 | 負責人 | 期限 | 優先級 |\n"
            "|---|\n"
        )

        issues = _meeting_content_quality_issues(broken_content)

        self.assertIn("缺少完整逐字稿區塊", issues)
        self.assertIn("待辦事項表格分隔列不完整", issues)

    def test_meeting_content_repair_uses_gemini_once_when_quality_check_fails(self):
        from backend.tasks import _repair_meeting_content_if_needed, _meeting_content_quality_issues

        broken_content = (
            "## 📋 一、討論摘要 (Discussion Summary)\n摘要\n\n"
            "## ✅ 二、最終決議 (Final Decisions)\n決議\n\n"
            "## 📌 三、待辦事項 (Action Items)\n\n"
            "| # | 任務描述 | 負責人 | 期限 | 優先級 |\n"
            "|---|\n"
        )
        repaired_content = (
            "## 📋 一、討論摘要 (Discussion Summary)\n摘要\n\n"
            "## ✅ 二、最終決議 (Final Decisions)\n決議\n\n"
            "## 📌 三、待辦事項 (Action Items)\n\n"
            "| # | 任務描述 | 負責人 | 期限 | 優先級 |\n"
            "|---|---------|--------|------|--------|\n"
            "| 1 | 整理需求 | 王經理 | 下週三 | 高 |\n\n"
            "## 📝 四、完整逐字稿 (Verbatim Transcript)\n[00:00] **[發言者]**：內容。\n"
        )

        class FakeModels:
            def __init__(self):
                self.calls = []

            def generate_content(self, **kwargs):
                self.calls.append(kwargs)
                return type("Response", (), {"text": repaired_content})()

        class FakeClient:
            def __init__(self):
                self.models = FakeModels()

        client = FakeClient()

        result = _repair_meeting_content_if_needed(
            client=client,
            model="gemini-test",
            meeting_content=broken_content,
            job_id="quality-job",
        )

        self.assertEqual(result, repaired_content)
        self.assertEqual(len(client.models.calls), 1)
        self.assertEqual(_meeting_content_quality_issues(result), [])

    def test_golden_meeting_markdown_keeps_required_structure(self):
        from backend.tasks import _extract_summary_preview, _meeting_content_quality_issues

        content = (ROOT / "tests" / "fixtures" / "golden_meeting.md").read_text(encoding="utf-8")

        self.assertEqual(_meeting_content_quality_issues(content), [])
        self.assertEqual(_extract_summary_preview(content), "本次會議確認新版儀器驗證排程，並分派文件與測試準備工作。")


class DurableQueueRegressionTests(unittest.TestCase):
    def _isolated_database(self):
        import backend.database as database

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        patcher = mock.patch.object(database, "DB_PATH", Path(tmpdir.name) / "meetings.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_db()
        return database

    def test_jobs_table_has_durable_queue_columns(self):
        database = self._isolated_database()

        with database.get_db() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}

        self.assertTrue(
            {
                "task_type",
                "source",
                "payload_json",
                "attempts",
                "max_attempts",
                "queued_at",
                "started_at",
                "updated_at",
                "cancel_requested",
                "progress_current",
                "progress_total",
            }.issubset(columns)
        )

    def test_startup_bumps_active_legacy_jobs_to_default_attempts(self):
        database = self._isolated_database()
        database.create_job("legacy-low-attempts-job", max_attempts=2)

        database.init_db()

        self.assertEqual(database.get_job("legacy-low-attempts-job")["max_attempts"], 5)

    def test_claim_next_pending_job_persists_payload_and_increments_attempts(self):
        database = self._isolated_database()
        database.create_job(
            "queue-job-1",
            task_type="audio_processing",
            source="upload",
            payload={"audio_path": "/tmp/audio.mp3", "model": "test-model"},
            max_attempts=3,
        )

        claimed = database.claim_next_pending_job()

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["job_id"], "queue-job-1")
        self.assertEqual(claimed["status"], "processing")
        self.assertEqual(claimed["attempts"], 1)
        self.assertEqual(claimed["payload"]["audio_path"], "/tmp/audio.mp3")
        self.assertEqual(database.get_job("queue-job-1")["status"], "processing")

    def test_failed_attempt_requeues_until_max_attempts_then_fails(self):
        database = self._isolated_database()
        database.create_job("queue-job-2", max_attempts=2)

        database.claim_next_pending_job()
        first_status = database.retry_or_fail_job("queue-job-2", "temporary failure")
        self.assertEqual(first_status, "pending")
        self.assertEqual(database.get_job("queue-job-2")["error_detail"], "temporary failure")

        database.claim_next_pending_job()
        final_status = database.retry_or_fail_job("queue-job-2", "permanent failure")
        job = database.get_job("queue-job-2")

        self.assertEqual(final_status, "failed")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_detail"], "permanent failure")
        self.assertIsNotNone(job["completed_at"])

    def test_transient_503_failure_is_requeued_after_delay(self):
        database = self._isolated_database()
        database.create_job("queue-job-503", max_attempts=5)

        with mock.patch.dict(os.environ, {"JOB_QUEUE_TRANSIENT_RETRY_DELAY_SECONDS": "60"}, clear=False):
            database.claim_next_pending_job()
            status = database.retry_or_fail_job("queue-job-503", "503 UNAVAILABLE")
            job = database.get_job("queue-job-503")

            self.assertEqual(status, "pending")
            self.assertIn("服務暫時忙碌", job["message"])
            self.assertIsNone(database.claim_next_pending_job())

    def test_interrupted_processing_jobs_are_requeued_on_startup(self):
        database = self._isolated_database()
        database.create_job(
            "queue-job-3",
            payload={"audio_path": "/tmp/audio.mp3", "output_dir": "/tmp"},
            max_attempts=3,
        )
        database.claim_next_pending_job()

        requeued = database.requeue_interrupted_jobs()
        job = database.get_job("queue-job-3")

        self.assertEqual(requeued, 1)
        self.assertEqual(job["status"], "pending")
        self.assertIn("重新排入", job["message"])

    def test_interrupted_legacy_jobs_without_payload_are_not_requeued(self):
        database = self._isolated_database()
        database.create_job("queue-job-legacy", max_attempts=3)
        database.claim_next_pending_job()

        requeued = database.requeue_interrupted_jobs()
        job = database.get_job("queue-job-legacy")

        self.assertEqual(requeued, 0)
        self.assertEqual(job["status"], "failed")
        self.assertIn("缺少可恢復", job["error_detail"])

    def test_cancel_pending_job_prevents_future_claim(self):
        database = self._isolated_database()
        database.create_job("queue-job-4")

        self.assertTrue(database.request_job_cancel("queue-job-4"))
        self.assertIsNone(database.claim_next_pending_job())
        self.assertEqual(database.get_job("queue-job-4")["status"], "cancelled")

    def test_progress_fields_are_persisted_on_status_update(self):
        database = self._isolated_database()
        database.create_job("queue-job-5")

        database.update_job_status(
            "queue-job-5",
            "processing",
            "處理到一半",
            progress_current=1,
            progress_total=2,
        )
        job = database.get_job("queue-job-5")

        self.assertEqual(job["progress_current"], 1)
        self.assertEqual(job["progress_total"], 2)
        self.assertIsNotNone(job["updated_at"])


class UploadQueueRegressionTests(unittest.TestCase):
    def test_upload_route_enqueues_audio_job_instead_of_inline_background_task(self):
        source = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        upload_body = source[
            source.index("async def upload_audio") :
            source.index("# =============================================================================\n# 任務狀態查詢端點")
        ]

        self.assertIn("enqueue_audio_job", upload_body)
        self.assertNotIn("background_tasks.add_task", upload_body)

    def test_upload_route_saves_original_audio_to_source_audio_dir(self):
        import backend.main as main

        captured = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            source_audio_dir = tmpdir_path / "source_audio"
            output_dir = tmpdir_path / "output"
            source_audio_dir.mkdir()
            output_dir.mkdir()

            def fake_enqueue_audio_job(**kwargs):
                captured.update(kwargs)

            with mock.patch.object(main, "SOURCE_AUDIO_DIR", source_audio_dir), \
                 mock.patch.object(main, "OUTPUT_DIR", output_dir), \
                 mock.patch.object(main, "enqueue_audio_job", side_effect=fake_enqueue_audio_job):
                response = asgi_request(
                    main.app,
                    "POST",
                    "/upload-audio",
                    files={"file": ("meeting.mp3", BytesIO(b"ID3" + b"\0" * 32), "audio/mpeg")},
                )

            self.assertEqual(response.status_code, 202)
            saved_audio = list(source_audio_dir.glob("*.mp3"))
            self.assertEqual(len(saved_audio), 1)
            self.assertEqual(captured["audio_path"], saved_audio[0])
            self.assertEqual(captured["output_dir"], output_dir)
            self.assertTrue(saved_audio[0].read_bytes().startswith(b"ID3"))

    def test_upload_route_reuses_existing_audio_with_same_sha256(self):
        import backend.main as main

        captured = {}
        media_bytes = b"ID3" + b"\0" * 32
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            source_audio_dir = tmpdir_path / "source_audio"
            output_dir = tmpdir_path / "output"
            source_audio_dir.mkdir()
            output_dir.mkdir()
            existing_audio = source_audio_dir / "existing-source.mp3"
            existing_audio.write_bytes(media_bytes)

            def fake_enqueue_audio_job(**kwargs):
                captured.update(kwargs)

            with mock.patch.object(main, "SOURCE_AUDIO_DIR", source_audio_dir), \
                 mock.patch.object(main, "OUTPUT_DIR", output_dir), \
                 mock.patch.object(main, "enqueue_audio_job", side_effect=fake_enqueue_audio_job):
                response = asgi_request(
                    main.app,
                    "POST",
                    "/upload-audio",
                    files={"file": ("meeting.mp3", BytesIO(media_bytes), "audio/mpeg")},
                )

            self.assertEqual(response.status_code, 202)
            self.assertEqual(captured["audio_path"], existing_audio)
            self.assertEqual(list(source_audio_dir.glob("*.mp3")), [existing_audio])
            self.assertFalse(list(source_audio_dir.glob(".upload_*")))

    def test_upload_route_keeps_reused_audio_when_enqueue_fails(self):
        import backend.main as main

        media_bytes = b"ID3" + b"\0" * 32
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            source_audio_dir = tmpdir_path / "source_audio"
            output_dir = tmpdir_path / "output"
            source_audio_dir.mkdir()
            output_dir.mkdir()
            existing_audio = source_audio_dir / "existing-source.mp3"
            existing_audio.write_bytes(media_bytes)

            with mock.patch.object(main, "SOURCE_AUDIO_DIR", source_audio_dir), \
                 mock.patch.object(main, "OUTPUT_DIR", output_dir), \
                 mock.patch.object(main, "enqueue_audio_job", side_effect=RuntimeError("queue down")):
                response = asgi_request(
                    main.app,
                    "POST",
                    "/upload-audio",
                    files={"file": ("meeting.mp3", BytesIO(media_bytes), "audio/mpeg")},
                )

            self.assertEqual(response.status_code, 500)
            self.assertTrue(existing_audio.exists())
            self.assertEqual(list(source_audio_dir.glob("*.mp3")), [existing_audio])
            self.assertFalse(list(source_audio_dir.glob(".upload_*")))


class TempCleanupRegressionTests(unittest.TestCase):
    def test_stale_temp_cleanup_preserves_active_and_fresh_files(self):
        from backend.cleanup import cleanup_stale_temp_files

        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            stale = temp_dir / "old-unreferenced.wav"
            active = temp_dir / "active-retry.wav"
            fresh = temp_dir / "fresh.wav"
            nested_segment = temp_dir / "_seg_old_000.mp3"

            for path in (stale, active, fresh, nested_segment):
                path.write_bytes(b"audio")

            old_time = 1_700_000_000
            now = old_time + 7200
            for path in (stale, active, nested_segment):
                path.touch()
                import os
                os.utime(path, (old_time, old_time))
            import os
            os.utime(fresh, (now, now))

            deleted = cleanup_stale_temp_files(
                temp_dir=temp_dir,
                active_paths={active},
                max_age_seconds=3600,
                now=now,
            )

            self.assertEqual({path.name for path in deleted}, {"old-unreferenced.wav", "_seg_old_000.mp3"})
            self.assertFalse(stale.exists())
            self.assertTrue(active.exists())
            self.assertTrue(fresh.exists())

    def test_lifespan_runs_temp_cleanup_before_worker_start(self):
        source = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        lifespan_body = source[
            source.index("async def lifespan") :
            source.index("app = FastAPI(")
        ]

        self.assertIn("cleanup_stale_temp_files_for_jobs", lifespan_body)
        self.assertLess(
            lifespan_body.index("cleanup_stale_temp_files_for_jobs"),
            lifespan_body.index("job_worker.start()"),
        )


class JobRetentionCleanupRegressionTests(unittest.TestCase):
    def _isolated_database(self):
        import backend.database as database

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        patcher = mock.patch.object(database, "DB_PATH", Path(tmpdir.name) / "meetings.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_db()
        return database

    def test_terminal_job_cleanup_deletes_only_jobs_older_than_retention(self):
        database = self._isolated_database()
        from backend.cleanup import cleanup_terminal_jobs

        now = datetime(2026, 1, 15, 12, 0, 0)
        old_completed_at = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        recent_completed_at = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")

        database.create_job("old-failed")
        database.update_job_status("old-failed", "failed", "失敗")
        database.create_job("recent-done")
        database.update_job_status("recent-done", "done", "完成")
        database.create_job("old-pending")

        with database.get_db() as conn:
            conn.execute(
                "UPDATE jobs SET completed_at=?, updated_at=? WHERE job_id='old-failed'",
                (old_completed_at, old_completed_at),
            )
            conn.execute(
                "UPDATE jobs SET completed_at=?, updated_at=? WHERE job_id='recent-done'",
                (recent_completed_at, recent_completed_at),
            )
            conn.execute(
                "UPDATE jobs SET queued_at=?, updated_at=? WHERE job_id='old-pending'",
                (old_completed_at, old_completed_at),
            )

        deleted = cleanup_terminal_jobs(max_age_days=7, now=now)

        self.assertEqual(deleted, 1)
        self.assertIsNone(database.get_job("old-failed"))
        self.assertIsNotNone(database.get_job("recent-done"))
        self.assertIsNotNone(database.get_job("old-pending"))

    def test_lifespan_runs_terminal_job_cleanup_before_worker_start(self):
        source = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        lifespan_body = source[
            source.index("async def lifespan") :
            source.index("app = FastAPI(")
        ]

        self.assertIn("cleanup_terminal_jobs", lifespan_body)
        self.assertLess(
            lifespan_body.index("cleanup_terminal_jobs"),
            lifespan_body.index("job_worker.start()"),
        )


class JobDashboardRegressionTests(unittest.TestCase):
    def _isolated_database(self):
        import backend.database as database

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        patcher = mock.patch.object(database, "DB_PATH", Path(tmpdir.name) / "meetings.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_db()
        return database

    def test_jobs_endpoint_lists_jobs_and_filters_by_status(self):
        database = self._isolated_database()
        import backend.main as main

        database.create_job(
            "job-dashboard-pending",
            source="upload",
            payload={"audio_path": "/tmp/pending.wav", "output_dir": "/tmp"},
        )
        database.create_job(
            "job-dashboard-failed",
            source="line",
            payload={"message_id": "line-message", "user_id": "user-id"},
        )
        database.update_job_status(
            "job-dashboard-failed",
            "failed",
            "處理失敗",
            error_detail="測試錯誤",
        )

        response = asgi_request(main.app, "GET", "/jobs?status=failed&limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["jobs"][0]["job_id"], "job-dashboard-failed")
        self.assertEqual(payload["jobs"][0]["status"], "failed")
        self.assertEqual(payload["jobs"][0]["source"], "line")
        self.assertEqual(payload["jobs"][0]["error_detail"], "測試錯誤")

    def test_failed_job_can_be_requeued_by_api(self):
        database = self._isolated_database()
        import backend.main as main

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            audio_path = tmp_path / "retry.mp3"
            audio_path.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x21audio")
            database.create_job(
                "job-retry-api",
                source="upload",
                payload={"audio_path": str(audio_path), "output_dir": str(tmp_path)},
            )
            database.update_job_status("job-retry-api", "failed", "處理失敗", error_detail="暫時錯誤")

            response = asgi_request(main.app, "POST", "/jobs/job-retry-api/retry")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["attempts"], 0)
        self.assertIsNone(payload["error_detail"])

    def test_retry_rejects_missing_audio_payload_file(self):
        database = self._isolated_database()
        import backend.main as main

        missing_audio = Path(tempfile.gettempdir()) / "meeting-assistant-missing-retry-source.mp3"
        if missing_audio.exists():
            missing_audio.unlink()
        database.create_job(
            "job-retry-missing-audio",
            source="upload",
            payload={"audio_path": str(missing_audio), "output_dir": tempfile.gettempdir()},
        )
        database.update_job_status("job-retry-missing-audio", "failed", "處理失敗", error_detail="原檔遺失")

        response = asgi_request(main.app, "POST", "/jobs/job-retry-missing-audio/retry")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(database.get_job("job-retry-missing-audio")["status"], "failed")

    def test_job_events_endpoint_returns_status_timeline(self):
        database = self._isolated_database()
        import backend.main as main

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            audio_path = tmp_path / "timeline.mp3"
            audio_path.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x21audio")
            database.create_job(
                "job-events-api",
                payload={"audio_path": str(audio_path), "output_dir": str(tmp_path)},
            )
            database.update_job_status("job-events-api", "failed", "處理失敗", error_detail="temporary")
            database.requeue_failed_job("job-events-api")

            response = asgi_request(main.app, "GET", "/jobs/job-events-api/events")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["job_id"], "job-events-api")
        self.assertEqual(
            [event["event_type"] for event in payload["events"]],
            ["created", "status_failed", "requeued"],
        )
        self.assertEqual(payload["events"][1]["detail"], "temporary")

    def test_terminal_job_can_be_deleted_by_api(self):
        database = self._isolated_database()
        import backend.main as main

        database.create_job("job-delete-api")
        database.update_job_status("job-delete-api", "cancelled", "已取消")

        response = asgi_request(main.app, "DELETE", "/jobs/job-delete-api")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(database.get_job("job-delete-api"))

    def test_processing_job_cannot_be_deleted_by_api(self):
        database = self._isolated_database()
        import backend.main as main

        database.create_job("job-delete-processing", payload={"audio_path": "/tmp/a.mp3", "output_dir": "/tmp"})
        database.claim_next_pending_job()

        response = asgi_request(main.app, "DELETE", "/jobs/job-delete-processing")

        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(database.get_job("job-delete-processing"))


class MetricsRegressionTests(unittest.TestCase):
    def _isolated_database(self):
        import backend.database as database

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        patcher = mock.patch.object(database, "DB_PATH", Path(tmpdir.name) / "meetings.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_db()
        return database

    def test_metrics_endpoint_reports_job_counts_and_recent_errors(self):
        database = self._isolated_database()
        import backend.main as main

        database.create_job("metrics-pending")
        database.create_job("metrics-failed")
        database.update_job_status("metrics-failed", "failed", "處理失敗", error_detail="metrics error")

        response = asgi_request(main.app, "GET", "/metrics")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["jobs"]["total"], 2)
        self.assertEqual(payload["jobs"]["by_status"]["pending"], 1)
        self.assertEqual(payload["jobs"]["by_status"]["failed"], 1)
        self.assertEqual(payload["recent_errors"][0]["job_id"], "metrics-failed")
        self.assertEqual(payload["recent_errors"][0]["error_detail"], "metrics error")

    def test_metrics_endpoint_reports_ngrok_status(self):
        self._isolated_database()
        import backend.main as main

        ngrok_status = {
            "running": True,
            "public_url": "https://example.ngrok-free.app",
            "webhook_url": "https://example.ngrok-free.app/line-webhook",
            "message": "ngrok tunnel is forwarding to local server",
        }

        with mock.patch.object(main, "get_ngrok_status", return_value=ngrok_status):
            response = asgi_request(main.app, "GET", "/metrics")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ngrok"]["running"])
        self.assertEqual(payload["ngrok"]["webhook_url"], "https://example.ngrok-free.app/line-webhook")


class SearchRegressionTests(unittest.TestCase):
    def _isolated_database(self):
        import backend.database as database

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        patcher = mock.patch.object(database, "DB_PATH", Path(tmpdir.name) / "meetings.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_db()
        return database, Path(tmpdir.name)

    def test_search_meetings_uses_fts5_index_for_audio_filename(self):
        database, tmp_path = self._isolated_database()
        output_path = tmp_path / "indexed-meeting.md"
        output_path.write_text("完整逐字稿包含 ultrasonic validation", encoding="utf-8")

        database.save_meeting(
            title="Weekly Sync",
            date="2026/07/05",
            source_audio="sourceaudiospecial.wav",
            output_path=str(output_path),
            summary="討論例行事項",
        )

        results = database.search_meetings("sourceaudiospecial")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source_audio"], "sourceaudiospecial.wav")
        with database.get_db() as conn:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='meeting_fts'"
            ).fetchone()
        self.assertIsNotNone(table)


class MeetingEvidenceRegressionTests(unittest.TestCase):
    def _isolated_database(self):
        import backend.database as database

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        patcher = mock.patch.object(database, "DB_PATH", Path(tmpdir.name) / "meetings.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_db()
        return database, Path(tmpdir.name)

    def test_supplementary_evidence_is_appended_to_meeting_markdown(self):
        database, tmp_path = self._isolated_database()
        from backend import evidence

        meeting_path = tmp_path / "meeting.md"
        meeting_path.write_text(
            "## 📋 一、討論摘要 (Discussion Summary)\n原摘要\n\n"
            "## 📝 四、完整逐字稿 (Verbatim Transcript)\n[00:00] 內容\n",
            encoding="utf-8",
        )
        meeting_id = database.save_meeting(
            title="補充資料測試",
            date="2026/07/05",
            source_audio="meeting.mp3",
            output_path=str(meeting_path),
            summary="原摘要",
        )
        evidence_path = tmp_path / "quote.png"
        evidence_path.write_bytes(b"png-bytes")

        with mock.patch.object(
            evidence,
            "generate_evidence_markdown",
            return_value=(
                "### 資料 1：quote.png\n"
                "- 系統判斷：與採購決策高度相關\n"
                "- 擷取重點：單價為 12,000 元\n"
                "- 原始檔案：quote.png\n"
            ),
        ):
            result = evidence.analyze_and_append_evidence(
                meeting_id=meeting_id,
                source_path=evidence_path,
                original_filename="quote.png",
                note=None,
                model="test-model",
            )

        updated = meeting_path.read_text(encoding="utf-8")
        self.assertIn("## 📎 五、補充資料與佐證", updated)
        self.assertIn("單價為 12,000 元", updated)
        self.assertTrue(Path(result["attachment_path"]).is_file())
        self.assertIn("quote.png", result["evidence_markdown"])

    def test_evidence_upload_endpoint_appends_analysis_and_returns_updated_content(self):
        database, tmp_path = self._isolated_database()
        import backend.main as main

        meeting_path = tmp_path / "meeting.md"
        meeting_path.write_text("## 📋 一、討論摘要 (Discussion Summary)\n原摘要\n", encoding="utf-8")
        meeting_id = database.save_meeting(
            title="API 補充資料測試",
            date="2026/07/05",
            source_audio="meeting.mp3",
            output_path=str(meeting_path),
            summary="原摘要",
        )

        fake_result = {
            "status": "success",
            "meeting_id": meeting_id,
            "file_name": "evidence.txt",
            "attachment_path": str(tmp_path / "attachments" / "evidence.txt"),
            "evidence_markdown": "### 資料 1：evidence.txt\n- 擷取重點：補充內容\n",
            "full_content": "## 📎 五、補充資料與佐證\n補充內容",
        }

        with mock.patch.object(main, "analyze_and_append_evidence", return_value=fake_result):
            response = asgi_request(
                main.app,
                "POST",
                f"/meetings/{meeting_id}/evidence",
                files={"file": ("evidence.txt", b"hello", "text/plain")},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["meeting_id"], meeting_id)
        self.assertIn("補充內容", payload["full_content"])


class DocxExporterRegressionTests(unittest.TestCase):
    def test_exporter_converts_action_items_markdown_table_to_word_table(self):
        from docx import Document
        from backend.exporter import export_meeting_to_docx

        markdown = """---
title: 會議記錄 - 測試會議
---

## 📋 一、討論摘要 (Discussion Summary)

這是一段含有 **粗體重點** 的摘要。

## 📌 三、待辦事項 (Action Items)

| # | 關聯討論 | 關聯決議 | 任務描述 | 負責人 | 期限 | 優先級 |
|---|---------|---------|---------|--------|------|--------|
| A1 | D1 | R1 | **追蹤** 供應商報價與回覆 | 王經理 | 7月底 | 高 |
| A2 | D2 | 未提及 | 整理測試資料 | QA | 未定 | 中 |
"""

        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_doc = Document()
            template_doc.add_table(rows=6, cols=2)
            template_doc.save(str(template_path))
            template_env = mock.patch.dict(os.environ, {"MEETING_DOCX_TEMPLATE_PATH": str(template_path)})
            template_env.start()
            self.addCleanup(template_env.stop)
            output_path = Path(tmpdir) / "meeting.docx"
            ok = export_meeting_to_docx(
                {
                    "date": "2026/07/06",
                    "title": "測試會議",
                    "full_content": markdown,
                },
                str(output_path),
            )

            self.assertTrue(ok)
            doc = Document(str(output_path))
            content_cell = doc.tables[0].cell(5, 0)
            self.assertEqual(len(content_cell.tables), 1)

            action_table = content_cell.tables[0]
            self.assertEqual(action_table.cell(0, 3).text, "任務描述")
            self.assertEqual(action_table.cell(1, 1).text, "D1")
            self.assertEqual(action_table.cell(1, 2).text, "R1")
            self.assertEqual(action_table.cell(1, 3).text, "追蹤 供應商報價與回覆")
            self.assertTrue(action_table.cell(1, 3).paragraphs[0].runs[0].bold)
            self.assertNotIn("**", content_cell.text)
            self.assertNotIn("|---", content_cell.text)


class JobQueueWorkerRegressionTests(unittest.TestCase):
    def _isolated_database(self):
        import backend.database as database

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        patcher = mock.patch.object(database, "DB_PATH", Path(tmpdir.name) / "meetings.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_db()
        return database, Path(tmpdir.name)

    def test_audio_jobs_default_to_five_attempts(self):
        database, tmpdir = self._isolated_database()
        import backend.job_queue as job_queue

        audio_path = tmpdir / "default-attempts.mp3"
        audio_path.write_bytes(b"audio")
        job_queue.enqueue_audio_job(
            "worker-default-attempts-job",
            audio_path=audio_path,
            output_dir=tmpdir,
            model="test-model",
        )

        self.assertEqual(database.get_job("worker-default-attempts-job")["max_attempts"], 5)

    def test_audio_worker_keeps_source_file_for_retry_and_terminal_failure(self):
        database, tmpdir = self._isolated_database()
        import backend.job_queue as job_queue

        audio_path = tmpdir / "retry-source.mp3"
        audio_path.write_bytes(b"audio")
        job_queue.enqueue_audio_job(
            "worker-retry-job",
            audio_path=audio_path,
            output_dir=tmpdir,
            model="test-model",
            max_attempts=2,
        )
        worker = job_queue.JobQueueWorker(poll_interval=0.01)

        first_claim = database.claim_next_pending_job()
        with mock.patch.object(job_queue, "process_audio_task", return_value=None):
            worker.process_job(first_claim)

        self.assertEqual(database.get_job("worker-retry-job")["status"], "pending")
        self.assertTrue(audio_path.exists())

        second_claim = database.claim_next_pending_job()
        with mock.patch.object(job_queue, "process_audio_task", return_value=None):
            worker.process_job(second_claim)

        self.assertEqual(database.get_job("worker-retry-job")["status"], "failed")
        self.assertTrue(audio_path.exists())

    def test_audio_worker_keeps_source_file_after_success(self):
        database, tmpdir = self._isolated_database()
        import backend.job_queue as job_queue

        audio_path = tmpdir / "success-source.mp3"
        output_path = tmpdir / "meeting.md"
        audio_path.write_bytes(b"audio")
        output_path.write_text("done", encoding="utf-8")
        job_queue.enqueue_audio_job(
            "worker-success-job",
            audio_path=audio_path,
            output_dir=tmpdir,
            model="test-model",
        )
        worker = job_queue.JobQueueWorker(poll_interval=0.01)

        def mark_done(**kwargs):
            database.update_job_status(
                kwargs["job_id"],
                "done",
                "完成",
                output_path=str(output_path),
            )
            return output_path

        claim = database.claim_next_pending_job()
        with mock.patch.object(job_queue, "process_audio_task", side_effect=mark_done):
            worker.process_job(claim)

        self.assertEqual(database.get_job("worker-success-job")["status"], "done")
        self.assertTrue(audio_path.exists())


class LineRegressionTests(unittest.TestCase):
    def test_enqueue_line_audio_job_is_idempotent_by_message_id(self):
        import backend.database as database
        import backend.job_queue as job_queue

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(database, "DB_PATH", Path(tmpdir) / "meetings.db"):
                database.init_db()

                job_queue.enqueue_line_audio_job(
                    job_id="line-job-first",
                    message_id="same-line-message",
                    user_id="user-id",
                    model="test-model",
                )
                job_queue.enqueue_line_audio_job(
                    job_id="line-job-duplicate",
                    message_id="same-line-message",
                    user_id="user-id",
                    model="test-model",
                )

                jobs = database.list_jobs(limit=10)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_id"], "line-job-first")

    def test_push_text_sends_long_outputs_in_five_message_batches(self):
        import backend.line_handler as line_handler

        class FakeLineApi:
            def __init__(self):
                self.requests = []

            def push_message(self, request):
                self.requests.append(request)

        api = FakeLineApi()
        line_handler.push_text(api, "user-id", "a" * (4900 * 6))

        self.assertEqual(len(api.requests), 2)
        self.assertEqual([len(request.messages) for request in api.requests], [5, 1])

    def test_download_line_audio_waits_when_line_content_is_still_processing(self):
        import backend.line_handler as line_handler

        class FakeResponse:
            def __init__(self, status_code, content=b"", payload=None):
                self.status_code = status_code
                self.content = content
                self._payload = payload or {}

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def json(self):
                return self._payload

        calls = []
        content_calls = 0

        def fake_get(url, **kwargs):
            nonlocal content_calls
            calls.append(url)
            if url.endswith("/content/transcoding"):
                return FakeResponse(200, payload={"status": "succeeded"})
            content_calls += 1
            if content_calls == 1:
                return FakeResponse(202)
            return FakeResponse(200, content=b"audio")

        with mock.patch.object(line_handler.requests, "get", side_effect=fake_get):
            audio = line_handler.download_line_audio("message-id")

        self.assertEqual(audio, b"audio")
        self.assertTrue(any(url.endswith("/content/transcoding") for url in calls))

    def test_line_file_message_enqueues_supported_audio_file(self):
        import backend.main as main
        import backend.line_handler as line_handler
        from linebot.v3.webhooks import (
            DeliveryContext,
            EventMode,
            FileMessageContent,
            MessageEvent,
            UserSource,
        )

        event = MessageEvent(
            source=UserSource(userId="user-id"),
            timestamp=1,
            mode=EventMode.ACTIVE,
            webhookEventId="event-id",
            deliveryContext=DeliveryContext(isRedelivery=False),
            replyToken="reply-token",
            message=FileMessageContent(
                id="file-message-id",
                fileName="meeting.mp3",
                fileSize=1234,
            ),
        )

        class FakeParser:
            def parse(self, body, signature):
                return [event]

        enqueued = []

        def fake_enqueue(**kwargs):
            enqueued.append(kwargs)

        with mock.patch.object(line_handler, "get_webhook_parser", return_value=FakeParser()), \
             mock.patch.object(line_handler, "get_line_api", return_value=object()), \
             mock.patch.object(line_handler, "reply_text"), \
             mock.patch.object(main, "enqueue_line_audio_job", side_effect=fake_enqueue):
            response = asgi_request(
                main.app,
                "POST",
                "/line-webhook",
                headers={"X-Line-Signature": "sig"},
                content=b'{"events":[]}',
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(enqueued[0]["message_id"], "file-message-id")
        self.assertEqual(enqueued[0]["user_id"], "user-id")
        self.assertEqual(enqueued[0]["file_name"], "meeting.mp3")

    def test_line_text_status_query_replies_with_latest_user_job(self):
        import backend.main as main
        import backend.database as database
        import backend.line_handler as line_handler
        from linebot.v3.webhooks import (
            DeliveryContext,
            EventMode,
            MessageEvent,
            TextMessageContent,
            UserSource,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(database, "DB_PATH", Path(tmpdir) / "meetings.db"):
                database.init_db()
                database.create_job(
                    "line-status-job",
                    task_type="line_audio_processing",
                    source="line",
                    payload={
                        "message_id": "line-message-id",
                        "user_id": "user-id",
                        "model": "test-model",
                    },
                    message="LINE 媒體已接收，已排入可靠處理佇列。",
                )
                database.update_job_status(
                    "line-status-job",
                    "processing",
                    "📝 正在轉錄第 1/2 段音訊...",
                    progress_current=1,
                    progress_total=2,
                )

                event = MessageEvent(
                    source=UserSource(userId="user-id"),
                    timestamp=1,
                    mode=EventMode.ACTIVE,
                    webhookEventId="event-id",
                    deliveryContext=DeliveryContext(isRedelivery=False),
                    replyToken="reply-token",
                    message=TextMessageContent(id="text-message-id", text="狀態", quoteToken="quote-token"),
                )

                class FakeParser:
                    def parse(self, body, signature):
                        return [event]

                replies = []
                with mock.patch.object(line_handler, "get_webhook_parser", return_value=FakeParser()), \
                     mock.patch.object(line_handler, "get_line_api", return_value=object()), \
                     mock.patch.object(line_handler, "reply_text", side_effect=lambda api, token, text: replies.append(text)), \
                     mock.patch.object(line_handler, "process_line_text_in_background") as chat_handler:
                    response = asgi_request(
                        main.app,
                        "POST",
                        "/line-webhook",
                        headers={"X-Line-Signature": "sig"},
                        content=b'{"events":[]}',
                    )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(chat_handler.called)
        self.assertTrue(any("line-sta" in reply for reply in replies))
        self.assertTrue(any("處理中" in reply for reply in replies))
        self.assertTrue(any("1/2" in reply for reply in replies))

    def test_line_audio_flow_creates_job_and_pushes_returned_output_path(self):
        import backend.database as database
        import backend.line_handler as line_handler

        job_id = "line-regression-job"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            output_path = tmpdir_path / "meeting_notes_line.md"
            output_path.write_text("會議記錄內容", encoding="utf-8")

            pushed_messages = []

            with mock.patch.object(database, "DB_PATH", tmpdir_path / "meetings.db"):
                database.init_db()
                with database.get_db() as conn:
                    conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))

                with mock.patch.object(line_handler, "get_line_api", return_value=object()), \
                     mock.patch.object(line_handler, "download_line_audio", return_value=b"audio"), \
                     mock.patch.object(line_handler, "SOURCE_AUDIO_DIR", tmpdir_path), \
                     mock.patch.object(line_handler, "process_audio_task", return_value=output_path) as process_mock, \
                     mock.patch.object(line_handler, "push_text", side_effect=lambda api, user_id, text: pushed_messages.append(text)):
                    line_handler.process_line_audio_in_background(
                        job_id=job_id,
                        message_id="message-id",
                        user_id="user-id",
                        model="gemini-3.1-flash-lite",
                    )

                saved_audio = list(tmpdir_path.glob("line-reg_*.m4a"))
                self.assertEqual(len(saved_audio), 1)
                self.assertEqual(saved_audio[0].read_bytes(), b"audio")
                self.assertFalse(process_mock.call_args.kwargs["cleanup_source_audio"])

                self.assertIsNotNone(database.get_job(job_id))
                self.assertTrue(any("會議記錄內容" in msg for msg in pushed_messages))

                with database.get_db() as conn:
                    conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))

    def test_line_audio_flow_reuses_existing_audio_with_same_sha256(self):
        import backend.database as database
        import backend.line_handler as line_handler

        job_id = "line-dedup-job"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            output_path = tmpdir_path / "meeting_notes_line.md"
            output_path.write_text("line result", encoding="utf-8")
            existing_audio = tmpdir_path / "existing-source.m4a"
            existing_audio.write_bytes(b"audio")

            with mock.patch.object(database, "DB_PATH", tmpdir_path / "meetings.db"):
                database.init_db()
                with database.get_db() as conn:
                    conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))

                with mock.patch.object(line_handler, "get_line_api", return_value=object()), \
                     mock.patch.object(line_handler, "download_line_audio", return_value=b"audio"), \
                     mock.patch.object(line_handler, "SOURCE_AUDIO_DIR", tmpdir_path), \
                     mock.patch.object(line_handler, "process_audio_task", return_value=output_path) as process_mock, \
                     mock.patch.object(line_handler, "push_text"):
                    line_handler.process_line_audio_in_background(
                        job_id=job_id,
                        message_id="message-id",
                        user_id="user-id",
                        model="gemini-3.1-flash-lite",
                    )

                self.assertEqual(process_mock.call_args.kwargs["audio_path"], existing_audio)
                self.assertEqual(list(tmpdir_path.glob("*.m4a")), [existing_audio])
                self.assertFalse(list(tmpdir_path.glob(".upload_*")))

                with database.get_db() as conn:
                    conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))

    def test_line_delivery_message_excludes_full_transcript_and_points_to_output_path(self):
        import backend.line_handler as line_handler

        markdown = """---
title: 會議記錄
---

## 📋 一、討論摘要 (Discussion Summary)
摘要內容。

## ✅ 二、最終決議 (Final Decisions)
決議內容。

## 📌 三、待辦事項 (Action Items)
| # | 任務描述 | 負責人 | 期限 | 優先級 |
|---|---------|--------|------|--------|
| 1 | 整理需求 | 王經理 | 下週 | 高 |

## 📝 四、完整逐字稿 (Verbatim Transcript)
[00:00] **[發言者 A]**：這是一大段完整逐字稿，不應該推回 LINE。
"""

        output_path = Path("/tmp/output/meeting_notes.md")
        message = line_handler.build_line_meeting_delivery_message(
            markdown,
            output_path,
        )

        self.assertIn("摘要內容", message)
        self.assertIn("決議內容", message)
        self.assertIn("整理需求", message)
        self.assertIn("完整逐字稿已保存", message)
        self.assertIn(str(output_path), message)
        self.assertNotIn("這是一大段完整逐字稿", message)


class UiRegressionTests(unittest.TestCase):
    def test_regression_tests_do_not_import_deprecated_fastapi_testclient(self):
        source = (ROOT / "tests" / "test_regressions.py").read_text(encoding="utf-8")
        deprecated_import = "fastapi" + ".testclient"

        self.assertNotIn(deprecated_import, source)

    def test_desktop_gui_handles_backend_failed_status(self):
        source = (ROOT / "gui" / "app.py").read_text(encoding="utf-8")

        self.assertIn('status == "failed"', source)
        self.assertNotIn('status == "error"', source)

    def test_web_ui_can_cancel_active_backend_jobs(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn("/jobs/${jobId}/cancel", html)
        self.assertIn("activeUploadJobId", html)
        self.assertIn("activeRecordingJobId", html)

    def test_web_ui_has_job_dashboard(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="job-dashboard"', html)
        self.assertIn("async function loadJobs", html)
        self.assertIn("/jobs?limit=20", html)
        self.assertIn("任務狀態", html)

    def test_web_ui_has_metrics_panel_and_job_actions(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="ops-dashboard"', html)
        self.assertIn("async function loadMetrics", html)
        self.assertIn("/metrics", html)
        self.assertIn("/health", html)
        self.assertIn("health.status === 'ok'", html)
        self.assertNotIn("healthEl.textContent = failed ? '需注意' : '正常'", html)
        self.assertIn("/jobs/${jobId}/retry", html)
        self.assertIn("/jobs/${jobId}", html)
        self.assertIn("維運狀態", html)
        self.assertIn('id="ops-ngrok"', html)
        self.assertIn("LINE/ngrok", html)
        self.assertIn("data.ngrok", html)

    def test_web_ui_loads_runtime_config_and_prevents_oversized_uploads(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="upload-limit-hint"', html)
        self.assertIn("let runtimeConfig", html)
        self.assertIn("async function loadRuntimeConfig", html)
        self.assertIn("/config", html)
        self.assertIn("selectedFile.size > runtimeConfig.max_upload_bytes", html)
        self.assertIn("formatBytes(runtimeConfig.max_upload_bytes)", html)

    def test_web_ui_has_readable_api_errors_and_job_event_timeline(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn("async function apiErrorMessage", html)
        self.assertIn("payload.detail", html)
        self.assertIn('id="job-event-panel"', html)
        self.assertIn("async function loadJobEvents", html)
        self.assertIn("/jobs/${jobId}/events", html)
        self.assertIn("事件", html)

    def test_web_ui_can_upload_supplementary_evidence_for_a_meeting(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="evidence-file-input"', html)
        self.assertIn("async function uploadEvidence", html)
        self.assertIn("/meetings/${id}/evidence", html)
        self.assertIn("補充資料", html)

    def test_web_ui_can_record_screen_with_microphone(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="tab-screen"', html)
        self.assertIn("switchRecMode('screen')", html)
        self.assertIn("getDisplayMedia", html)
        self.assertIn("audio: true", html)
        self.assertIn("getUserMedia({ audio: true })", html)
        self.assertIn("...displayStream.getVideoTracks()", html)
        self.assertIn("displayStream.getAudioTracks()", html)
        self.assertIn("...micStream.getAudioTracks()", html)
        self.assertIn("createMediaStreamDestination", html)

    def test_frontend_smoke_script_checks_static_ui_and_upload_guard(self):
        smoke_script = ROOT / "scripts" / "smoke_e2e.sh"

        self.assertTrue(smoke_script.is_file())
        self.assertTrue(os.access(smoke_script, os.X_OK))

        script = smoke_script.read_text(encoding="utf-8")
        self.assertIn("BASE_URL", script)
        self.assertIn("/history", script)
        self.assertIn("ops-dashboard", script)
        self.assertIn("fake.mp3", script)
        self.assertIn("415", script)

    def test_python_docx_is_declared_as_runtime_dependency(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn("python-docx", requirements)

    def test_audioop_lts_is_only_installed_for_python_313_or_newer(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn('audioop-lts>=0.2.2; python_version >= "3.13"', requirements)


class TestSuiteQualityRegressionTests(unittest.TestCase):
    def test_root_test_modules_do_not_print_at_import_time(self):
        for filename in ("test_gemini.py", "test_regex.py"):
            source = (ROOT / filename).read_text(encoding="utf-8")

            self.assertNotIn("print(", source, msg=f"{filename} should be quiet during discovery")


class StartupScriptRegressionTests(unittest.TestCase):
    def test_startup_kills_existing_meeting_assistant_listener_before_launch(self):
        import start

        calls = []

        class Result:
            def __init__(self, stdout="", returncode=0):
                self.stdout = stdout
                self.returncode = returncode

        def fake_run(args, **kwargs):
            calls.append(args)
            if args[0] == "lsof":
                return Result("123\n456\n")
            if args[0] == "ps" and args[2] == "123":
                return Result("/usr/bin/python -m uvicorn backend.main:app --port 8001\n")
            if args[0] == "ps" and args[2] == "456":
                return Result("/usr/bin/python unrelated_server.py\n")
            if args[:2] == ["kill", "-0"]:
                return Result(returncode=1)
            return Result()

        with mock.patch.object(start.platform, "system", return_value="Darwin"), \
             mock.patch.object(start.os, "getpid", return_value=999), \
             mock.patch.object(start.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(start.time, "sleep"), \
             mock.patch("builtins.print"):
            start.terminate_existing_server(8001)

        self.assertIn(["kill", "-TERM", "123"], calls)
        self.assertNotIn(["kill", "-TERM", "456"], calls)

    def test_startup_script_uses_cleanup_before_uvicorn_launch(self):
        source = (ROOT / "start.py").read_text(encoding="utf-8")
        main_block = source[source.index('if __name__ == "__main__":') :]

        self.assertIn("terminate_existing_server(SERVER_PORT)", main_block)
        self.assertLess(
            main_block.index("terminate_existing_server(SERVER_PORT)"),
            main_block.index("subprocess.run(["),
        )

    def test_startup_can_build_mobile_history_url_with_api_key(self):
        import start

        url = start.mobile_history_url("192.168.1.20", 8001, "mobile secret")

        self.assertEqual(url, "http://192.168.1.20:8001/history?api_key=mobile%20secret")

    def test_startup_prints_direct_mobile_access_url_for_same_network(self):
        import start

        with mock.patch.dict(start.os.environ, {"APP_API_KEY": "mobile-key"}, clear=False), \
             mock.patch.object(start, "local_lan_ip", return_value="192.168.1.20"), \
             mock.patch("builtins.print") as printed:
            start.print_access_urls()

        output = "\n".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        self.assertIn("手機 / 平板：http://192.168.1.20:8001/history", output)
        self.assertNotIn("手機 / 平板：http://192.168.1.20:8001/history?api_key=", output)

    def test_startup_generates_temporary_api_key_when_missing(self):
        import start

        with mock.patch.dict(start.os.environ, {}, clear=True), \
             mock.patch.object(start.secrets, "token_urlsafe", return_value="generated-mobile-key"), \
             mock.patch("builtins.print"):
            key = start.ensure_app_api_key()
            env_key = start.os.environ["APP_API_KEY"]

        self.assertEqual(key, "generated-mobile-key")
        self.assertEqual(env_key, "generated-mobile-key")

    def test_startup_main_generates_api_key_before_mobile_urls_and_backend_launch(self):
        source = (ROOT / "start.py").read_text(encoding="utf-8")
        main_block = source[source.index('if __name__ == "__main__":') :]

        self.assertIn("ensure_app_api_key()", main_block)
        self.assertLess(
            main_block.index("ensure_app_api_key()"),
            main_block.index("print_access_urls()"),
        )
        self.assertLess(
            main_block.index("ensure_app_api_key()"),
            main_block.index("subprocess.run(["),
        )

    def test_startup_main_prints_mobile_access_urls(self):
        source = (ROOT / "start.py").read_text(encoding="utf-8")
        main_block = source[source.index('if __name__ == "__main__":') :]

        self.assertIn("print_access_urls()", main_block)

    def test_startup_launches_ngrok_with_configured_static_url(self):
        import start

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            pid_file = tmp_path / "ngrok.pid"
            log_file = tmp_path / "ngrok.log"

            process = mock.Mock()
            process.pid = 321

            with mock.patch.dict(
                start.os.environ,
                {
                    "MEETING_ASSISTANT_NGROK": "1",
                    "MEETING_ASSISTANT_NGROK_URL": "https://example.ngrok-free.app",
                },
                clear=False,
            ), \
                 mock.patch.object(start, "NGROK_PID_FILE", pid_file), \
                 mock.patch.object(start, "NGROK_LOG_FILE", log_file), \
                 mock.patch.object(start.shutil, "which", return_value="/usr/local/bin/ngrok"), \
                 mock.patch.object(start, "terminate_existing_ngrok"), \
                 mock.patch.object(start.subprocess, "Popen", return_value=process) as popen, \
                 mock.patch("builtins.print"):
                returned = start.start_ngrok_tunnel(8001, wait_for_status=False)
                pid_text = pid_file.read_text(encoding="utf-8")

        self.assertIs(returned, process)
        command = popen.call_args.args[0]
        self.assertEqual(command[:3], ["ngrok", "http", "8001"])
        self.assertIn("--url=https://example.ngrok-free.app", command)
        self.assertIn("--log=stdout", command)
        self.assertEqual(pid_text, "321")


if __name__ == "__main__":
    unittest.main()
