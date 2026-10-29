"""mpv as the screen: a delayed MPEG-TS feed in, subtitles drawn over IPC.

mpv is told to buffer `delay` seconds before it starts (and again after any
underrun), so the picture runs that far behind the live feed: that is the
head start the translator gets. Buffering inside mpv, not in our relay, also
smooths out HLS's bursty delivery. Cues are drawn with `osd-overlay`
(ASS events) keyed on mpv's own `time-pos`, so subtitles stay in sync with
the picture even when mpv stalls or buffers.
"""
from __future__ import annotations

import collections
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

from .subs import ass_escape, wrap

FONT = "Nirmala UI" if sys.platform == "win32" else "Noto Sans"  # Telugu + Devanagari glyphs


def find_mpv() -> str | None:
    found = shutil.which("mpv")
    if found:
        return found
    if sys.platform == "win32":  # winget installs without refreshing PATH
        root = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
        if os.path.isdir(root):
            for d in os.listdir(root):
                p = os.path.join(root, d, "mpv.exe")
                if d.lower().startswith("mpv") and os.path.exists(p):
                    return p
    return None


class MpvIpc:
    """Strictly request/response: Windows pipes deadlock on concurrent read+write."""

    def __init__(self, path: str, timeout: float = 15.0):
        self.path = path
        deadline = time.monotonic() + timeout
        while True:
            try:
                if sys.platform == "win32":
                    self.f = open(path, "r+b", buffering=0)
                else:
                    s = socket.socket(socket.AF_UNIX)
                    s.connect(path)
                    self.f = s.makefile("rwb", buffering=0)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        self._buf = b""
        self._id = 0

    def command(self, *args):
        self._id += 1
        rid = self._id
        self.f.write(json.dumps({"command": list(args), "request_id": rid}).encode() + b"\n")
        while True:
            while b"\n" not in self._buf:
                chunk = self.f.read(4096)
                if not chunk:
                    raise ConnectionError("mpv closed")
                self._buf += chunk
            line, self._buf = self._buf.split(b"\n", 1)
            msg = json.loads(line)
            if msg.get("request_id") == rid:
                return msg.get("data") if msg.get("error") == "success" else None

    def overlay(self, ass: str, res_y: int = 720):
        self._id += 1
        rid = self._id
        cmd = {"command": {"name": "osd-overlay", "id": 1, "format": "ass-events",
                           "data": ass, "res_y": res_y, "z": 0}, "request_id": rid}
        self.f.write(json.dumps(cmd, ensure_ascii=False).encode("utf-8") + b"\n")
        while True:
            while b"\n" not in self._buf:
                chunk = self.f.read(4096)
                if not chunk:
                    raise ConnectionError("mpv closed")
                self._buf += chunk
            line, self._buf = self._buf.split(b"\n", 1)
            if json.loads(line).get("request_id") == rid:
                return


def cue_ass(text: str, source: str | None = None, font: str = FONT) -> str:
    main = (r"{\an2\fn" + font + r"\fs40\bord2.5\shad1\1c&HFFFFFF&\3c&H101010&}"
            + ass_escape(wrap(text)))
    if not source:
        return main
    small = (r"{\an2\fn" + font + r"\fs24\bord2\shad0\1c&HB8E6FF&\3c&H101010&}"
             + ass_escape(wrap(source, width=70, max_lines=1)) + r"\N")
    return small + main


class MpvPlayer:
    def __init__(self, delay: float, title: str = "babel-tv", show_source: bool = True,
                 extra_args: list[str] | None = None, mpv: str | None = None):
        self.delay = delay
        self.hold = 0.0
        self.title = title
        self.show_source = show_source
        self.extra_args = extra_args or []
        self.mpv = mpv or find_mpv()
        if not self.mpv:
            raise RuntimeError("mpv not found. Install it (winget install mpv-player.mpv-CI.MSVC, "
                               "brew install mpv, apt install mpv) or use --no-player.")
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._chunks: collections.deque = collections.deque()
        self._closed = False

    def start(self, relay: socket.socket, pipeline) -> None:
        self.pipeline = pipeline
        name = f"babeltv-{os.getpid()}"
        self.ipc_path = rf"\\.\pipe\{name}" if sys.platform == "win32" else f"/tmp/{name}.sock"
        self.proc = subprocess.Popen(
            [self.mpv, "-", f"--title={self.title}", f"--input-ipc-server={self.ipc_path}",
             "--force-window=immediate", "--keep-open=no", "--osd-level=1",
             "--force-seekable=no", "--cache=yes", "--cache-secs=120",
             "--cache-pause-initial=yes", f"--cache-pause-wait={self.delay}",
             "--terminal=no", *self.extra_args],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._accept, args=(relay,), daemon=True).start()
        threading.Thread(target=self._feed, daemon=True).start()
        threading.Thread(target=self._overlay, daemon=True).start()

    def closed(self) -> bool:
        return self._closed or (self.proc is not None and self.proc.poll() is not None)

    def stop(self) -> None:
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass

    def _accept(self, relay: socket.socket) -> None:
        relay.settimeout(60)
        try:
            conn, _ = relay.accept()
        except OSError:
            self._closed = True
            return
        while not self._stop.is_set():
            data = conn.recv(65536)
            if not data:
                break
            self._chunks.append((time.monotonic(), data))

    def _feed(self) -> None:
        # Optional extra hold in the relay (default 0: mpv's cache does the delaying).
        while not self._stop.is_set():
            if not self._chunks:
                time.sleep(0.02)
                continue
            t, data = self._chunks[0]
            wait = t + self.hold - time.monotonic()
            if wait > 0:
                time.sleep(min(wait, 0.05))
                continue
            self._chunks.popleft()
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except OSError:
                self._closed = True
                return

    def _overlay(self) -> None:
        try:
            ipc = MpvIpc(self.ipc_path)
        except OSError:
            return
        last = None
        n = 0
        while not self._stop.is_set():
            try:
                pos = ipc.command("get_property", "time-pos")
                if pos is not None:
                    n += 1
                    arrived = self.pipeline.clock.wall_at(pos)
                    if arrived is not None and n % 10 == 0:
                        self.pipeline.view_delays.append(time.monotonic() - arrived)
                    cue = self.pipeline.track.active(pos)
                    key = id(cue) if cue else None
                    if key != last:
                        ipc.overlay(cue_ass(cue.text, cue.source if self.show_source else None)
                                    if cue else "")
                        last = key
            except (ConnectionError, OSError, ValueError):
                self._closed = True
                return
            time.sleep(0.05)
