"""babel-tv: watch any public TV channel with live subtitles in your language."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import playlist
from .translate import LANGS


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass


def resolve(target: str, country: str | None, language: str | None,
            log=print) -> tuple[str, playlist.Channel | None]:
    if target.lower().startswith(("http://", "https://", "rtmp://", "rtsp://", "udp://")) \
            or os.path.exists(target):
        return target, None
    chans = playlist.load(country=country, language=language)
    exact = [c for c in chans if c.title.lower() == target.lower()]
    cands = exact or playlist.search(chans, query=target, include_geo_blocked=True)
    cands.sort(key=lambda c: ("Geo-blocked" in c.tags, "Not 24/7" in c.tags))
    if not cands:
        raise SystemExit(f"No channel matching {target!r}. Try: babel-tv channels --query {target}")
    for c in cands[:8]:
        ok, secs, info = playlist.probe(c)
        log(f"probe {c.name}: {'ok' if ok else 'dead'} ({secs:.1f}s, {info})")
        if ok:
            return c.url, c
    raise SystemExit(f"Found {len(cands)} channels matching {target!r}, but none responded.")


def cmd_channels(a) -> None:
    chans = playlist.search(playlist.load(a.country, a.language, a.category), a.query, a.group,
                            include_geo_blocked=a.geo_blocked)
    chans = chans[: a.limit]
    if not a.probe:
        for c in chans:
            print(f"{c.title:40.40} {c.quality:6} {c.group:16.16} {' '.join(c.tags)}")
        print(f"\n{len(chans)} channels (add --probe to keep only live ones)")
        return
    alive = 0
    def show(c, ok, secs, info):
        nonlocal alive
        if ok:
            alive += 1
            print(f"  live  {c.title:40.40} {c.quality:6} {c.group:16.16} {secs:4.1f}s  {info}",
                  flush=True)
        elif a.verbose:
            print(f"  dead  {c.title:40.40} {info[:60]}", flush=True)
    print(f"probing {len(chans)} streams...")
    playlist.probe_all(chans, on_result=show)
    print(f"\n{alive}/{len(chans)} live")


def cmd_pick(a) -> None:
    chans = playlist.search(playlist.load(a.country, a.language, a.category), a.query, a.group)
    chans = chans[: a.limit]
    print(f"probing {len(chans)} streams (skipping dead ones)...")
    res = [r for r in playlist.probe_all(chans) if r[1]]
    if not res:
        raise SystemExit("No live streams in that selection.")
    res.sort(key=lambda r: r[0].title.lower())
    for i, (c, _, secs, info) in enumerate(res, 1):
        print(f"{i:3}. {c.title:40.40} {c.quality:6} {c.group:16.16} {info}")
    choice = input("\nchannel number: ").strip()
    c = res[int(choice) - 1][0]
    a.target = c.url
    run_play(a, c.url, c)


def build_pipeline(a, url, ch):
    from .asr import ASR
    from .chunker import Chunker
    from .pipeline import Pipeline
    from .translate import Translator
    print(f"loading whisper {a.asr_model} ...", flush=True)
    asr = ASR(a.asr_model, device=a.device, compute_type=a.compute_type,
              device_index=a.gpu, language=a.source_lang)
    tr = Translator(a.to, base_url=a.server, model=a.model, api_key=a.api_key)
    player = None
    if not a.no_player:
        from .player import MpvPlayer
        player = MpvPlayer(a.delay, title=f"babel-tv · {ch.title if ch else url} → {LANGS.get(a.to, a.to)}",
                           show_source=not a.no_source)
    out = Path(a.out) if a.out else Path("runs") / time.strftime("%Y%m%d-%H%M%S")
    return Pipeline(url, asr, tr, channel=ch, delay=a.delay, player=player,
                    duration=a.duration, out_dir=out, chunker=Chunker(max_s=a.max_chunk))


def run_play(a, url, ch) -> None:
    p = build_pipeline(a, url, ch)
    print(f"playing {ch.name if ch else url}  ->  {LANGS.get(a.to, a.to)} "
          f"(delay {a.delay}s; Ctrl+C to stop)", flush=True)
    s = p.run()
    print("\n--- summary ---")
    for k, v in s.items():
        print(f"{k:22} {v}")
    print(f"saved to {p.out_dir}")


def cmd_play(a) -> None:
    url, ch = resolve(a.target, a.country, a.language)
    run_play(a, url, ch)


def add_filters(p):
    p.add_argument("--country", help="ISO country code, e.g. in, jp, br")
    p.add_argument("--language", help="spoken language, e.g. hi, ja, es")
    p.add_argument("--category", help="iptv-org category, e.g. news, sports")
    p.add_argument("--group", help="filter by group-title substring")
    p.add_argument("--query", help="filter by name substring")
    p.add_argument("--limit", type=int, default=60)


def add_play(p):
    p.add_argument("--to", default="te", help="subtitle language (te, hi, en, ...)")
    p.add_argument("--from", dest="source_lang", help="spoken language if known (skips auto-detect)")
    p.add_argument("--delay", type=float, default=5.0, help="seconds the picture is held back")
    p.add_argument("--no-player", action="store_true", help="headless: print subtitles only")
    p.add_argument("--no-source", action="store_true", help="hide the original-language line")
    p.add_argument("--duration", type=float, help="stop after this many seconds of audio")
    p.add_argument("--out", help="run directory for subs.srt / cues.jsonl / summary.json")
    p.add_argument("--max-chunk", type=float, default=3.0, help="longest speech chunk, seconds")
    p.add_argument("--asr-model", default="large-v3-turbo")
    p.add_argument("--device", default="cuda")
    p.add_argument("--compute-type", default="int8_float16")
    p.add_argument("--gpu", type=int, default=0)
    p.add_argument("--server", default=os.environ.get("BABEL_TV_SERVER", "http://127.0.0.1:8080"))
    p.add_argument("--model", default=os.environ.get("BABEL_TV_MODEL", "gemma-4-12b"))
    p.add_argument("--api-key", default=None, help="llama-server key (default: $LLAMA_API_KEY)")


def main(argv=None) -> None:
    _utf8()
    ap = argparse.ArgumentParser(prog="babel-tv", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("play", help="play a channel (name or URL) with live translated subtitles")
    p.add_argument("target", help="channel name from iptv-org, a stream URL, or a file")
    p.add_argument("--country")
    p.add_argument("--language")
    add_play(p)
    p.set_defaults(fn=cmd_play)

    p = sub.add_parser("channels", help="list channels from iptv-org")
    add_filters(p)
    p.add_argument("--probe", action="store_true", help="check streams, show live ones only")
    p.add_argument("--geo-blocked", action="store_true", help="include channels marked geo-blocked")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_channels)

    p = sub.add_parser("pick", help="probe channels, pick one from a numbered list, play it")
    add_filters(p)
    add_play(p)
    p.set_defaults(fn=cmd_pick)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
