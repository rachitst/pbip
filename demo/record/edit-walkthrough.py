"""Edit the raw walkthrough recording into the final film and the highlights cut (item 14).

record-walkthrough.ts writes the raw WebM and a timeline JSON: chapters, steps (start, end, outcome,
highlight) with "dead" marks (waits the viewer need not see) and "fast" marks (shown at 1.5x), and
the captions with their times. This script turns that into:

  <name>.mp4 (H.264) and <name>.webm (VP9), 1920x1080: every step with its dead time cut, the fast
      marks sped up, and a 2-second title card before each chapter;
  <name>.chapters.txt (YouTube style, "MM:SS Title") and <name>.srt (the captions on the edited
      clock, usable as a voice-over script);
  the same four files for the highlights cut (<name>-highlights.*): only the steps marked
      highlight, each capped in length, with shorter cards.

Only the time between a step's start and end is used, so the gaps between steps (sign-outs, the
page settling) are cut too. Steps recorded as PENDING take no time and are left out.

ffmpeg: a full static build (Playwright's own writes WebM only). Pass --ffmpeg, set FFMPEG,
or install the pinned imageio-ffmpeg wheel in a scratch venv (demo/record/README.md); no binary is
committed. The pure planning functions are unit-tested in
apps/api/tests/unit/test_edit_walkthrough.py.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

FPS = 25  # Playwright's screencast rate; constant frame rate makes the clips concatenable
FAST_SPEED = 1.5
CARD_SECONDS = 2.0
HIGHLIGHT_CARD_SECONDS = 1.5
MIN_SEGMENT = 0.25  # shorter kept slivers between two cuts only flicker
FONT_DIR = "/usr/share/fonts/truetype/dejavu"


@dataclass(frozen=True)
class Clip:
    """A span of the raw video [s, e) played at ``speed``."""

    s: float
    e: float
    speed: float
    step: int
    frame: tuple[int, int] | None = None  # (width, height) of a smaller viewport, top-left
    source: str = ""  # another raw video (a timeline merged from two takes); "" is the default
    offset: float = 0.0  # what that take's times were shifted by in the merged timeline

    @property
    def duration(self) -> float:
        return (self.e - self.s) / self.speed


@dataclass(frozen=True)
class Card:
    chapter: int
    title: str
    number: int
    total: int
    seconds: float

    @property
    def duration(self) -> float:
        return self.seconds


Item = Clip | Card


# ------------------------------------------------------------------------------- planning


def split_step(step: dict[str, Any], index: int, min_len: float = MIN_SEGMENT) -> list[Clip]:
    """The kept clips of one step: its span minus the dead marks, fast marks at FAST_SPEED."""
    start, end = float(step["start"]), float(step["end"])
    box = step.get("frame")
    frame = (int(box["width"]), int(box["height"])) if box else None
    take = (str(step.get("video", "")), float(step.get("offset", 0.0)))
    marks = step.get("marks", [])
    cuts = sorted({start, end} | {float(m[k]) for m in marks for k in ("s", "e")})
    cuts = [t for t in cuts if start <= t <= end]
    clips: list[Clip] = []
    for s, e in pairwise(cuts):
        mid = (s + e) / 2
        covering = {m["kind"] for m in marks if float(m["s"]) <= mid < float(m["e"])}
        if "dead" in covering or e - s <= 0:
            continue
        speed = FAST_SPEED if "fast" in covering else 1.0
        if clips and clips[-1].speed == speed and abs(clips[-1].e - s) < 1e-6:
            clips[-1] = Clip(clips[-1].s, e, speed, index, frame, *take)
        else:
            clips.append(Clip(s, e, speed, index, frame, *take))
    return [c for c in clips if c.e - c.s >= min_len]


def cap_clips(clips: list[Clip], cap: float) -> list[Clip]:
    """At most ``cap`` seconds of output: the opening third and the closing two thirds, so a
    highlight keeps both the action and its result."""
    total = sum(c.duration for c in clips)
    if cap <= 0 or total <= cap:
        return clips
    return _trim_head(clips, cap / 3) + _trim_tail(clips, total - cap * 2 / 3)


def _trim_head(clips: list[Clip], keep: float) -> list[Clip]:
    out: list[Clip] = []
    for c in clips:
        if keep <= 0:
            break
        take = min(c.duration, keep)
        out.append(Clip(c.s, c.s + take * c.speed, c.speed, c.step, c.frame, c.source, c.offset))
        keep -= take
    return out


def _trim_tail(clips: list[Clip], skip: float) -> list[Clip]:
    """Everything after the first ``skip`` seconds of output."""
    out: list[Clip] = []
    for c in clips:
        if skip >= c.duration:
            skip -= c.duration
            continue
        out.append(Clip(c.s + skip * c.speed, c.e, c.speed, c.step, c.frame, c.source, c.offset))
        skip = 0
    return out


def plan(timeline: dict[str, Any], highlights: bool = False, cap: float = 0.0) -> list[Item]:
    """Cards and clips in playing order."""
    chapters = timeline["chapters"]
    steps = timeline["steps"]
    by_chapter: dict[int, list[Clip]] = {}
    for index, step in enumerate(steps):
        if step["outcome"] == "PENDING" or (highlights and not step.get("highlight")):
            continue
        clips = split_step(step, index)
        if highlights:
            clips = cap_clips(clips, cap)
        by_chapter.setdefault(int(step["chapter"]), []).extend(clips)
    used = [c for c in sorted(by_chapter) if by_chapter[c]]
    seconds = HIGHLIGHT_CARD_SECONDS if highlights else CARD_SECONDS
    items: list[Item] = []
    for number, chapter in enumerate(used, start=1):
        title = chapters[chapter]["title"] if 0 <= chapter < len(chapters) else "PBIP"
        items.append(Card(chapter, title, number, len(used), seconds))
        items.extend(by_chapter[chapter])
    return items


def starts(items: Sequence[Item]) -> list[float]:
    """Each item's start on the edited clock."""
    out, t = [], 0.0
    for item in items:
        out.append(t)
        t += item.duration
    return out


def to_output(items: Sequence[Item], raw: float) -> float | None:
    """Where raw time ``raw`` lands in the edit: inside a clip, or the start of the next clip of
    the same step when it falls in a cut. None when it is not shown at all."""
    clip_starts = starts(items)
    for item, at in zip(items, clip_starts, strict=True):
        if isinstance(item, Clip) and item.s <= raw < item.e:
            return at + (raw - item.s) / item.speed
    return None


def _snap(items: Sequence[Item], raw: float, step: int) -> float | None:
    exact = to_output(items, raw)
    if exact is not None:
        return exact
    for item, at in zip(items, starts(items), strict=True):
        if isinstance(item, Clip) and item.step == step and item.s >= raw:
            return at
    return None


def _step_of(timeline: dict[str, Any], raw: float) -> int | None:
    for index, step in enumerate(timeline["steps"]):
        if float(step["start"]) <= raw <= float(step["end"]):
            return index
    return None


def cues(timeline: dict[str, Any], items: Sequence[Item]) -> list[tuple[float, float, str]]:
    """Subtitle cues on the edited clock: one per card and one per caption, each lasting until
    the next cue (or the end)."""
    marks: list[tuple[float, str]] = []
    for item, at in zip(items, starts(items), strict=True):
        if isinstance(item, Card):
            marks.append((at, f"Chapter {item.number}: {item.title}"))
    for caption in timeline["captions"]:
        raw = float(caption["t"])
        step = _step_of(timeline, raw)
        shown = _snap(items, raw, step) if step is not None else None
        if shown is not None:
            marks.append((shown, str(caption["text"])))
    marks.sort(key=lambda m: m[0])
    end = sum(item.duration for item in items)
    out: list[tuple[float, float, str]] = []
    for i, (at, text) in enumerate(marks):
        until = marks[i + 1][0] if i + 1 < len(marks) else end
        if until - at >= 0.3:
            out.append((at, until, text))
    return out


def srt_time(t: float) -> str:
    ms = round(t * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt(cue_list: Sequence[tuple[float, float, str]]) -> str:
    blocks = [
        f"{n}\n{srt_time(a)} --> {srt_time(b)}\n{text}\n"
        for n, (a, b, text) in enumerate(cue_list, start=1)
    ]
    return "\n".join(blocks)


def chapters_txt(items: Sequence[Item]) -> str:
    """YouTube chapters: "MM:SS Title" (H:MM:SS past an hour), the first at 00:00."""
    lines = []
    for item, at in zip(items, starts(items), strict=True):
        if isinstance(item, Card):
            whole = int(at)
            h, rest = divmod(whole, 3600)
            stamp = (
                f"{h}:{rest // 60:02d}:{rest % 60:02d}"
                if h
                else f"{rest // 60:02d}:{rest % 60:02d}"
            )
            lines.append(f"{stamp} {item.title}")
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------------------- rendering


def card_ass(card: Card, label: str) -> str:
    def clean(text: str) -> str:
        return text.replace("\\", "/").replace("{", "(").replace("}", ")")

    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Title,DejaVu Sans,86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,"
        "100,100,0,0,1,0,0,5,100,100,0,1\n"
        "Style: Kicker,DejaVu Sans,38,&H00FAC593,&H00FAC593,&H00000000,&H00000000,0,0,0,0,"
        "100,100,2,0,1,0,0,5,100,100,0,1\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    kicker = f"{clean(label)}  ·  Chapter {card.number} of {card.total}"
    return (
        head
        + f"Dialogue: 0,0:00:00.00,0:00:30.00,Kicker,,0,0,0,,{{\\pos(960,450)}}{kicker}\n"
        + f"Dialogue: 0,0:00:00.00,0:00:30.00,Title,,0,0,0,,{{\\pos(960,560)}}{clean(card.title)}\n"
    )


def clip_filter(clip: Clip) -> str:
    """Speed, constant frame rate, 1920x1080. A phone-sized viewport sits in the top-left corner
    of the raw frame: it is cropped, enlarged and centred on the card colour."""
    steps = [f"setpts=(PTS-STARTPTS)/{clip.speed}", f"fps={FPS}"]
    if clip.frame:
        width, height = clip.frame
        steps += [
            f"crop={width}:{height}:0:0",
            "scale=-2:1000",
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0f172a",
        ]
    else:
        steps.append("scale=1920:1080")
    return ",".join([*steps, "format=yuv420p"])


H264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
H264 += ["-profile:v", "high", "-r", str(FPS), "-an"]


def _run(ffmpeg: str, args: list[str], cwd: Path) -> None:
    subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *args], cwd=cwd, check=True
    )


def render_items(ffmpeg: str, raw: Path, items: Sequence[Item], work: Path, label: str) -> Path:
    """Encodes every item with identical settings, then joins them without re-encoding."""
    names = []
    for n, item in enumerate(items):
        name = f"part{n:04d}.mp4"
        if isinstance(item, Card):
            (work / f"card{n:04d}.ass").write_text(card_ass(item, label), encoding="utf-8")
            source = ["-f", "lavfi", "-i", f"color=c=0x0f172a:s=1920x1080:r={FPS}:d={item.seconds}"]
            vf = f"subtitles=card{n:04d}.ass:fontsdir={FONT_DIR},format=yuv420p"
        else:
            video = raw.parent / item.source if item.source else raw
            start = item.s - item.offset
            source = ["-ss", f"{start:.3f}", "-t", f"{item.e - item.s:.3f}", "-i", str(video)]
            vf = clip_filter(item)
        _run(ffmpeg, [*source, "-vf", vf, *H264, name], work)
        names.append(name)
    (work / "list.txt").write_text("".join(f"file '{n}'\n" for n in names), encoding="utf-8")
    joined = work / "joined.mp4"
    _run(
        ffmpeg,
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            "list.txt",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            joined.name,
        ],
        work,
    )
    return joined


def write_outputs(
    ffmpeg: str,
    joined: Path,
    out: Path,
    name: str,
    timeline: dict[str, Any],
    items: Sequence[Item],
    webm: bool,
) -> None:
    shutil.copyfile(joined, out / f"{name}.mp4")
    if webm:
        vp9 = ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34", "-row-mt", "1"]
        vp9 += ["-deadline", "realtime", "-cpu-used", "7", "-threads", "2", "-an"]
        _run(ffmpeg, ["-i", str(out / f"{name}.mp4"), *vp9, str(out / f"{name}.webm")], out)
    (out / f"{name}.chapters.txt").write_text(chapters_txt(items), encoding="utf-8")
    (out / f"{name}.srt").write_text(srt(cues(timeline, items)), encoding="utf-8")
    length = sum(item.duration for item in items)
    sys.stdout.write(
        f"{out / name}.mp4 {int(length // 60)}:{int(length % 60):02d} ({len(items)} items)\n"
    )


def find_ffmpeg(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("FFMPEG"):
        return os.environ["FFMPEG"]
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]  # optional: the pinned wheel

        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except ImportError:
        found = shutil.which("ffmpeg")
        if found:
            return found
    raise SystemExit("no ffmpeg: pass --ffmpeg, set FFMPEG or install imageio-ffmpeg (README.md)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("timeline", type=Path, help="the .timeline.json the recorder wrote")
    parser.add_argument("--out", type=Path, help="output directory (default: next to the timeline)")
    parser.add_argument("--name", default="pbip-walkthrough", help="base name of the outputs")
    parser.add_argument("--ffmpeg", help="ffmpeg binary (default: FFMPEG, imageio-ffmpeg, PATH)")
    parser.add_argument(
        "--highlight-cap", type=float, default=22.0, help="max seconds per highlight step"
    )
    parser.add_argument("--no-webm", action="store_true", help="skip the VP9 WebM (slow)")
    parser.add_argument("--skip-full", action="store_true", help="only the highlights cut")
    parser.add_argument("--plan-only", action="store_true", help="print lengths, encode nothing")
    args = parser.parse_args(argv)
    timeline = json.loads(args.timeline.read_text(encoding="utf-8"))
    # Absolute: ffmpeg runs inside a temporary work directory.
    raw = (args.timeline.parent / timeline["video"]).resolve()
    out = (args.out or args.timeline.parent).resolve()
    out.mkdir(parents=True, exist_ok=True)
    cuts = [] if args.skip_full else [(args.name, plan(timeline), "PBIP walkthrough")]
    cuts.append(
        (f"{args.name}-highlights", plan(timeline, True, args.highlight_cap), "PBIP highlights")
    )
    for name, items, label in cuts:
        length = sum(item.duration for item in items)
        sys.stdout.write(
            f"{name}: {int(length // 60)}:{int(length % 60):02d}, {len(items)} items\n"
        )
        if args.plan_only:
            continue
        ffmpeg = find_ffmpeg(args.ffmpeg)
        with tempfile.TemporaryDirectory(prefix="pbip-edit-") as tmp:
            joined = render_items(ffmpeg, raw, items, Path(tmp), label)
            write_outputs(ffmpeg, joined, out, name, timeline, items, not args.no_webm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
