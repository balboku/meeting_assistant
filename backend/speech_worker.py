"""Offline-only native inference entry point. No database, API or cloud imports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def infer_segments(samples, rate, paths):
    import sherpa_onnx as sherpa
    threads = min(4, max(1, int(os.getenv("LOCAL_SPEECH_THREADS", "2"))))
    config = sherpa.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa.OfflineSpeakerSegmentationPyannoteModelConfig(model=paths["segmentation"]),
            num_threads=threads, provider="cpu"),
        embedding=sherpa.SpeakerEmbeddingExtractorConfig(model=paths["embedding"], num_threads=threads, provider="cpu"),
        clustering=sherpa.FastClusteringConfig(num_clusters=-1, threshold=float(os.getenv("SPEAKER_CLUSTER_THRESHOLD", "0.9"))),
        min_duration_on=0.3, min_duration_off=0.5)
    if not config.validate():
        raise ValueError("Invalid diarization configuration")
    engine = sherpa.OfflineSpeakerDiarization(config)
    if rate != engine.sample_rate:
        raise ValueError("Sample rate mismatch")
    segments = engine.process(samples).sort_by_start_time()
    return [{"start": float(s.start), "end": float(s.end), "speaker": int(s.speaker)} for s in segments]


def transcribe(samples, rate, paths):
    import numpy as np
    import sherpa_onnx as sherpa
    from opencc import OpenCC
    engine = sherpa.OfflineRecognizer.from_whisper(encoder=paths["encoder"], decoder=paths["decoder"],
        tokens=paths["tokens"], language="", task="transcribe", num_threads=2, provider="cpu")
    converter, lines = OpenCC("s2t"), []
    for start in range(0, len(samples), 25 * rate):
        chunk = samples[start:start + 25 * rate]
        if not len(chunk) or float(np.sqrt(np.mean(chunk * chunk))) < 0.0005:
            continue
        stream = engine.create_stream()
        stream.accept_waveform(rate, chunk)
        engine.decode_stream(stream)
        text = converter.convert(stream.result.text.strip()) or "[聽不清]"
        seconds = start // rate
        lines.append(f"[{seconds // 60:02d}:{seconds % 60:02d}] **[發言者不明]**：{text}")
    if not lines:
        raise ValueError("No speech detected")
    return "\n\n".join(lines)


def main():
    import soundfile as sf
    operation, audio, output, paths_json = sys.argv[1:]
    paths = json.loads(paths_json)
    samples, rate = sf.read(audio, dtype="float32")
    if rate != 16000 or samples.ndim != 1:
        raise ValueError("Expected mono 16 kHz audio")
    if operation == "diarize":
        value = infer_segments(samples, rate, paths)
    elif operation == "transcribe":
        value = transcribe(samples, rate, paths)
    else:
        raise ValueError("Unsupported operation")
    Path(output).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
