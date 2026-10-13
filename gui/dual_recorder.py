"""WASAPI loopback + microphone capture with independent, recoverable local tracks."""
from __future__ import annotations

import ctypes
import json
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf


def available_devices():
    import soundcard as sc
    return {"speakers": [{"id": d.id, "name": d.name} for d in sc.all_speakers()],
            "microphones": [{"id": d.id, "name": d.name} for d in sc.all_microphones()],
            "default_speaker": getattr(sc.default_speaker(), "id", None),
            "default_microphone": getattr(sc.default_microphone(), "id", None)}


class DualRecorder:
    def __init__(self, directory, *, sample_rate=48000, provider=None):
        self.root = Path(directory)
        self.sample_rate = sample_rate
        self.provider = provider
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.threads = []
        self.state = {"status": "idle", "tracks": {}, "errors": []}
        self.folder = None

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def _persist(self):
        if self.folder:
            path = self.folder / "recording.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)

    def start(self, *, speaker_id, microphone_id=None):
        if any(thread.is_alive() for thread in self.threads):
            raise RuntimeError("錄音仍在進行中。")
        if not speaker_id:
            raise ValueError("請選擇同仁聲音播放的喇叭或耳機。")
        self.folder = self.root / (time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8])
        self.folder.mkdir(parents=True, exist_ok=False)
        self.stop_event.clear()
        self.started = time.monotonic()
        self.state = {"status": "starting", "sample_rate": self.sample_rate, "tracks": {}, "errors": []}
        self.threads = []
        for name, device_id, loopback in [("system", speaker_id, True), ("microphone", microphone_id, False)]:
            if not device_id:
                continue
            self.state["tracks"][name] = {"device_id": device_id, "frames": 0, "peak": 0, "last_signal": None}
            worker = threading.Thread(target=self._capture, args=(name, device_id, loopback), daemon=True)
            self.threads.append(worker)
        # Verify metadata can be written before any audio device is activated.
        self._persist()
        for worker in self.threads:
            worker.start()

    def _capture(self, name, device_id, loopback):
        initialized = False
        try:
            if self.provider is None:
                result = ctypes.windll.ole32.CoInitializeEx(None, 0)
                initialized = result in (0, 1)
                import soundcard as sc
                device = sc.get_microphone(device_id, include_loopback=loopback)
            else:
                device = self.provider(device_id, loopback)
            # Record all physical channels: requesting mono through WASAPI can
            # corrupt input on some devices. Downmix explicitly after capture.
            with device.recorder(samplerate=self.sample_rate, channels=device.channels,
                                 blocksize=self.sample_rate // 10) as stream, sf.SoundFile(
                    self.folder / f"{name}.wav", "x", samplerate=self.sample_rate, channels=1, subtype="PCM_16") as output:
                with self.lock:
                    self.state["tracks"][name]["offset_seconds"] = time.monotonic() - self.started
                    self.state["status"] = "recording"
                while not self.stop_event.is_set():
                    data = stream.record(numframes=self.sample_rate // 10)
                    mono = np.asarray(data, dtype=np.float32).mean(axis=1)
                    if not np.isfinite(mono).all():
                        raise ValueError("錄音裝置傳回無效聲音資料。")
                    output.write(mono)
                    output.flush()
                    peak = float(np.max(np.abs(mono))) if len(mono) else 0
                    with self.lock:
                        track = self.state["tracks"][name]
                        track["frames"] += len(mono)
                        track["peak"] = peak
                        if peak > 0.005:
                            track["last_signal"] = time.monotonic() - self.started
        except Exception as exc:
            with self.lock:
                self.state["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
                self.state["status"] = "error"
            self.stop_event.set()
        finally:
            if initialized:
                ctypes.windll.ole32.CoUninitialize()

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=5)
        with self.lock:
            if any(thread.is_alive() for thread in self.threads):
                self.state["errors"].append("錄音裝置未回應停止指令；原始檔已保留，請重新開啟錄音程式。")
            self.state["status"] = "error" if self.state["errors"] else "saved"
        self._persist()
        return self.snapshot()

    def mix(self):
        state = self.snapshot()
        if state["status"] != "saved":
            raise RuntimeError("錄音尚未完整停止，請檢查原始檔。")
        if not state["tracks"]["system"]["last_signal"]:
            raise RuntimeError("未偵測到系統聲音。檔案已保留，請確認播放裝置或直接檢查原始錄音。")
        from contextlib import ExitStack
        import audioop
        destination = self.folder / "meeting.flac"
        temporary = self.folder / f"meeting-{uuid.uuid4().hex}.partial.flac"
        if destination.exists():
            raise FileExistsError("合併檔已存在，請直接使用既有檔案。")
        # FLAC is seekable and compressed without a separately installed encoder.
        # Original tracks remain untouched, with timing offsets recorded above.
        with ExitStack() as stack:
            tracks = [(stack.enter_context(sf.SoundFile(self.folder / f"{name}.wav")),
                       round(track.get("offset_seconds", 0) * self.sample_rate)) for name, track in state["tracks"].items()]
            output = stack.enter_context(sf.SoundFile(temporary, "x", samplerate=16000, channels=1, format="FLAC", subtype="PCM_16"))
            length = max(len(source) + offset for source, offset in tracks)
            resample_state = None
            for start in range(0, length, self.sample_rate):
                size = min(self.sample_rate, length - start)
                mixed = np.zeros(size, dtype=np.float32)
                for source, offset in tracks:
                    first, last = max(start, offset), min(start + size, offset + len(source))
                    if last > first:
                        source.seek(first - offset)
                        mixed[first - start:last - start] += source.read(last - first, dtype="float32") / len(tracks)
                pcm = (np.clip(mixed, -1, 1) * 32767).astype("<i2").tobytes()
                converted, resample_state = audioop.ratecv(pcm, 2, 1, self.sample_rate, 16000, resample_state)
                output.write(np.frombuffer(converted, dtype="<i2"))
        # A failed merge retains its partial file and original WAVs for diagnosis;
        # only a fully closed FLAC is published as the upload-ready file.
        temporary.rename(destination)
        return destination
