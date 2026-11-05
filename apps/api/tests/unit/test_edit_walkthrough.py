"""The walkthrough editor (demo/record/edit-walkthrough.py) cuts the raw recording by the recorder's
timeline. These tests pin the planning, which decides what the viewer sees: dead time is cut, fast
marks play at 1.5x, each chapter gets a card, PENDING steps take no time, highlights are capped,
and the chapters and subtitles land on the edited clock. Nothing here runs ffmpeg.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]


def _load() -> ModuleType:
    path = ROOT / "demo/record/edit-walkthrough.py"
    spec = importlib.util.spec_from_file_location("edit_walkthrough", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["edit_walkthrough"] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


ew = _load()


def _step(
    chapter: int,
    start: float,
    end: float,
    marks: list[dict[str, Any]] | None = None,
    outcome: str = "PASS",
    highlight: bool = False,
) -> dict[str, Any]:
    return {
        "chapter": chapter,
        "name": f"step {start}",
        "start": start,
        "end": end,
        "outcome": outcome,
        "detail": "",
        "highlight": highlight,
        "marks": marks or [],
    }


def _timeline(
    steps: list[dict[str, Any]], captions: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "version": 1,
        "video": "raw.webm",
        "chapters": [
            {"title": "Sign-in", "highlight": True},
            {"title": "Payments", "highlight": False},
        ],
        "steps": steps,
        "captions": captions or [],
    }


def test_dead_marks_are_cut_and_fast_marks_play_at_one_and_a_half() -> None:
    step = _step(
        0,
        10.0,
        30.0,
        [{"kind": "dead", "s": 12.0, "e": 15.0}, {"kind": "fast", "s": 20.0, "e": 26.0}],
    )
    clips = ew.split_step(step, 0)
    assert [(c.s, c.e, c.speed) for c in clips] == [
        (10.0, 12.0, 1.0),
        (15.0, 20.0, 1.0),
        (20.0, 26.0, 1.5),
        (26.0, 30.0, 1.0),
    ]
    assert sum(c.duration for c in clips) == pytest.approx(2 + 5 + 4 + 4)


def test_dead_wins_over_fast_and_slivers_are_dropped() -> None:
    marks = [
        {"kind": "fast", "s": 0.0, "e": 10.0},
        {"kind": "dead", "s": 2.0, "e": 9.9},  # leaves a 0.1 s sliver of fast before 10.0
    ]
    clips = ew.split_step(_step(0, 0.0, 12.0, marks), 3)
    assert [(c.s, c.e, c.speed, c.step) for c in clips] == [
        (0.0, 2.0, 1.5, 3),
        (10.0, 12.0, 1.0, 3),
    ]


def test_marks_outside_the_step_do_not_extend_it() -> None:
    clips = ew.split_step(_step(0, 5.0, 8.0, [{"kind": "dead", "s": 1.0, "e": 6.0}]), 0)
    assert [(c.s, c.e) for c in clips] == [(6.0, 8.0)]


def test_plan_puts_a_card_before_each_chapter_and_skips_pending_steps() -> None:
    timeline = _timeline(
        [
            _step(0, 0.0, 4.0),
            _step(1, 5.0, 5.0, outcome="PENDING"),
            _step(1, 6.0, 9.0),
        ]
    )
    items = ew.plan(timeline)
    kinds = [type(i).__name__ for i in items]
    assert kinds == ["Card", "Clip", "Card", "Clip"]
    cards = [i for i in items if isinstance(i, ew.Card)]
    assert [(c.title, c.number, c.total, c.seconds) for c in cards] == [
        ("Sign-in", 1, 2, 2.0),
        ("Payments", 2, 2, 2.0),
    ]
    assert sum(i.duration for i in items) == pytest.approx(2 + 4 + 2 + 3)


def test_a_chapter_with_only_pending_steps_gets_no_card() -> None:
    items = ew.plan(_timeline([_step(0, 0.0, 4.0), _step(1, 5.0, 5.0, outcome="PENDING")]))
    assert [i.title for i in items if isinstance(i, ew.Card)] == ["Sign-in"]


def test_highlights_keep_only_marked_steps_capped_head_and_tail() -> None:
    timeline = _timeline([_step(0, 0.0, 60.0, highlight=True), _step(1, 70.0, 80.0)])
    items = ew.plan(timeline, highlights=True, cap=30.0)
    clips = [i for i in items if isinstance(i, ew.Clip)]
    assert [(c.s, c.e) for c in clips] == [(0.0, 10.0), (40.0, 60.0)]
    assert [i.seconds for i in items if isinstance(i, ew.Card)] == [1.5]


def test_chapters_start_at_zero_on_the_edited_clock() -> None:
    items = ew.plan(_timeline([_step(0, 0.0, 70.0), _step(1, 100.0, 103.0)]))
    assert ew.chapters_txt(items) == "00:00 Sign-in\n01:12 Payments\n"


def test_captions_move_to_the_edited_clock_and_snap_out_of_cuts() -> None:
    steps = [_step(0, 10.0, 30.0, [{"kind": "dead", "s": 12.0, "e": 15.0}])]
    captions = [{"t": 10.5, "text": "first"}, {"t": 13.0, "text": "said during a wait"}]
    timeline = _timeline(steps, captions)
    items = ew.plan(timeline)
    cues = ew.cues(timeline, items)
    # Card 0-2 s; clip 10-12 raw -> 2-4; clip 15-30 raw -> 4-19.
    assert cues == [
        (0.0, pytest.approx(2.5), "Chapter 1: Sign-in"),
        (pytest.approx(2.5), 4.0, "first"),
        (4.0, 19.0, "said during a wait"),
    ]


def test_srt_format() -> None:
    text = ew.srt([(0.0, 2.5, "Chapter 1: Sign-in"), (3661.2, 3662.0, "late")])
    assert text == (
        "1\n00:00:00,000 --> 00:00:02,500\nChapter 1: Sign-in\n\n"
        "2\n01:01:01,200 --> 01:01:02,000\nlate\n"
    )


def test_card_text_cannot_inject_subtitle_overrides() -> None:
    card = ew.Card(0, "Odd {\\b0} title", 1, 1, 2.0)
    ass = ew.card_ass(card, "PBIP")
    assert "Odd (/b0) title" in ass


def test_a_phone_viewport_is_cropped_and_centred() -> None:
    step = _step(0, 0.0, 5.0)
    step["frame"] = {"width": 390, "height": 844}
    [clip] = ew.split_step(step, 0)
    assert clip.frame == (390, 844)
    assert "crop=390:844:0:0" in ew.clip_filter(clip)
    assert "pad=1920:1080" in ew.clip_filter(clip)
    [desktop] = ew.split_step(_step(0, 0.0, 5.0), 0)
    assert "crop" not in ew.clip_filter(desktop)
    assert "scale=1920:1080" in ew.clip_filter(desktop)


def test_a_step_from_another_take_keeps_its_video_and_offset() -> None:
    step = _step(0, 100_010.0, 100_015.0)
    step["video"] = "take-2.webm"
    step["offset"] = 100_000.0
    [clip] = ew.split_step(step, 0)
    assert (clip.source, clip.offset) == ("take-2.webm", 100_000.0)
    assert (clip.s - clip.offset, clip.e - clip.offset) == (10.0, 15.0)
