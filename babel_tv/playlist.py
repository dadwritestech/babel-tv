"""Channel directory: the iptv-org public playlists, filtered and probed.

iptv-org (github.com/iptv-org/iptv) publishes per-country and per-language
M3U lists of publicly available streams. We only read those lists; nothing
here hosts or rebroadcasts a stream.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

BASE = "https://iptv-org.github.io/iptv"
CACHE = Path(os.environ.get("BABEL_TV_CACHE", Path.home() / ".cache" / "babel-tv"))
CACHE_TTL_S = 6 * 3600

# ISO 639-1 (what users type) -> ISO 639-3 (iptv-org list names)
ISO3 = {"te": "tel", "hi": "hin", "ta": "tam", "kn": "kan", "ml": "mal", "bn": "ben",
        "mr": "mar", "gu": "guj", "pa": "pan", "ur": "urd", "en": "eng", "es": "spa",
        "pt": "por", "fr": "fra", "de": "deu", "it": "ita", "ru": "rus", "ja": "jpn",
        "ko": "kor", "zh": "zho", "ar": "ara", "tr": "tur", "id": "ind", "vi": "vie",
        "th": "tha", "pl": "pol", "nl": "nld", "uk": "ukr", "fa": "fas", "sw": "swa"}

ATTR = re.compile(r'([\w-]+)="([^"]*)"')


@dataclass
class Channel:
    name: str
    url: str
    tvg_id: str = ""
    group: str = ""
    logo: str = ""
    referrer: str = ""
    user_agent: str = ""
    tags: list[str] = field(default_factory=list)  # e.g. "Geo-blocked", "Not 24/7"

    @property
    def quality(self) -> str:
        m = re.search(r"\((\d+[pi])\)", self.name)
        return m.group(1) if m else ""

    @property
    def title(self) -> str:
        return re.sub(r"\s*(\(\d+[pi]\)|\[[^\]]*\])", "", self.name).strip()


def split_extinf(line: str) -> tuple[str, str]:
    """Split at the first comma outside quotes (user agents contain commas)."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            return line[:i], line[i + 1:]
    return line, ""


def parse_m3u(text: str) -> list[Channel]:
    chans: list[Channel] = []
    cur: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            head, name = split_extinf(line)
            attrs = dict(ATTR.findall(head))
            cur = {"name": name.strip(), "tvg_id": attrs.get("tvg-id", ""),
                   "group": attrs.get("group-title", ""), "logo": attrs.get("tvg-logo", ""),
                   "referrer": attrs.get("http-referrer", ""),
                   "user_agent": attrs.get("http-user-agent", "")}
        elif line.startswith("#EXTVLCOPT:") and cur is not None:
            k, _, v = line[len("#EXTVLCOPT:"):].partition("=")
            if k == "http-referrer":
                cur["referrer"] = cur["referrer"] or v
            elif k == "http-user-agent":
                cur["user_agent"] = cur["user_agent"] or v
        elif line and not line.startswith("#") and cur is not None:
            tags = re.findall(r"\[([^\]]+)\]", cur["name"])
            chans.append(Channel(url=line, tags=tags, **cur))
            cur = None
    return chans


def list_url(country: str | None = None, language: str | None = None,
             category: str | None = None) -> str:
    if country:
        return f"{BASE}/countries/{country.lower()}.m3u"
    if language:
        lang = language.lower()
        return f"{BASE}/languages/{ISO3.get(lang, lang)}.m3u"
    if category:
        return f"{BASE}/categories/{category.lower()}.m3u"
    return f"{BASE}/index.m3u"


def fetch(url: str, cache: Path = CACHE, ttl: float = CACHE_TTL_S) -> str:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / re.sub(r"[^\w.-]", "_", url.split("//", 1)[-1])
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        return path.read_text(encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": "babel-tv/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8", "replace")
    path.write_text(text, encoding="utf-8")
    return text


def load(country: str | None = None, language: str | None = None,
         category: str | None = None) -> list[Channel]:
    chans = parse_m3u(fetch(list_url(country, language, category)))
    if country and language:
        # Country lists don't say which language a channel speaks; intersect
        # with the language list by URL.
        lang_urls = {c.url for c in parse_m3u(fetch(list_url(language=language)))}
        chans = [c for c in chans if c.url in lang_urls]
    return chans


def search(chans: list[Channel], query: str | None = None, group: str | None = None,
           include_geo_blocked: bool = False) -> list[Channel]:
    out = []
    for c in chans:
        if not include_geo_blocked and "Geo-blocked" in c.tags:
            continue
        if query and query.lower() not in c.name.lower():
            continue
        if group and group.lower() not in c.group.lower():
            continue
        out.append(c)
    return out


def ffmpeg_input_args(ch: Channel | None) -> list[str]:
    args = ["-rw_timeout", "15000000"]
    if ch and ch.user_agent:
        args += ["-user_agent", ch.user_agent]
    if ch and ch.referrer:
        args += ["-headers", f"Referer: {ch.referrer}\r\n"]
    return args


def probe(ch: Channel, timeout: float = 12.0) -> tuple[bool, float, str]:
    """Is the stream alive and does it carry audio? Returns (ok, seconds, info)."""
    t = time.monotonic()
    cmd = ["ffprobe", "-v", "error", *ffmpeg_input_args(ch), "-select_streams", "a:0",
           "-show_entries", "stream=codec_name,sample_rate", "-of", "csv=p=0", ch.url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        ok = r.returncode == 0 and bool(r.stdout.strip())
        info = r.stdout.strip().splitlines()[0] if ok else (r.stderr.strip().splitlines() or ["no audio"])[-1]
    except subprocess.TimeoutExpired:
        ok, info = False, "timeout"
    return ok, time.monotonic() - t, info


def probe_all(chans: list[Channel], workers: int = 16, timeout: float = 12.0,
              on_result=None) -> list[tuple[Channel, bool, float, str]]:
    def one(c):
        res = (c, *probe(c, timeout))
        if on_result:
            on_result(*res)
        return res
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, chans))
