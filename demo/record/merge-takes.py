"""Merge several takes of the walkthrough into one timeline for edit-walkthrough.py (item 14).

A take that fails part-way still holds good steps; a rerun changes the demo data (a second step-up
on the same seed can be held instead). So the film can take each step from the take where it ran on
the documented path. Each argument is TIMELINE::FIRST_STEP::LAST_STEP (empty = from the start / to
the end); the last argument is the merged timeline to write, next to the takes' videos.

Usage:
    python demo/record/merge-takes.py "a.timeline.json::::fixed deposit booking" \\
        "b.timeline.json::investing: a fund, a monthly SIP and a stock::" merged.timeline.json

Every take's times are shifted by 100,000 s per take, and each step records its video and shift,
so the editor cuts each clip from the right file. Chapters are matched by title.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

SHIFT = 100_000.0


def merge(specs: list[str]) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    captions: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []
    base: dict[str, Any] = {}
    for k, spec in enumerate(specs):
        path, first, last = spec.split("::")
        take = json.loads(Path(path).read_text(encoding="utf-8"))
        names = [s["name"] for s in take["steps"]]
        i = names.index(first) if first else 0
        j = names.index(last) if last else len(names) - 1
        off = k * SHIFT
        lo, hi = take["steps"][i]["start"], take["steps"][j]["end"]
        for step in take["steps"][i : j + 1]:
            chapter = take["chapters"][step["chapter"]]
            titles = [c["title"] for c in chapters]
            if chapter["title"] not in titles:
                chapters.append(chapter)
                titles.append(chapter["title"])
            moved = dict(step, start=step["start"] + off, end=step["end"] + off)
            moved.update(video=take["video"], offset=off, chapter=titles.index(chapter["title"]))
            moved["marks"] = [dict(m, s=m["s"] + off, e=m["e"] + off) for m in step["marks"]]
            steps.append(moved)
        captions += [dict(c, t=c["t"] + off) for c in take["captions"] if lo <= c["t"] <= hi]
        base = take
    return dict(base, chapters=chapters, steps=steps, captions=captions)


def main(argv: list[str]) -> int:
    *specs, out = argv[1:]
    merged = merge(specs)
    Path(out).write_text(json.dumps(merged, indent=1), encoding="utf-8")
    outcomes = Counter(s["outcome"] for s in merged["steps"])
    sys.stdout.write(f"{len(merged['steps'])} steps {dict(outcomes)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
