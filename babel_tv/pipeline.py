"""Live pipeline: ffmpeg -> VAD chunks -> whisper -> LLM translation -> cues.

Threads, left to right:
  reader     ffmpeg PCM -> silero VAD -> Chunker -> asr_q
  asr        faster-whisper                     -> mt_q
  mt         llama-server translation           -> CueTrack (+ metrics)
  relay      ffmpeg's remuxed MPEG-TS, held back by `delay` seconds -> mpv
  overlay    polls mpv's time-pos, draws the cue for that media time

Media time is the PCM sample clock (seconds since the first audio sample).
mpv rebases its clock to the same zero, so a cue made from audio at media
time t is drawn when mpv's time-pos reaches t, however long mpv buffered.
"""
from __future__ import annotations

import bisect
import json
import queue
import socket
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .chunker import SR, Chunker, FrameBuffer, Segment, SileroVAD
from .playlist import Channel, ffmpeg_input_args
from .subs import Cue, CueTrack


@dataclass
class CueMetric:
    start: float            # media seconds
    end: float
    arrived_start: float    # wall clock (monotonic) when the first speech sample arrived
    arrived_end: float      # ... when the last one arrived (segment closable)
    asr_done: float
    mt_done: float
    forced: bool
    lang: str | None
    source: str
    text: str

    @property
    def e2e(self) -> float:
        """Delay needed for this cue to appear exactly when its speech starts."""
        return self.mt_done - self.arrived_start

    @property
    def processing(self) -> float:
        return self.mt_done - self.arrived_end


class ArrivalClock:
    """Maps media time -> wall time at which that audio reached us."""

    def __init__(self):
        self._samples: list[int] = []
        self._walls: list[float] = []
        self._lock = threading.Lock()

    def mark(self, samples_total: int, wall: float) -> None:
        with self._lock:
            self._samples.append(samples_total)
            self._walls.append(wall)

    def wall_at(self, media_t: float) -> float | None:
        s = int(media_t * SR)
        with self._lock:
            i = bisect.bisect_left(self._samples, s)
            return self._walls[i] if i < len(self._walls) else None

    def live_offset(self) -> float | None:
        """Smallest constant (wall - media) a stall-free player could have used.

        HLS delivers audio in bursts (one segment, often 2-6 s, at a time), so
        "when the sample arrived" is a jagged clock. A player that never
        stalls plays media time t at t + this offset. Subtitle lag is measured
        against that clock: it is what you would see in a normal player.
        """
        with self._lock:
            if not self._samples:
                return None
            return max(w - n / SR for n, w in zip(self._samples, self._walls))


def build_ffmpeg(url: str, ch: Channel | None, ts_port: int | None, realtime: bool) -> list[str]:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    is_file = not url.lower().startswith(("http://", "https://", "rtmp://", "rtsp://", "udp://"))
    if realtime and is_file:
        cmd += ["-re"]
    if not is_file:
        cmd += ffmpeg_input_args(ch)
        if ".m3u8" in url.lower():
            # Start at the newest HLS segment instead of 3 back from the live edge.
            cmd += ["-live_start_index", "-1"]
    cmd += ["-i", url,
            "-map", "0:a:0", "-ac", "1", "-ar", str(SR),
            "-af", "aresample=async=1:first_pts=0",  # fill gaps so samples track time
            "-f", "s16le", "pipe:1"]
    if ts_port:
        cmd += ["-map", "0:v:0?", "-map", "0:a:0", "-c", "copy",
                "-f", "mpegts", f"tcp://127.0.0.1:{ts_port}"]
    return cmd


class Pipeline:
    def __init__(self, url: str, asr, translator, *, channel: Channel | None = None,
                 delay: float = 5.0, player=None, duration: float | None = None,
                 out_dir: Path | None = None, chunker: Chunker | None = None,
                 realtime_files: bool = True, save_audio: bool = False, log=print):
        self.url = url
        self.channel = channel
        self.asr = asr
        self.tr = translator
        self.delay = delay
        self.player = player
        self.duration = duration
        self.out_dir = out_dir
        self.chunker = chunker or Chunker()
        self.realtime_files = realtime_files
        self._wav = None
        if save_audio and out_dir:
            import wave
            out_dir.mkdir(parents=True, exist_ok=True)
            self._wav = wave.open(str(out_dir / "audio.wav"), "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(SR)
        self.log = log
        self.track = CueTrack()
        self.metrics: list[CueMetric] = []
        self.clock = ArrivalClock()
        self.asr_q: queue.Queue = queue.Queue()
        self.mt_q: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.t0: float | None = None
        self.view_delays: list[float] = []   # wall(now) - arrival(time-pos), from the player
        self.catchup_s = 0.0                 # wall seconds the player spent speeding up
        self.errors: list[str] = []

    # -- stages ------------------------------------------------------------
    def _reader(self, proc: subprocess.Popen) -> None:
        vad, fb, total = SileroVAD(), FrameBuffer(), 0
        block = SR // 2 * 2  # 0.5 s of s16le
        while not self.stop.is_set():
            data = proc.stdout.read1(block) if hasattr(proc.stdout, "read1") else proc.stdout.read(block)
            if not data:
                break
            now = time.monotonic()
            if self.t0 is None:
                self.t0 = now
            total += len(data) // 2
            if self._wav:
                self._wav.writeframes(data)
            self.clock.mark(total, now)
            frames = fb.push(data)
            if len(frames):
                for seg in self.chunker.feed(frames, vad(frames)):
                    self.asr_q.put((seg, now))
            if self.duration and total / SR >= self.duration:
                break
        for seg in self.chunker.flush():
            self.asr_q.put((seg, time.monotonic()))
        self.asr_q.put(None)

    def _asr(self) -> None:
        while (item := self.asr_q.get()) is not None:
            seg, arrived_end = item
            try:
                text, lang = self.asr.transcribe(seg.audio)
            except Exception as e:  # keep the stream going on a bad chunk
                self.errors.append(f"asr: {e}")
                continue
            if text:
                self.mt_q.put((seg, arrived_end, time.monotonic(), text, lang))
        self.mt_q.put(None)

    def _mt(self) -> None:
        while (item := self.mt_q.get()) is not None:
            seg, arrived_end, asr_done, text, lang = item
            try:
                out = self.tr.translate(text, lang)
            except Exception as e:
                self.errors.append(f"mt: {e}")
                out = ""
            done = time.monotonic()
            if not out:
                continue
            self.track.add(Cue(seg.start, seg.end, out, source=text, ready_at=done))
            arrived_start = self.clock.wall_at(seg.start) or arrived_end
            m = CueMetric(seg.start, seg.end, arrived_start, arrived_end, asr_done, done,
                          seg.forced, lang, text, out)
            self.metrics.append(m)
            self.log(f"[{seg.start:7.1f}s  e2e {m.e2e:4.1f}s  {lang or '?'}] {text}\n"
                     f"{'':10}-> {out}")

    # -- run -----------------------------------------------------------------
    def run(self) -> dict:
        ts_port = None
        relay = None
        if self.player:
            relay = socket.socket()
            relay.bind(("127.0.0.1", 0))
            relay.listen(1)
            ts_port = relay.getsockname()[1]
        cmd = build_ffmpeg(self.url, self.channel, ts_port, self.realtime_files)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._drain, args=(proc,), daemon=True).start()
        threads = [threading.Thread(target=f, daemon=True, name=f.__name__)
                   for f in (lambda: self._reader(proc), self._asr, self._mt)]
        if self.player:
            self.player.start(relay, self)
        [t.start() for t in threads]
        try:
            while threads[2].is_alive():
                threads[2].join(0.5)
                if self.player and self.player.closed():
                    self.log("player closed")
                    break
        except KeyboardInterrupt:
            self.log("stopping")
        self.stop.set()
        proc.kill()
        if self.player:
            self.player.stop()
        return self.finish()

    def _drain(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr:
            msg = line.decode("utf-8", "replace").strip()
            if msg:
                self.errors.append(f"ffmpeg: {msg}")

    def summary(self) -> dict:
        ms = self.metrics
        def pct(xs, p):
            xs = sorted(xs)
            return round(xs[min(len(xs) - 1, int(p * len(xs)))], 2) if xs else None
        e2e = [m.e2e for m in ms]
        off = self.clock.live_offset()
        lag = [m.mt_done - (m.start + off) for m in ms] if off is not None else []
        return {
            "url": self.url,
            "cues": len(ms),
            "media_seconds": round((self.metrics[-1].end if ms else 0), 1),
            "delay_setting_s": self.delay,
            # lag: subtitle ready time vs. a stall-free live player showing that speech.
            # This is the extra delay babel-tv needs; <= 0 means ready before shown.
            "lag_median_s": pct(lag, 0.5), "lag_p95_s": pct(lag, 0.95), "lag_max_s": pct(lag, 1.0),
            "late_at_delay": sum(x > self.delay for x in lag),
            # e2e: from first speech sample arriving (includes HLS burst waits).
            "e2e_median_s": pct(e2e, 0.5), "e2e_p95_s": pct(e2e, 0.95),
            "processing_median_s": pct([m.processing for m in ms], 0.5),
            "asr_median_s": pct([m.asr_done - m.arrived_end for m in ms], 0.5),
            "mt_median_s": pct([m.mt_done - m.asr_done for m in ms], 0.5),
            "segment_median_s": pct([m.end - m.start for m in ms], 0.5),
            "forced_cuts": sum(m.forced for m in ms),
            "view_delay_median_s": round(statistics.median(self.view_delays), 2) if self.view_delays else None,
            "view_delay_p95_s": pct(self.view_delays, 0.95) if self.view_delays else None,
            "catchup_s": round(self.catchup_s, 1),
            "languages": sorted({m.lang for m in ms if m.lang}),
            "errors": self.errors[-10:],
        }

    def finish(self) -> dict:
        if self._wav:
            self._wav.close()
        s = self.summary()
        if self.out_dir:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            (self.out_dir / "subs.srt").write_text(self.track.to_srt(), encoding="utf-8")
            src = CueTrack()
            for c in self.track.cues():
                src.add(Cue(c.start, c.end, c.source))
            (self.out_dir / "source.srt").write_text(src.to_srt(), encoding="utf-8")
            with open(self.out_dir / "cues.jsonl", "w", encoding="utf-8") as f:
                for m in self.metrics:
                    f.write(json.dumps({**asdict(m), "e2e": round(m.e2e, 3),
                                        "processing": round(m.processing, 3)},
                                       ensure_ascii=False) + "\n")
            (self.out_dir / "summary.json").write_text(json.dumps(s, indent=2, ensure_ascii=False),
                                                       encoding="utf-8")
        return s
