"""Evaluate human references against predictions offline; never calls a model."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from pathlib import Path


SYNTHETIC = "synthetic_algorithm_fixture_not_audio_accuracy"
SCORE_METRICS = {"mixed_token_error_rate", "chinese_cer", "english_wer",
                 "term_recall", "number_sequence_error_rate", "der"}


def _finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _validate_case(case):
    if not isinstance(case, dict):
        raise ValueError("Each case must be an object")
    for key in ("name", "reference", "prediction"):
        if not isinstance(case.get(key), str) or not case[key].strip():
            raise ValueError(f"Case {key} must be a non-empty string")
    terms = case.get("terms", [])
    if not isinstance(terms, list) or any(not isinstance(t, str) or not t.strip() for t in terms):
        raise ValueError("Case terms must be a list of non-empty strings")
    limits = {}
    for direction in ("maximum", "minimum"):
        rules = case.get(direction, {})
        if not isinstance(rules, dict):
            raise ValueError(f"Case {direction} thresholds must be an object")
        for metric, value in rules.items():
            if metric not in SCORE_METRICS:
                raise ValueError(f"Unknown metric: {metric}")
            if not _finite_number(value) or value < 0 or (metric == "term_recall" and value > 1):
                raise ValueError(f"Invalid threshold for {metric}")
        limits[direction] = rules
    if not any(limits.values()):
        raise ValueError("At least one quality threshold is required")
    for metric in limits["maximum"].keys() & limits["minimum"].keys():
        if limits["minimum"][metric] > limits["maximum"][metric]:
            raise ValueError(f"Contradictory thresholds for {metric}")
    return limits


def _validate_timeline(segments):
    if not isinstance(segments, list):
        raise ValueError("Invalid speaker timeline: expected a list")
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("Invalid speaker interval: expected an object")
        start, end, speaker = (segment.get(key) for key in ("start", "end", "speaker"))
        if (not _finite_number(start) or not _finite_number(end) or not 0 <= start < end
                or type(speaker) not in (str, int) or not str(speaker).strip()):
            raise ValueError("Invalid speaker interval or label")


def _audio_identity(document):
    value = document.get("audio_sha256")
    if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", value):
        raise ValueError("Human-reference datasets require valid audio_sha256 in both files")
    return value.lower()


def distance(reference, prediction):
    row = list(range(len(prediction) + 1))
    for i, expected in enumerate(reference, 1):
        next_row = [i]
        for j, actual in enumerate(prediction, 1):
            next_row.append(min(row[j] + 1, next_row[-1] + 1, row[j - 1] + (expected != actual)))
        row = next_row
    return row[-1]


def text_metrics(reference, prediction, terms=()):
    def tokens(text):
        return re.findall(r"[\u3400-\u9fff]|[a-z]+(?:'[a-z]+)?|\d+(?:\.\d+)?", unicodedata.normalize("NFKC", text).lower())
    ref, hyp = tokens(reference), tokens(prediction)
    def score(a, b):
        return distance(a, b) / len(a) if a else (0.0 if not b else None)
    chinese = lambda values: [v for v in values if re.fullmatch(r"[\u3400-\u9fff]", v)]
    english = lambda values: [v for v in values if re.fullmatch(r"[a-z]+(?:'[a-z]+)?", v)]
    numbers = lambda text: re.findall(r"\d+(?:\.\d+)?%?", text)
    expected_terms = [term for term in terms if term in reference]
    expected_numbers, actual_numbers = numbers(reference), numbers(prediction)
    return {"mixed_token_error_rate": score(ref, hyp), "chinese_cer": score(chinese(ref), chinese(hyp)),
            "english_wer": score(english(ref), english(hyp)),
            "term_recall": sum(prediction.count(t) >= reference.count(t) for t in expected_terms) / len(expected_terms) if expected_terms else None,
            "number_sequence_error_rate": score(expected_numbers, actual_numbers),
            "reference_tokens": len(ref), "prediction_tokens": len(hyp)}


def best_mapping(weights):
    """Maximum-weight assignment (Hungarian); anonymous label spelling is irrelevant."""
    n = len(weights)
    if not n:
        return {}
    u, v, p, way = [0.0] * (n + 1), [0.0] * (n + 1), [0] * (n + 1), [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0, minimum, used = 0, [math.inf] * (n + 1), [False] * (n + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], math.inf, 0
            for j in range(1, n + 1):
                if not used[j]:
                    cost = -weights[i0 - 1][j - 1] - u[i0] - v[j]
                    if cost < minimum[j]:
                        minimum[j], way[j] = cost, j0
                    if minimum[j] < delta:
                        delta, j1 = minimum[j], j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minimum[j] -= delta
            j0 = j1
            if not p[j0]:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if not j0:
                break
    return {p[j] - 1: j - 1 for j in range(1, n + 1)}


def diarization_error(reference, prediction):
    for segment in reference + prediction:
        if not (math.isfinite(segment["start"]) and math.isfinite(segment["end"]) and 0 <= segment["start"] < segment["end"]):
            raise ValueError("Invalid speaker interval")
    ref_labels = sorted({str(s["speaker"]) for s in reference})
    hyp_labels = sorted({str(s["speaker"]) for s in prediction})
    boundaries = sorted({float(s[k]) for s in reference + prediction for k in ("start", "end")})
    intervals = []
    size = max(len(ref_labels), len(hyp_labels))
    weights = [[0.0] * size for _ in range(size)]
    for start, end in zip(boundaries, boundaries[1:]):
        r = {ref_labels.index(str(s["speaker"])) for s in reference if s["start"] <= start < s["end"]}
        h = {hyp_labels.index(str(s["speaker"])) for s in prediction if s["start"] <= start < s["end"]}
        intervals.append((end - start, r, h))
        for ri in r:
            for hi in h:
                weights[ri][hi] += end - start
    mapping = best_mapping(weights)
    total = missed = false_alarm = confusion = 0.0
    for duration, r, h in intervals:
        correct = sum(mapping[ri] in h for ri in r)
        total += duration * len(r)
        missed += duration * max(0, len(r) - len(h))
        false_alarm += duration * max(0, len(h) - len(r))
        confusion += duration * (min(len(r), len(h)) - correct)
    return {"der": (missed + false_alarm + confusion) / total if total else (0 if not prediction else None),
            "missed_seconds": missed, "false_alarm_seconds": false_alarm, "confused_seconds": confusion,
            "reference_speaker_seconds": total, "collar_seconds": 0, "overlap_included": True}


def evaluate_case(case, folder):
    limits = _validate_case(case)
    reference_path, prediction_path = folder / case["reference"], folder / case["prediction"]
    if reference_path.resolve() == prediction_path.resolve() or reference_path.samefile(prediction_path):
        raise ValueError("Reference and prediction must be separate files")
    reference_bytes, prediction_bytes = reference_path.read_bytes(), prediction_path.read_bytes()
    reference, prediction = json.loads(reference_bytes), json.loads(prediction_bytes)
    if not isinstance(reference, dict) or not isinstance(prediction, dict):
        raise ValueError("Reference and prediction must be objects")
    synthetic = reference.get("dataset_kind") == SYNTHETIC
    if not synthetic and reference.get("human_verified") is not True:
        raise ValueError("Reference must be human verified; an AI transcript is not ground truth")
    audio_sha256 = reference.get("audio_sha256")
    if not synthetic:
        audio_sha256 = _audio_identity(reference)
        if audio_sha256 != _audio_identity(prediction):
            raise ValueError("Reference and prediction must describe the same audio")
    if any(not isinstance(document.get("text"), str) for document in (reference, prediction)):
        raise ValueError("Reference and prediction text must be strings")
    metrics = text_metrics(reference["text"], prediction["text"], case.get("terms", []))
    if not metrics["reference_tokens"]:
        raise ValueError("Reference must contain non-empty, scorable speech text")
    requested = limits["maximum"].keys() | limits["minimum"].keys()
    normalized = unicodedata.normalize("NFKC", reference["text"]).lower()
    for metric, pattern in (("chinese_cer", r"[\u3400-\u9fff]"),
                            ("english_wer", r"[a-z]"),
                            ("number_sequence_error_rate", r"\d")):
        if metric in requested and not re.search(pattern, normalized):
            raise ValueError(f"No reference evidence for threshold metric: {metric}")
    requires_der = any("der" in rules for rules in limits.values())
    if requires_der and not reference.get("speakers"):
        raise ValueError("A DER threshold requires a non-empty reference speaker timeline")
    for document in (reference, prediction):
        if "speakers" in document:
            _validate_timeline(document["speakers"])
    if "speakers" in reference:
        metrics.update(diarization_error(reference["speakers"], prediction.get("speakers", [])))
    failures = [name for name, maximum in limits["maximum"].items()
                if not _finite_number(metrics.get(name)) or metrics[name] > maximum]
    failures += [name for name, minimum in limits["minimum"].items()
                 if not _finite_number(metrics.get(name)) or metrics[name] < minimum]
    return {"name": case["name"], "metrics": metrics, "failures": failures,
            "reference_sha256": hashlib.sha256(reference_bytes).hexdigest(),
            "prediction_sha256": hashlib.sha256(prediction_bytes).hexdigest(),
            "audio_sha256": audio_sha256, "human_reference": not synthetic,
            "dataset_kind": reference.get("dataset_kind", "human_verified_audio")}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        cases = json.loads(args.manifest.read_bytes())
        if not isinstance(cases, list) or not cases:
            raise ValueError("Manifest must be a non-empty list of cases")
        names = set()
        for case in cases:
            _validate_case(case)
            name = case["name"].strip()
            if name in names:
                raise ValueError(f"Duplicate case name: {name}")
            names.add(name)
        results = [evaluate_case(case, args.manifest.parent) for case in cases]
        human_count = sum(r["human_reference"] for r in results)
        report = {"results": results, "passed": all(not r["failures"] for r in results),
                  "human_reference_case_count": human_count,
                  "synthetic_case_count": len(results) - human_count,
                  "evaluation_scope": ("synthetic_algorithm_only" if not human_count else
                                       "human_reference_comparison" if human_count == len(results) else "mixed")}
        rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    except (ValueError, OSError) as error:
        print(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(rendered)
    return int(any(r["failures"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
