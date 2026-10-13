"""Evaluate human references against predictions offline; never calls a model."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path


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
    reference_path, prediction_path = folder / case["reference"], folder / case["prediction"]
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    prediction = json.loads(prediction_path.read_text(encoding="utf-8"))
    if not reference.get("human_verified") and reference.get("dataset_kind") != "synthetic_algorithm_fixture_not_audio_accuracy":
        raise ValueError("Reference must be human verified; an AI transcript is not ground truth")
    if reference_path.resolve() == prediction_path.resolve():
        raise ValueError("Reference and prediction must be separate files")
    metrics = text_metrics(reference["text"], prediction["text"], case.get("terms", []))
    if "speakers" in reference:
        metrics.update(diarization_error(reference["speakers"], prediction.get("speakers", [])))
    failures = [name for name, maximum in case.get("maximum", {}).items() if metrics.get(name) is None or metrics[name] > maximum]
    failures += [name for name, minimum in case.get("minimum", {}).items() if metrics.get(name) is None or metrics[name] < minimum]
    return {"name": case["name"], "metrics": metrics, "failures": failures,
            "reference_sha256": hashlib.sha256(reference_path.read_bytes()).hexdigest(),
            "prediction_sha256": hashlib.sha256(prediction_path.read_bytes()).hexdigest(),
            "audio_sha256": reference.get("audio_sha256"), "dataset_kind": reference.get("dataset_kind", "unspecified")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    cases = json.loads(args.manifest.read_text(encoding="utf-8"))
    results = [evaluate_case(case, args.manifest.parent) for case in cases]
    print(json.dumps({"results": results, "passed": all(not r["failures"] for r in results)}, ensure_ascii=False, indent=2))
    return int(any(r["failures"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
