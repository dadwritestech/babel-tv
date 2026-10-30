#!/usr/bin/env python3
"""herald: turn a repo's data changes into posts on X, Bluesky and Mastodon.

One file, standard library only, so it can be copied into any repo and run in
any CI. The repo owns its feed (a script that prints events as JSON lines);
herald owns everything after that: validation, dedupe, rate limits and posting.

Event (one JSON object per line):
  {"id": "token-tax/weekly/2026-W45",   # stable; posted once, ever
   "text": "Amharic costs 5.81x ...",    # facts only, no link, no @mentions
   "link": "https://github.com/...",     # optional, appended by herald
   "priority": 1}                        # optional, higher posts first

Usage:
  python feed.py | python herald.py --repo token-tax             # dry run
  python feed.py | python herald.py --repo token-tax --post      # really post

Platforms are enabled by their environment variables:
  X:         HERALD_X_API_KEY, HERALD_X_API_SECRET,
             HERALD_X_ACCESS_TOKEN, HERALD_X_ACCESS_SECRET
  Bluesky:   HERALD_BSKY_HANDLE, HERALD_BSKY_APP_PASSWORD
  Mastodon:  HERALD_MASTODON_URL, HERALD_MASTODON_TOKEN
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request

VERSION = "0.1.0"
STATE_DEFAULT = ".herald/state.json"
LIMITS = {"x": 280, "bluesky": 300, "mastodon": 500}
URL_LEN = {"x": 23}  # X counts every link as 23 characters
MENTION = re.compile(r"(?<![\w.])@(?=[A-Za-z0-9_])")


# --- events -----------------------------------------------------------------

class BadEvent(ValueError):
    pass


def clean_text(text: str) -> str:
    """No unsolicited mentions: '@name' becomes '@​name', which no
    platform links or notifies. Also normalises whitespace at the ends."""
    return MENTION.sub("@\u200b", text.strip())


def parse_events(lines) -> list[dict]:
    events, seen = [], set()
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as e:
            raise BadEvent(f"line {n}: not JSON ({e})") from None
        if not isinstance(ev, dict) or not ev.get("id") or not ev.get("text"):
            raise BadEvent(f"line {n}: needs 'id' and 'text'")
        ev["id"] = str(ev["id"])
        if ev["id"] in seen:
            continue
        seen.add(ev["id"])
        ev["text"] = clean_text(str(ev["text"]))
        ev["priority"] = int(ev.get("priority", 0))
        events.append(ev)
    return events


def compose(ev: dict, platform: str) -> str:
    """Text plus link, trimmed to the platform limit at a word boundary."""
    limit = LIMITS[platform]
    link = ev.get("link") or ""
    link_cost = (URL_LEN.get(platform, len(link)) + 2) if link else 0
    room = limit - link_cost
    text = ev["text"]
    if len(text) > room:
        cut = text[: room - 1]
        if " " in cut[room // 2:]:
            cut = cut[: cut.rfind(" ")]
        text = cut.rstrip(" ,;:") + "…"
    return f"{text}\n\n{link}" if link else text


# --- state ------------------------------------------------------------------

def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except FileNotFoundError:
        st = {}
    st.setdefault("posted", {})
    st.setdefault("last_post_at", None)
    return st


def save_state(path: str, st: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def select(events: list[dict], st: dict, max_posts: int, min_gap_h: float,
           now: dt.datetime) -> list[dict]:
    fresh = [e for e in events if e["id"] not in st["posted"]]
    fresh.sort(key=lambda e: -e["priority"])  # stable: feed order breaks ties
    if st.get("last_post_at") and min_gap_h > 0:
        last = dt.datetime.fromisoformat(st["last_post_at"])
        if (now - last).total_seconds() < min_gap_h * 3600:
            return []
    return fresh[:max_posts]


# --- platforms --------------------------------------------------------------

def _http(method, url, headers=None, body=None, timeout=30):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def _pct(s: str) -> str:
    return urllib.parse.quote(s, safe="~")


def oauth1_header(method, url, key, secret, token, token_secret,
                  nonce=None, ts=None, form=None) -> str:
    """OAuth 1.0a HMAC-SHA1 header. JSON bodies are not signed; form bodies are."""
    params = {
        "oauth_consumer_key": key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(ts or int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query)
    base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    pairs = sorted((_pct(k), _pct(v)) for k, v in list(params.items()) + query + list((form or {}).items()))
    param_str = "&".join(f"{k}={v}" for k, v in pairs)
    base = "&".join([method.upper(), _pct(base_url), _pct(param_str)])
    signing_key = f"{_pct(secret)}&{_pct(token_secret)}"
    sig = base64.b64encode(hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    params["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(params.items()))


class X:
    name = "x"
    URL = "https://api.x.com/2/tweets"

    def __init__(self, env):
        self.k = [env.get(v) for v in ("HERALD_X_API_KEY", "HERALD_X_API_SECRET",
                                        "HERALD_X_ACCESS_TOKEN", "HERALD_X_ACCESS_SECRET")]
        self.ready = all(self.k)

    def post(self, text):
        auth = oauth1_header("POST", self.URL, *self.k)
        r = _http("POST", self.URL, {"Authorization": auth, "Content-Type": "application/json"},
                  json.dumps({"text": text}).encode())
        return f"https://x.com/i/web/status/{r['data']['id']}"


def bsky_facets(text: str) -> list[dict]:
    """Bluesky needs explicit byte ranges for links to be clickable."""
    facets = []
    for m in re.finditer(r"https?://\S+", text):
        start = len(text[: m.start()].encode("utf-8"))
        end = start + len(m.group(0).encode("utf-8"))
        facets.append({"index": {"byteStart": start, "byteEnd": end},
                       "features": [{"$type": "app.bsky.richtext.facet#link", "uri": m.group(0)}]})
    return facets


class Bluesky:
    name = "bluesky"
    HOST = "https://bsky.social/xrpc"

    def __init__(self, env):
        self.handle = env.get("HERALD_BSKY_HANDLE")
        self.pw = env.get("HERALD_BSKY_APP_PASSWORD")
        self.ready = bool(self.handle and self.pw)
        self.session = None

    def post(self, text):
        if not self.session:
            self.session = _http("POST", f"{self.HOST}/com.atproto.server.createSession",
                                 {"Content-Type": "application/json"},
                                 json.dumps({"identifier": self.handle, "password": self.pw}).encode())
        record = {"$type": "app.bsky.feed.post", "text": text,
                  "createdAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                  "langs": ["en"],
                  "labels": {"$type": "com.atproto.label.defs#selfLabels",
                             "values": [{"val": "bot"}]}}
        facets = bsky_facets(text)
        if facets:
            record["facets"] = facets
        r = _http("POST", f"{self.HOST}/com.atproto.repo.createRecord",
                  {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.session['accessJwt']}"},
                  json.dumps({"repo": self.session["did"], "collection": "app.bsky.feed.post",
                              "record": record}).encode())
        rkey = r["uri"].rsplit("/", 1)[-1]
        return f"https://bsky.app/profile/{self.handle}/post/{rkey}"


class Mastodon:
    name = "mastodon"

    def __init__(self, env):
        self.base = (env.get("HERALD_MASTODON_URL") or "").rstrip("/")
        self.token = env.get("HERALD_MASTODON_TOKEN")
        self.ready = bool(self.base and self.token)

    def post(self, text):
        r = _http("POST", f"{self.base}/api/v1/statuses",
                  {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/x-www-form-urlencoded",
                   "Idempotency-Key": hashlib.sha256(text.encode()).hexdigest()[:32]},
                  urllib.parse.urlencode({"status": text, "visibility": "public"}).encode())
        return r.get("url", "")


PLATFORMS = (X, Bluesky, Mastodon)


# --- main -------------------------------------------------------------------

def run(lines, *, repo, state_path, post, max_posts, min_gap_h, env, out, now=None,
        platforms=PLATFORMS):
    now = now or dt.datetime.now(dt.timezone.utc)
    events = parse_events(lines)
    st = load_state(state_path)
    chosen = select(events, st, max_posts, min_gap_h, now)
    clients = [p(env) for p in platforms]
    ready = [c for c in clients if c.ready]
    print(f"herald {VERSION} [{repo}] {len(events)} events, {len(chosen)} to post, "
          f"platforms: {', '.join(c.name for c in ready) or 'none'}"
          f"{'' if post else ' (dry run)'}", file=out)
    if post and not ready:
        print("no platform credentials set; nothing posted", file=out)
        return 0
    failures = 0
    for ev in chosen:
        results = {}
        for c in (ready if post else clients):
            text = compose(ev, c.name)
            if not post:
                print(f"--- {ev['id']} -> {c.name} ({len(text)} chars)\n{text}", file=out)
                continue
            try:
                results[c.name] = c.post(text)
                print(f"posted {ev['id']} -> {results[c.name]}", file=out)
            except Exception as e:  # one platform failing must not block the others
                failures += 1
                print(f"FAILED {ev['id']} -> {c.name}: {e}", file=out)
        if post and results:
            st["posted"][ev["id"]] = {"at": now.isoformat(), **results}
            st["last_post_at"] = now.isoformat()
            save_state(state_path, st)
    return 1 if failures and post else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--state", default=STATE_DEFAULT)
    ap.add_argument("--post", action="store_true", help="really post (default: dry run)")
    ap.add_argument("--max-posts", type=int, default=1)
    ap.add_argument("--min-gap-hours", type=float, default=6.0)
    ap.add_argument("--mark-posted", action="store_true",
                    help="record every current event as posted without posting (first run)")
    a = ap.parse_args(argv)
    lines = sys.stdin.read().splitlines()
    if a.mark_posted:
        st = load_state(a.state)
        n = 0
        for ev in parse_events(lines):
            if ev["id"] not in st["posted"]:
                st["posted"][ev["id"]] = {"at": "baseline"}
                n += 1
        save_state(a.state, st)
        print(f"marked {n} events as already posted")
        return 0
    return run(lines, repo=a.repo, state_path=a.state, post=a.post, max_posts=a.max_posts,
               min_gap_h=a.min_gap_hours, env=os.environ, out=sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
