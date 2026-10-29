"""Live-vs-offline transcription agreement for a babel-tv run.

There is no human transcript of live TV, so this measures what live
chunking costs: the same audio.wav is transcribed offline with full
context (large-v3, beam 5, whisper's own VAD, word timestamps) and each
live cue is compared with the offline words that fall inside its time
span. Aligning by time keeps one dropped or looping stretch from wrecking
the whole score (a single whole-text CER did exactly that).

Reports:
  cer_aligned   character error rate of live cues vs offline words in the same span
  coverage      share of offline speech (chars) that lies inside some live cue
  unmatched     share of live chars in cues with no offline words at all
CER rather than WER because Hindi/Telugu word boundaries make WER unreliable.

usage: python scripts/quality.py runs/<run> [--model large-v3] [--lang hi]
"""
import argparse, json, re, sys, unicodedata, wave
from pathlib import Path

import numpy as np


def norm(t: str) -> str:
    t = unicodedata.normalize("NFKC", t).lower()
    t = "".join(ch for ch in t if not unicodedata.category(ch).startswith(("P", "S")))
    return re.sub(r"\s+", "", t)


def cer(hyp: str, ref: str) -> float:
    h, r = norm(hyp), norm(ref)
    if not r:
        return float("nan")
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hc in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc))
        prev = cur
    return prev[-1] / len(r)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--lang")
    a = ap.parse_args()
    run = Path(a.run)
    cues = [json.loads(l) for l in open(run / "cues.jsonl", encoding="utf-8")]
    live = " ".join(c["source"] for c in cues)
    lang = a.lang or max(set(c["lang"] for c in cues), key=[c["lang"] for c in cues].count)
    w = wave.open(str(run / "audio.wav"))
    audio = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    from faster_whisper import WhisperModel
    m = WhisperModel(a.model, device="cuda", compute_type="int8_float16")
    segs, _ = m.transcribe(audio, language=lang, beam_size=5, vad_filter=True,
                           word_timestamps=True, condition_on_previous_text=False)
    words = [(w.start, w.end, w.word) for s in segs for w in (s.words or [])]
    ref = "".join(w for _, _, w in words).strip()
    edits = ref_in = live_unmatched = 0
    used = set()
    for c in cues:
        inside = [i for i, (ws, we, _) in enumerate(words) if c["start"] <= (ws + we) / 2 < c["end"]]
        used.update(inside)
        r = "".join(words[i][2] for i in inside)
        if not norm(r):
            live_unmatched += len(norm(c["source"]))
            continue
        edits += cer(c["source"], r) * len(norm(r))
        ref_in += len(norm(r))
    ref_total = len(norm(ref))
    res = {"run": run.name, "lang": lang, "reference_model": a.model,
           "audio_s": round(len(audio) / 16000, 1), "live_chars": len(norm(live)),
           "ref_chars": ref_total,
           "cer_aligned": round(edits / ref_in, 3) if ref_in else None,
           "coverage": round(ref_in / ref_total, 3) if ref_total else None,
           "unmatched": round(live_unmatched / max(1, len(norm(live))), 3),
           "cer_whole_text": round(cer(live, ref), 3)}
    (run / "offline_reference.txt").write_text(ref, encoding="utf-8")
    (run / "quality.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
