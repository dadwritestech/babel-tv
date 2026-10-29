"""Turn a live 16 kHz PCM stream into speech segments.

The chunker is pure logic over per-frame speech probabilities so it can be
tested without a model. `SileroVAD` supplies those probabilities in production.

Live subtitles trade accuracy for delay: a segment only becomes text once it
is closed, so the longest segment we allow is a floor on subtitle latency.
We close a segment on a pause, or force-close it at `max_s`, cutting at the
quietest recent frame so we split between words rather than mid-word.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

SR = 16000
FRAME = 512  # silero frame at 16 kHz = 32 ms
FRAME_S = FRAME / SR


@dataclass
class Segment:
    start: float          # media seconds since stream start
    end: float
    audio: np.ndarray = field(repr=False)  # float32 mono, includes padding
    forced: bool = False  # closed by max_s instead of a pause

    @property
    def duration(self) -> float:
        return self.end - self.start


class Chunker:
    def __init__(self, threshold: float = 0.5, min_silence_s: float = 0.35,
                 min_speech_s: float = 0.30, max_s: float = 3.0,
                 pad_s: float = 0.15, cut_search_s: float = 0.8):
        self.threshold = threshold
        self.neg_threshold = max(threshold - 0.15, 0.01)  # hysteresis, as silero does
        self.min_silence = round(min_silence_s / FRAME_S)
        self.min_speech = round(min_speech_s / FRAME_S)
        self.max_frames = round(max_s / FRAME_S)
        self.pad = round(pad_s / FRAME_S)
        self.cut_search = round(cut_search_s / FRAME_S)
        self._frames: list[np.ndarray] = []   # every frame since `_base`
        self._probs: list[float] = []
        self._base = 0          # absolute index of _frames[0]
        self._start: int | None = None  # absolute index where current speech began
        self._silence = 0       # consecutive non-speech frames inside speech
        self._speech = 0        # speech frames in the current segment

    def feed(self, frames: np.ndarray, probs) -> list[Segment]:
        """frames: (n, FRAME) float32, probs: n speech probabilities."""
        out: list[Segment] = []
        for f, p in zip(frames, probs):
            self._frames.append(f)
            self._probs.append(float(p))
            idx = self._base + len(self._frames) - 1
            if self._start is None:
                if p >= self.threshold:
                    self._start, self._silence, self._speech = idx, 0, 1
                else:
                    self._trim(keep=self.pad)
                continue
            if p >= self.neg_threshold and (p >= self.threshold or self._silence == 0):
                self._silence = 0
                self._speech += p >= self.threshold
            else:
                self._silence += 1
            length = idx - self._start + 1
            if self._silence >= self.min_silence:
                seg = self._close(idx - self._silence + 1, forced=False)
                if seg:
                    out.append(seg)
            elif length >= self.max_frames:
                out.append(self._force_cut(idx))
        return out

    def flush(self) -> list[Segment]:
        if self._start is None:
            return []
        seg = self._close(self._base + len(self._frames), forced=False)
        return [seg] if seg else []

    # -- internals -------------------------------------------------------
    def _force_cut(self, idx: int) -> Segment:
        # Cut at the least speech-like frame in the tail window so the split
        # lands between words; fall back to the hard limit.
        lo = max(self._start + self.min_speech, idx - self.cut_search + 1)
        window = self._probs[lo - self._base: idx - self._base + 1]
        cut = lo + int(np.argmin(window)) + 1 if window else idx + 1
        start = self._start
        seg = self._make(start, cut, forced=True)
        # The remainder continues as the next segment without a new onset.
        self._start = cut
        self._silence = 0
        self._speech = sum(1 for q in self._probs[cut - self._base:] if q >= self.threshold)
        self._trim(keep=idx + 1 - cut)
        return seg

    def _close(self, end: int, forced: bool) -> Segment | None:
        start, speech = self._start, self._speech
        self._start, self._silence, self._speech = None, 0, 0
        seg = self._make(start, end, forced) if speech >= self.min_speech else None
        self._trim(keep=self.pad)
        return seg

    def _make(self, start: int, end: int, forced: bool) -> Segment:
        a = max(self._base, start - self.pad)
        b = min(self._base + len(self._frames), end + (0 if forced else self.pad))
        audio = np.concatenate(self._frames[a - self._base: b - self._base])
        return Segment(start=start * FRAME_S, end=end * FRAME_S, audio=audio, forced=forced)

    def _trim(self, keep: int) -> None:
        drop = len(self._frames) - keep
        if drop > 0:
            del self._frames[:drop]
            del self._probs[:drop]
            self._base += drop


class SileroVAD:
    """Streaming wrapper around the silero model bundled with faster-whisper."""

    def __init__(self):
        from faster_whisper.vad import get_vad_model
        self.model = get_vad_model()

    def __call__(self, frames: np.ndarray) -> np.ndarray:
        return np.asarray(self.model(frames.reshape(-1))).reshape(-1)


class FrameBuffer:
    """Accumulates arbitrary-sized PCM reads into whole FRAME-sample frames."""

    def __init__(self):
        self._rest = np.zeros(0, dtype=np.float32)
        self._odd = b""  # a read can end mid-sample

    def push(self, pcm_s16le: bytes) -> np.ndarray:
        data = self._odd + pcm_s16le
        cut = len(data) - len(data) % 2
        self._odd = data[cut:]
        x = np.frombuffer(data[:cut], dtype=np.int16).astype(np.float32) / 32768.0
        x = np.concatenate([self._rest, x])
        n = len(x) // FRAME
        self._rest = x[n * FRAME:]
        return x[: n * FRAME].reshape(n, FRAME)
