"""Build the short demo cut from the edited A-to-Z walkthrough (owner's spec, 21 Nov 2026).

    python demo/record/build-demo-cut.py ~/Desktop/Rachit/finora-video/finora-walkthrough.mp4 \
        --out ~/Desktop/Rachit/finora-video/finora-demo-cut.mp4

Fifteen clips, in order, each with an exact length (4:40.5 in total). Each clip is found in
the walkthrough by its captions (the `.srt` next to the video, written by edit-walkthrough.py):
it starts where the first matching caption starts and ends where the last matching caption ends.
A clip shorter than its length holds its last frame; a longer one is trimmed at the end. Every
clip fades in and out over 0.25 s. Output: 1920x1080 H.264, no audio, and a `.json` next to it
listing each clip's window in the source and its start in the cut. ffmpeg as in
edit-walkthrough.py (--ffmpeg, FFMPEG, imageio-ffmpeg).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

FADE = 0.25
WIDTH, HEIGHT, FPS = 1920, 1080, 30


@dataclass(frozen=True)
class Clip:
    title: str
    first: str  # regex for the clip's first caption
    last: str  # regex for the clip's last caption
    seconds: float
    # Seconds cut from the end of the caption window when the next step's page appears before
    # the next caption does (the hold then repeats the clip's own last frame instead).
    end_trim: float = 0.0


CLIPS: tuple[Clip, ...] = (
    Clip(
        "Sign-in code: wrong code refused, right code accepted",
        r"^Signing in: password first, then the authenticator code",
        r"^The right code: signed in",
        11.5,
    ),
    Clip(
        "Total balance, then transaction search for Salary",
        r"^Total balance across her accounts",
        r"^Search 'Salary'",
        17,
    ),
    Clip(
        "INR 35,000 to a new payee held for review; INR 20,000 to Bob with step-up, posted",
        r"^Send money: INR 35,000\.00",
        r"^Code accepted: posted to the ledger",
        20,
    ),
    Clip(
        "Financial Health Score, spending by category, balance forecast",
        r"^Financial Health Score",
        r"^Balance forecast",
        20,
    ),
    Clip(
        "Loans for you, live EMI, eligibility, approved and disbursed, repayment schedule",
        r"^Loans for you",
        r"^Her repayment schedule",
        19,
    ),
    Clip(
        "Recommendations with their reasons",
        r"^Recommendations always say why",
        r"^Recommendations always say why",
        9,
    ),
    Clip(
        "Ask Finora: seven questions and another customer's data refused",
        r"^Ask Finora: read-only tools",
        r"^Another customer's data: refused",
        37,
    ),
    Clip(
        "A customer opening a staff page gets 'not found'",
        r"^A customer opening a staff page",
        r"^A customer opening a staff page",
        10,
    ),
    Clip(
        "Security monitoring, then fraud demo lab F1 held",
        r"^Security monitoring:",
        r"^Held for review\. The lab shows",
        20,
    ),
    Clip(
        "Alert queue, alert detail (rule points, SHAP, model version), reject, "
        "release Carol's payment",
        r"^The alert queue",
        r"^Released: the held money",
        35,
        end_trim=2.0,  # the next alert starts loading before the next caption
    ),
    Clip("Fraud dashboard", r"^The fraud dashboard", r"^The reasons behind them", 13),
    Clip(
        "Compliance & risk, then audit log hash-chain verify",
        r"^Compliance & risk:",
        r"^Hash chain intact",
        24,
    ),
    Clip(
        "Customer insights, then behavioural analytics",
        r"^Customer insights:",
        r"^Behavioural analytics:",
        25,
    ),
    Clip(
        "Fraud model evaluation report tables",
        r"^The four promotion bars",
        r"^Each component, and the rules-only fail-safe",
        10,
    ),
    Clip(
        "Grafana API panel: request rate, errors, latency",
        r"^API: request rate, errors",
        r"^API: request rate, errors",
        10,
    ),
)


@dataclass(frozen=True)
class Caption:
    start: float
    end: float
    text: str


def _seconds(stamp: str) -> float:
    hours, minutes, rest = stamp.split(":")
    secs, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(secs) + int(millis) / 1000


def read_srt(path: Path) -> list[Caption]:
    captions = []
    for block in path.read_text(encoding="utf-8").strip().split("\n\n"):
        lines = block.splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start, end = (s.strip() for s in lines[1].split("-->"))
        captions.append(Caption(_seconds(start), _seconds(end), " ".join(lines[2:]).strip()))
    return captions


def locate(captions: list[Caption], clips: tuple[Clip, ...]) -> list[tuple[Clip, float, float]]:
    """Each clip's [start, end] in the source, searched forward so the clips keep story order."""
    found = []
    cursor = 0
    for clip in clips:
        first = next(
            (i for i in range(cursor, len(captions)) if re.search(clip.first, captions[i].text)),
            None,
        )
        if first is None:
            raise SystemExit(f"no caption matches {clip.first!r} (clip: {clip.title})")
        last = next(
            (i for i in range(first, len(captions)) if re.search(clip.last, captions[i].text)), None
        )
        if last is None:
            raise SystemExit(f"no caption matches {clip.last!r} after {clip.first!r}")
        found.append((clip, captions[first].start, captions[last].end - clip.end_trim))
        cursor = last + 1
    return found


def find_ffmpeg(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("FFMPEG"):
        return os.environ["FFMPEG"]
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]  # optional, scratch venv only

        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except ImportError:
        pass
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    raise SystemExit("no ffmpeg: pass --ffmpeg, set FFMPEG or install imageio-ffmpeg (README.md)")


def render_clip(
    ffmpeg: str, source: Path, start: float, end: float, seconds: float, out: Path
) -> None:
    """One clip at exactly `seconds`: trimmed or held on its last frame, faded at both edges."""
    span = end - start
    hold = max(0.0, seconds - span)
    filters = [
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease",
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2",
        f"fps={FPS}",
        "setsar=1",
    ]
    if hold > 0:
        filters.append(f"tpad=stop_mode=clone:stop_duration={hold:.3f}")
    filters += [
        f"trim=duration={seconds:.3f}",
        "setpts=PTS-STARTPTS",
        f"fade=t=in:st=0:d={FADE}",
        f"fade=t=out:st={seconds - FADE:.3f}:d={FADE}",
        "format=yuv420p",
    ]
    subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{min(span, seconds) + 0.5:.3f}",
            "-i",
            str(source),
            "-an",
            "-vf",
            ",".join(filters),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(FPS),
            "-video_track_timescale",
            str(FPS * 1000),
            "-frames:v",
            str(round(seconds * FPS)),
            str(out),
        ],
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "source", type=Path, help="the edited walkthrough video (its .srt must sit next to it)"
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="the cut (.mp4); the .json is written beside it"
    )
    parser.add_argument("--ffmpeg", help="ffmpeg binary (default: FFMPEG, imageio-ffmpeg, PATH)")
    parser.add_argument(
        "--plan-only", action="store_true", help="print the clip windows without encoding"
    )
    args = parser.parse_args(argv)

    captions = read_srt(args.source.with_suffix(".srt"))
    plan = locate(captions, CLIPS)
    total = sum(clip.seconds for clip, _, _ in plan)
    entries, at = [], 0.0
    for index, (clip, start, end) in enumerate(plan, 1):
        entries.append(
            {
                "clip": index,
                "title": clip.title,
                "seconds": clip.seconds,
                "source_start": round(start, 3),
                "source_end": round(end, 3),
                "source_seconds": round(end - start, 3),
                "cut_start": round(at, 3),
                "fit": "hold last frame"
                if end - start < clip.seconds
                else "trim end"
                if end - start > clip.seconds
                else "exact",
            }
        )
        at += clip.seconds
        window = f"src {start:7.2f}-{end:7.2f} ({end - start:5.2f}s)"
        sys.stdout.write(f"{index:2d} {clip.seconds:5.1f}s  {window}  {clip.title}\n")
    sys.stdout.write(f"total {total:.1f}s ({int(total // 60)}:{total % 60:04.1f})" + "\n")
    if args.plan_only:
        return 0

    ffmpeg = find_ffmpeg(args.ffmpeg)
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for entry in entries:
            part = Path(tmp) / f"clip{entry['clip']:02d}.mp4"
            render_clip(
                ffmpeg,
                args.source,
                entry["source_start"],
                entry["source_end"],
                entry["seconds"],
                part,
            )
            parts.append(part)
        listing = Path(tmp) / "parts.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(args.out),
            ],
            check=True,
        )
    manifest = {
        "source": args.source.name,
        "output": args.out.name,
        "width": WIDTH,
        "height": HEIGHT,
        "fps": FPS,
        "codec": "H.264",
        "audio": False,
        "fade_seconds": FADE,
        "total_seconds": total,
        "clips": entries,
    }
    args.out.with_suffix(".json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    sys.stdout.write(f"wrote {args.out} and {args.out.with_suffix('.json').name}" + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
