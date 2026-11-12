"""Local scene ranking; only timestamps leave this selector."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import math
import re


WIDTH, HEIGHT = 64, 36
SAMPLE_HZ = 2
MAX_SCAN_SECONDS = 3 * 60 * 60
MAX_REQUEST_IMAGES = 24  # Hard safety ceiling, not the number to select.
_SIZE = WIDTH * HEIGHT
_PTS = re.compile(rb"\bpts_time:([0-9]+(?:\.[0-9]+)?)")
_QUANTIZE = bytes(value // 12 for value in range(256))


@dataclass(frozen=True)
class Scene:
    second: int
    duration: float
    layout: float
    thumbnail: bytes

    @property
    def score(self) -> float:
        # A brief stable, structured page is more useful than a similar page
        # that is visible for minutes. Do not promote single-sample flashes.
        bonus = 0.65 + (8 - self.duration) * 0.08 if self.layout >= 0.4 and self.duration <= 8 else 0
        return self.layout + bonus


def _changed(a: bytes, b: bytes) -> bool:
    if a == b:
        return False
    # A stride coprime to the width samples all column phases across rows.
    differences = [abs(x - y) for x, y in zip(a[::3], b[::3])]
    return sum(differences) >= 4 * len(differences) or sum(d >= 22 for d in differences) >= len(differences) / 25


def _layout(image: bytes) -> float:
    # Density and alignment of visible edges are local heuristics, not OCR.
    # Table rows/columns score well; ordinary dark slides can still qualify.
    rows = [0] * HEIGHT
    columns = [0] * WIDTH
    edges = 0
    for index, value in enumerate(image):
        row, col = divmod(index, WIDTH)
        if row and abs(value - image[index - WIDTH]) >= 32:
            rows[row] += 1
            edges += 1
        if col and abs(value - image[index - 1]) >= 32:
            columns[col] += 1
            edges += 1
    aligned = sum(n >= WIDTH / 3 for n in rows) + sum(n >= HEIGHT / 3 for n in columns)
    return min(0.9, edges / 360) + min(0.6, aligned / 20)


def _scenes(thumbs: bytes, times: list[float]) -> list[Scene]:
    def frame(i: int) -> bytes:
        return thumbs[i * _SIZE:(i + 1) * _SIZE]

    unique: dict[bytes, Scene] = {}

    def add(start: int, stop: int):
        first, last = times[start], times[stop - 1]
        # The Markdown/player contract uses whole seconds. Seek inside the
        # observed stable interval, never a fixed offset past a transition.
        second = max(math.ceil(first), math.floor((first + last) / 2))
        if second > last or (stop - start < 2 and len(times) > 1):
            return
        index = min(stop - 1, start + round((second - first) * SAMPLE_HZ))
        image = frame(index)
        if max(image) - min(image) < 24 or sum(image) < _SIZE * 20:
            return
        key = hashlib.blake2s(image.translate(_QUANTIZE), digest_size=12).digest()
        duration = last - first + 1 / SAMPLE_HZ
        if previous := unique.get(key):
            # A slide seen for a long time anywhere is not a brief exclusive
            # opportunity merely because it reappears for a moment.
            unique[key] = replace(previous, duration=max(duration, previous.duration))
        else:
            unique[key] = Scene(second, duration, _layout(image), image)

    if not times:
        return []
    start, anchor = 0, frame(0)
    for i in range(1, len(times)):
        current = frame(i)
        if _changed(anchor, current):
            add(start, i)
            start, anchor = i, current
    add(start, len(times))
    return list(unique.values())


def _rank(scenes: list[Scene], duration: float) -> list[int]:
    budget = min(len(scenes), MAX_REQUEST_IMAGES,
                 4 + math.ceil(duration / 300) + math.ceil(math.sqrt(len(scenes))))
    if not budget:
        return []
    remaining = sorted(scenes, key=lambda s: (s.score, -s.second), reverse=True)
    chosen: list[Scene] = []
    while remaining and len(chosen) < budget:
        # Favor clear, short scenes, then spread among the rest so one busy
        # agenda item cannot exhaust the entire recording's image budget.
        if chosen:
            spacing = max(10.0, duration / budget)
            candidate = max(remaining, key=lambda s: s.score + 0.35 * min(
                1, min(abs(s.second - c.second) for c in chosen) / spacing))
        else:
            candidate = remaining[0]
        chosen.append(candidate)
        remaining = [scene for scene in remaining if _changed(scene.thumbnail, candidate.thumbnail)]
    return [scene.second for scene in chosen]


def select_scene_times(thumbs: bytes, stderr: bytes) -> list[int]:
    """Return ranked, deduplicated timestamps, highest priority first."""
    times = [float(value) for value in _PTS.findall(stderr)]
    if len(thumbs) != len(times) * _SIZE or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("frame timestamps do not match chronological thumbnails")
    times = [seconds for seconds in times if seconds < MAX_SCAN_SECONDS]
    duration = times[-1] + 1 / SAMPLE_HZ if times else 0
    return _rank(_scenes(thumbs, times), duration)
