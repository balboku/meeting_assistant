"""Fail-closed, sample-exact audio windows for opt-in transcription continuation.

All bounds are half-open positions on one decoded PCM sample clock. The caller
must verify source/configuration/cache identity and transcript quality before
constructing the plan. This path never invokes a general retry or a full upload.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import wave


class WindowMismatch(ValueError):
    """Stop before sending audio when coverage or artifact provenance disagrees."""


@dataclass(frozen=True, order=True)
class Window:
    start: int
    end: int

    def __post_init__(self):
        if type(self.start) is not int or type(self.end) is not int or not 0 <= self.start < self.end:
            raise WindowMismatch("Invalid half-open audio window")

    @property
    def frames(self):
        return self.end - self.start

    def overlaps(self, other):
        return self.start < other.end and other.start < self.end


def _union(windows):
    merged = []
    for window in sorted(windows):
        if merged and window.start <= merged[-1].end:
            merged[-1] = Window(merged[-1].start, max(window.end, merged[-1].end))
        else:
            merged.append(window)
    return tuple(merged)


@dataclass(frozen=True)
class GapPlan:
    extent: Window
    protected: tuple[Window, ...]
    missing: tuple[Window, ...]

    def __post_init__(self):
        if not self.protected or tuple(sorted(self.missing)) != self.missing:
            raise WindowMismatch("A continuation must retain verified coverage and ordered requests")
        cursor = self.extent.start
        for window in sorted((*self.protected, *self.missing)):
            if window.start != cursor:
                raise WindowMismatch("Verified and missing windows must partition the audio exactly")
            cursor = window.end
        if cursor != self.extent.end:
            raise WindowMismatch("Window plan does not end at the decoded audio boundary")

    @classmethod
    def build(cls, extent, verified, *, max_frames):
        protected = _union(verified)
        if not protected or type(max_frames) is not int or max_frames <= 0:
            raise WindowMismatch("Gap-only continuation requires verified coverage")
        if protected[0].start < extent.start or protected[-1].end > extent.end:
            raise WindowMismatch("Verified bounds are outside the decoded audio")
        missing = []

        def add(start, end):
            if start >= end:
                return
            # Balance the pieces so that a short remainder does not form an
            # unnecessarily tiny request at the end of a long gap.
            pieces = (end - start + max_frames - 1) // max_frames
            length = end - start
            missing.extend(Window(start + length*i//pieces, start + length*(i+1)//pieces)
                           for i in range(pieces))

        cursor = extent.start
        for window in protected:
            add(cursor, window.start)
            cursor = window.end
        add(cursor, extent.end)
        return cls(extent, protected, tuple(missing))

    def check_request(self, window):
        if (window not in self.missing or window == self.extent
                or any(window.overlaps(covered) for covered in self.protected)):
            raise WindowMismatch("Outbound audio would resend verified coverage or leave the plan")


def _file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class ExactClip:
    window: Window
    path: Path
    sha256: str
    rate: int

    def verify(self):
        if _file_sha256(self.path) != self.sha256:
            raise WindowMismatch("The reviewed outbound clip changed")
        with wave.open(str(self.path), "rb") as clip:
            if (clip.getframerate(), clip.getnchannels(), clip.getsampwidth(), clip.getnframes()) != (
                    self.rate, 1, 2, self.window.frames):
                raise WindowMismatch("Outbound clip is not exactly the planned sample range")


def cut_pcm(source: Path, window: Window, target: Path, *, rate: int) -> ExactClip:
    """Read only the specified PCM samples; never seek/keyframe-copy encoded input."""
    with wave.open(str(source), "rb") as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (rate, 1, 2):
            raise WindowMismatch("Unexpected decoded PCM format")
        if window.end > stream.getnframes():
            raise WindowMismatch("Planned window exceeds decoded audio")
        stream.setpos(window.start)
        pcm = stream.readframes(window.frames)
    if len(pcm) != window.frames * 2:
        raise WindowMismatch("Decoded PCM window is truncated")
    with Path(target).open("xb") as handle, wave.open(handle, "wb") as clip:
        clip.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        clip.writeframes(pcm)
    return ExactClip(window, Path(target), _file_sha256(target), rate)


def run_gap_plan(plan, clips, *, revalidate, transcribe, accept, previously_dispatched=()):
    """Preflight the whole plan, then send only missing windows not dispatched earlier."""
    clips = tuple(clips)
    dispatched = frozenset(previously_dispatched)
    if tuple(clip.window for clip in clips) != plan.missing or len(set(plan.missing)) != len(clips):
        raise WindowMismatch("Missing, duplicate or reordered outbound windows")
    if not dispatched.issubset(plan.missing):
        raise WindowMismatch("Prior request ledger does not match this gap plan")
    revalidate()
    for clip in clips:
        plan.check_request(clip.window)
        clip.verify()
    for clip in clips:
        if clip.window in dispatched:
            continue
        revalidate()
        plan.check_request(clip.window)
        clip.verify()
        accept(clip, transcribe(clip))
