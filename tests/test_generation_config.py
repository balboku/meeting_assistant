from __future__ import annotations

import unittest
from unittest import mock

from google.genai import types

from backend import tasks
from backend.generation_config import for_model


class GenerationConfigTests(unittest.TestCase):
    def test_gemini3_uses_default_sampling_and_preserves_other_settings(self):
        config = types.GenerateContentConfig(
            temperature=0.0, top_p=0.8, top_k=20,
            max_output_tokens=8192, response_mime_type="application/json",
        )
        for model in ("gemini-3.1-flash-lite", "gemini-3.5-flash", "models/gemini-3-flash-preview"):
            with self.subTest(model=model):
                result = for_model(model, config)
                self.assertIsNone(result.temperature)
                self.assertIsNone(result.top_p)
                self.assertIsNone(result.top_k)
                self.assertEqual(result.max_output_tokens, 8192)
                self.assertEqual(result.response_mime_type, "application/json")
        self.assertEqual(config.temperature, 0.0)
        self.assertEqual(config.top_p, 0.8)
        self.assertEqual(config.top_k, 20)

    def test_other_models_keep_existing_sampling(self):
        config = types.GenerateContentConfig(temperature=0.0, top_p=0.8)
        for model in ("gemini-2.5-flash", "gemma-4-31b-it", "gemini-30-test", "custom"):
            self.assertIs(for_model(model, config), config)

    def test_transcription_uses_actual_primary_model_settings(self):
        client = mock.Mock()
        config = types.GenerateContentConfig(temperature=0.0, top_p=0.8)
        tasks._generate_transcript_with_transient_fallback(
            client, model="gemini-3.1-flash-lite", contents=["audio"], config=config,
            job_id="test", segment_index=0, total_segments=1,
        )
        self.assertIsNone(client.models.generate_content.call_args.kwargs["config"].temperature)

    def test_transcription_fallback_reselects_settings_for_actual_model(self):
        client = mock.Mock()
        client.models.generate_content.side_effect = [RuntimeError("503 UNAVAILABLE"), "ok"]
        config = types.GenerateContentConfig(temperature=0.0, top_p=0.8)
        with mock.patch.object(tasks, '_resolve_transcription_recovery_model', return_value='gemini-3.5-flash'), \
             mock.patch.object(tasks, 'update_job_status'), \
             mock.patch.object(tasks, '_raise_if_cancelled'):
            result = tasks._generate_transcript_with_transient_fallback(
                client, model="gemini-2.5-flash", contents=["audio"], config=config,
                job_id="test", segment_index=0, total_segments=1,
            )
        calls = client.models.generate_content.call_args_list
        self.assertEqual(calls[0].kwargs["config"].temperature, 0.0)
        self.assertIsNone(calls[1].kwargs["config"].temperature)
        self.assertEqual(result, ("ok", "gemini-3.5-flash"))

    def test_summary_fallback_keeps_non_gemini_sampling(self):
        client = mock.Mock()
        client.models.generate_content.side_effect = [RuntimeError("failed"), "ok"]
        config = types.GenerateContentConfig(temperature=0.1, top_p=0.9)
        with mock.patch.object(tasks, 'update_job_status'):
            tasks._generate_text_with_fallback(
                client, primary_model="gemini-3.5-flash", fallback_model="gemma-4-31b-it",
                contents=["summary"], config=config, job_id="test", stage="test",
            )
        calls = client.models.generate_content.call_args_list
        self.assertIsNone(calls[0].kwargs["config"].temperature)
        self.assertIs(calls[1].kwargs["config"], config)


if __name__ == '__main__':
    unittest.main()
