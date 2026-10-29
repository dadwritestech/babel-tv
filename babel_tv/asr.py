"""Speech recognition with faster-whisper, tuned for 1-3 s live chunks."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

import numpy as np

# Whisper was trained on subtitled video, so on music or silence it "remembers"
# the credits. These are the lines it invents most; seen live on NHK and Weathernews.
HALLUCINATIONS = {
    "ご視聴ありがとうございました", "ご視聴ありがとうございました。", "音楽", "字幕視聴ありがとうございました",
    "thank you for watching", "thanks for watching", "thank you.", "thank you", "you",
    "subtitles by the amara.org community", "music", "[music]", "(music)", "♪",
    "धन्यवाद", "सब्सक्राइब करें", "please subscribe", "transcription by castingwords",
    "請不吝點贊 訂閱 轉發 打賞支持明鏡與點點欄目", "字幕由amara.org社区提供",
    "sottotitoli creati dalla comunità amara.org", "untertitel der amara.org-community",
    "продолжение следует...", "subtítulos realizados por la comunidad de amara.org",
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip().lower()


def is_hallucination(text: str, no_speech_prob: float = 0.0, avg_logprob: float = 0.0,
                     compression_ratio: float = 1.0) -> bool:
    t = normalize(text)
    if not t or t in HALLUCINATIONS or t.strip("♪ .。!！") == "":
        return True
    if compression_ratio > 2.4:          # "the the the the" loops
        return True
    if no_speech_prob > 0.6 and avg_logprob < -0.8:
        return True
    return False


class LanguageLock:
    """Per-chunk language ID on 2 s of audio is noisy; vote, then lock."""

    def __init__(self, fixed: str | None = None, votes: int = 3, min_prob: float = 0.5):
        self.lang = fixed
        self.votes = votes
        self.min_prob = min_prob
        self._seen: Counter[str] = Counter()

    def observe(self, lang: str, prob: float) -> None:
        if self.lang or prob < self.min_prob:
            return
        self._seen[lang] += 1
        top, n = self._seen.most_common(1)[0]
        if n >= self.votes:
            self.lang = top


class ASR:
    def __init__(self, model: str = "large-v3-turbo", device: str = "cuda",
                 compute_type: str = "int8_float16", device_index: int = 0,
                 language: str | None = None):
        from faster_whisper import WhisperModel
        try:
            self.model = WhisperModel(model, device=device, device_index=device_index,
                                      compute_type=compute_type)
        except Exception:
            if device == "cpu":
                raise
            # No CUDA, or no VRAM left: CPU int8 still runs small models live.
            self.model = WhisperModel(model, device="cpu", compute_type="int8")
        self.lock = LanguageLock(language)
        self._prev = ""

    def transcribe(self, audio: np.ndarray) -> tuple[str, str | None]:
        segs, info = self.model.transcribe(
            audio, language=self.lock.lang, beam_size=1, vad_filter=False,
            condition_on_previous_text=False, without_timestamps=True,
            initial_prompt=self._prev[-200:] or None, temperature=0.0)
        segs = list(segs)
        self.lock.observe(info.language, info.language_probability)
        kept = [s.text.strip() for s in segs
                if not is_hallucination(s.text, s.no_speech_prob, s.avg_logprob, s.compression_ratio)]
        text = " ".join(kept).strip()
        if text:
            self._prev = (self._prev + " " + text)[-400:]
        return text, self.lock.lang or info.language
