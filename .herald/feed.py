"""Generic herald feed for repos without a data stream: release tags and new entries.

Reads .herald/entries.json next to this file:
  {"repo": "reskin",
   "link": "https://github.com/dadwritestech/reskin",
   "releases": true,
   "entries": [{"glob": "gallery/pairs/*", "kind": "pair",
                "text": "New in the reskin gallery: {name}."}]}

Release posts use the tag's annotation message (first paragraph), so writing a good
`git tag -a v0.2.0 -m "..."` message is writing the post. {name} is the entry's file
or directory name with its extension removed; {stem_words} swaps - and _ for spaces;
{left} and {right} split the name at "--"; {Name} and {Right} are title-cased.
"""
import glob
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def tags():
    try:
        out = subprocess.run(
            ["git", "for-each-ref", "--sort=creatordate", "--format=%(refname:short)%00%(contents)%01",
             "refs/tags"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    res = []
    for rec in out.split("\x01"):
        rec = rec.strip("\n")
        if "\x00" in rec:
            name, msg = rec.split("\x00", 1)
            res.append((name, msg.strip().split("\n\n")[0].strip()))
    return res


def main():
    cfg = json.load(open(os.path.join(HERE, "entries.json"), encoding="utf-8"))
    repo, link = cfg["repo"], cfg["link"]
    if cfg.get("releases", True):
        for name, msg in tags():
            if not name.startswith("v"):
                continue
            print(json.dumps({"id": f"{repo}/release/{name}",
                              "text": f"{repo} {name}: {msg}" if msg else f"{repo} {name} is out.",
                              "link": f"{link}/releases/tag/{name}", "priority": 8},
                             ensure_ascii=False))
    for e in cfg.get("entries", []):
        for path in sorted(glob.glob(os.path.join(ROOT, e["glob"]))):
            base = os.path.basename(path.rstrip("/\\"))
            name = os.path.splitext(base)[0] if os.path.isfile(path) else base
            left, _, right = name.partition("--")
            print(json.dumps({"id": f"{repo}/{e['kind']}/{name}",
                              "text": e["text"].format(name=name, left=left, right=right, Name=name.replace("-", " ").title(),
                                                       Right=right.replace("-", " ").title(),
                                                       stem_words=name.replace("-", " ").replace("_", " ")),
                              "link": e.get("link", link), "priority": e.get("priority", 2)},
                             ensure_ascii=False))


if __name__ == "__main__":
    main()
