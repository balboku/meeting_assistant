"""Offline benchmark gates must never manufacture a passing accuracy claim."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_speech import evaluate_case


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = "synthetic_algorithm_fixture_not_audio_accuracy"


class SpeechBenchmarkInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.reference = {
            "human_verified": True,
            "audio_sha256": "a" * 64,
            "text": "Mercury 的報告有 12 件。Review Friday.",
            "speakers": [{"start": 0, "end": 4, "speaker": "A"}],
        }
        self.prediction = {
            "audio_sha256": "a" * 64,
            "text": self.reference["text"],
            "speakers": [{"start": 0, "end": 4, "speaker": "B"}],
        }
        self.case = {
            "name": "Human-checked fixture (not real audio)",
            "reference": "reference.json",
            "prediction": "prediction.json",
            "terms": ["Mercury"],
            "maximum": {"mixed_token_error_rate": 0, "der": 0},
            "minimum": {"term_recall": 1},
        }

    def write_json(self, name, value):
        path = self.folder / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def evaluate(self):
        self.write_json("reference.json", self.reference)
        self.write_json("prediction.json", self.prediction)
        return evaluate_case(self.case, self.folder)

    def cli(self, cases):
        self.write_json("reference.json", self.reference)
        self.write_json("prediction.json", self.prediction)
        manifest = self.write_json("cases.json", cases)
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(ROOT / "scripts/evaluate_speech.py"), str(manifest)],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )

    def test_valid_reference_keeps_scores_and_hash_receipts(self):
        result = self.evaluate()
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["metrics"]["der"], 0)
        self.assertEqual(len(result["reference_sha256"]), 64)
        self.assertEqual(result["audio_sha256"], "a" * 64)
        self.assertTrue(result["human_reference"])

    def test_confirmation_requires_actual_boolean_true(self):
        for value in (False, "false", "true", 1, [], None):
            with self.subTest(value=value):
                self.reference["human_verified"] = value
                with self.assertRaisesRegex(ValueError, "human verified"):
                    self.evaluate()

    def test_real_references_require_matching_audio_identity(self):
        for document in (self.reference, self.prediction):
            original = document["audio_sha256"]
            for value in (None, "", "a" * 63, "z" * 64, False):
                with self.subTest(value=value, document=document is self.reference):
                    document["audio_sha256"] = value
                    with self.assertRaisesRegex(ValueError, "audio_sha256"):
                        self.evaluate()
            document["audio_sha256"] = original
        self.prediction["audio_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "same audio"):
            self.evaluate()

    def test_blank_or_non_string_reference_cannot_pass(self):
        for value in ("", "  \n", "。!!!", None, []):
            with self.subTest(value=value):
                self.reference["text"] = value
                with self.assertRaises(ValueError):
                    self.evaluate()

    def test_empty_prediction_is_scored_as_missing_speech(self):
        self.prediction["text"] = ""
        self.prediction.pop("speakers")
        result = self.evaluate()
        self.assertEqual(result["metrics"]["mixed_token_error_rate"], 1)
        self.assertEqual(result["metrics"]["der"], 1)
        self.assertIn("der", result["failures"])
        self.assertIn("mixed_token_error_rate", result["failures"])

    def test_missing_rules_and_unknown_metrics_are_invalid(self):
        self.case["maximum"] = {}
        self.case["minimum"] = {}
        with self.assertRaisesRegex(ValueError, "threshold"):
            self.evaluate()
        self.case["maximum"] = {"english_wre": 0}
        with self.assertRaisesRegex(ValueError, "Unknown metric"):
            self.evaluate()

    def test_non_finite_negative_and_boolean_limits_are_invalid(self):
        for value in (float("nan"), float("inf"), -0.1, True, "0", None):
            with self.subTest(value=value):
                self.case["maximum"]["der"] = value
                with self.assertRaisesRegex(ValueError, "threshold"):
                    self.evaluate()
        self.case["maximum"]["der"] = 0
        self.case["minimum"]["term_recall"] = 1.1
        with self.assertRaises(ValueError):
            self.evaluate()

    def test_contradictory_limits_are_invalid(self):
        self.case["minimum"]["der"] = 0.5
        with self.assertRaisesRegex(ValueError, "threshold"):
            self.evaluate()

    def test_der_threshold_requires_a_reference_timeline(self):
        for value in (None, [], "A"):
            with self.subTest(value=value):
                self.reference["speakers"] = value
                with self.assertRaisesRegex(ValueError, "speaker"):
                    self.evaluate()
        self.reference.pop("speakers")
        with self.assertRaisesRegex(ValueError, "speaker"):
            self.evaluate()

    def test_invalid_timelines_are_rejected_not_scored(self):
        for segment in (
            {"start": 0, "end": float("nan"), "speaker": "A"},
            {"start": False, "end": 4, "speaker": "A"},
            {"start": 4, "end": 4, "speaker": "A"},
            {"start": -1, "end": 4, "speaker": "A"},
            {"start": 0, "end": 4, "speaker": None},
            {"start": 0, "end": 4, "speaker": " "},
            {"start": 0, "end": 4},
        ):
            for document in (self.reference, self.prediction):
                with self.subTest(segment=segment, reference=document is self.reference):
                    original = document["speakers"]
                    document["speakers"] = [segment]
                    with self.assertRaisesRegex(ValueError, "speaker"):
                        self.evaluate()
                    document["speakers"] = original

    def test_same_file_reference_is_rejected(self):
        self.case["prediction"] = "reference.json"
        with self.assertRaisesRegex(ValueError, "separate files"):
            self.evaluate()

    def test_malformed_case_data_is_rejected(self):
        for key, value in (("name", ""), ("reference", None), ("prediction", []),
                           ("terms", "Mercury"), ("terms", [""]), ("maximum", [])):
            with self.subTest(key=key, value=value):
                original = copy.deepcopy(self.case)
                self.case[key] = value
                with self.assertRaises(ValueError):
                    self.evaluate()
                self.case = original

    def test_cli_empty_and_non_list_manifests_fail_with_json(self):
        for manifest in ([], {}, None, [None]):
            with self.subTest(manifest=manifest):
                result = self.cli(manifest)
                self.assertEqual(result.returncode, 2, result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report["passed"])
                self.assertIn("error", report)
                self.assertNotIn("Traceback", result.stderr)

    def test_cli_distinguishes_synthetic_from_human_reference_comparison(self):
        human = self.cli([self.case])
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertEqual(json.loads(human.stdout)["human_reference_case_count"], 1)
        self.reference["human_verified"] = False
        self.reference["dataset_kind"] = SYNTHETIC
        self.reference.pop("audio_sha256")
        self.prediction.pop("audio_sha256")
        synthetic = self.cli([self.case])
        self.assertEqual(synthetic.returncode, 0, synthetic.stderr)
        report = json.loads(synthetic.stdout)
        self.assertEqual(report["human_reference_case_count"], 0)
        self.assertEqual(report["synthetic_case_count"], 1)
        self.assertEqual(report["evaluation_scope"], "synthetic_algorithm_only")

    def test_cli_bad_prediction_fails_without_losing_metrics(self):
        self.prediction["text"] = "錯誤"
        result = self.cli([self.case])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(json.loads(result.stdout)["passed"])

    def test_cli_rejects_duplicate_case_names(self):
        result = self.cli([self.case, self.case])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(json.loads(result.stdout)["passed"])

    def test_document_roots_and_prediction_text_are_validated(self):
        self.prediction["text"] = None
        with self.assertRaisesRegex(ValueError, "text must be strings"):
            self.evaluate()
        self.reference = []
        with self.assertRaisesRegex(ValueError, "must be objects"):
            self.evaluate()

    def test_audio_identity_is_case_insensitive_hex(self):
        self.prediction["audio_sha256"] = "A" * 64
        self.assertEqual(self.evaluate()["failures"], [])

    def test_language_or_number_threshold_requires_matching_evidence(self):
        for metric, text in (("chinese_cer", "Only English"),
                             ("english_wer", "只有中文"),
                             ("number_sequence_error_rate", "沒有數字")):
            with self.subTest(metric=metric):
                self.reference["text"] = self.prediction["text"] = text
                self.case["maximum"] = {metric: 0}
                self.case["minimum"] = {}
                with self.assertRaisesRegex(ValueError, "No reference evidence"):
                    self.evaluate()

    def test_cli_bad_json_and_missing_files_are_not_passes(self):
        self.case["reference"] = "missing.json"
        result = self.cli([self.case])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(json.loads(result.stdout)["passed"])
        self.write_json("bad.json", {})
        (self.folder / "bad.json").write_text("{", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(ROOT / "scripts/evaluate_speech.py"),
             str(self.folder / "bad.json")],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(json.loads(result.stdout)["passed"])


if __name__ == "__main__":
    unittest.main()
