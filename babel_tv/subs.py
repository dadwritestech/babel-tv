"""Subtitle cues on the media timeline, and when to show them.

Cues come from speech segments, so their raw spans are exactly the speech.
Speech spans are too short to read (a 0.8 s "はい" is gone before your eyes
land on it), so display time is stretched: each cue stays up for at least a
reading time derived from its length, but never past the next cue's start.
"""
from __future__ import annotations

import bisect
import threading
import textwrap
from dataclasses import dataclass, field

# Reading speed for subtitles: ~15 characters/second is the usual broadcast
# guideline; Indic scripts render wider, but per-character speed is similar.
CPS = 15.0
MIN_SHOW_S = 1.2
MAX_SHOW_S = 7.0
LINGER_S = 1.5   # keep the last cue up this long past its speech
LINE_CHARS = 42


@dataclass(order=True)
class Cue:
    start: float
    end: float
    text: str = field(compare=False)
    source: str = field(default="", compare=False)
    ready_at: float = field(default=0.0, compare=False)  # wall clock when translated


def wrap(text: str, width: int = LINE_CHARS, max_lines: int = 2) -> str:
    lines = textwrap.wrap(" ".join(text.split()), width=width) or [""]
    if len(lines) > max_lines:
        # Keep the end of the sentence: in live captions the newest words matter most.
        lines = textwrap.wrap(" ".join(" ".join(lines).split()[-(width * max_lines // 6):]),
                              width=width)[-max_lines:]
    return "\n".join(lines)


def display_end(cue: Cue, next_start: float | None) -> float:
    reading = min(max(len(cue.text) / CPS, MIN_SHOW_S), MAX_SHOW_S)
    end = max(cue.end + LINGER_S, cue.start + reading)
    if next_start is not None:
        end = min(end, next_start)
    return max(end, cue.start)


class CueTrack:
    """Thread-safe, start-ordered cue list. Producers add; the player queries."""

    def __init__(self):
        self._cues: list[Cue] = []
        self._lock = threading.Lock()

    def add(self, cue: Cue) -> None:
        if not cue.text.strip():
            return
        with self._lock:
            bisect.insort(self._cues, cue)

    def __len__(self) -> int:
        return len(self._cues)

    def cues(self) -> list[Cue]:
        with self._lock:
            return list(self._cues)

    def active(self, t: float) -> Cue | None:
        with self._lock:
            i = bisect.bisect_right([c.start for c in self._cues], t) - 1
            if i < 0:
                return None
            cue = self._cues[i]
            nxt = self._cues[i + 1].start if i + 1 < len(self._cues) else None
        return cue if t < display_end(cue, nxt) else None

    def to_srt(self) -> str:
        cues = self.cues()
        out = []
        for i, c in enumerate(cues):
            nxt = cues[i + 1].start if i + 1 < len(cues) else None
            out.append(f"{i + 1}\n{srt_time(c.start)} --> {srt_time(display_end(c, nxt))}\n"
                       f"{wrap(c.text)}\n")
        return "\n".join(out)


def srt_time(t: float) -> str:
    ms = max(0, round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")
