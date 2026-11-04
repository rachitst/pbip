"""Availability report from the staging uptime probe log (TASK-103).

    python3 scripts/uptime_report.py ~/pbip/uptime/probe.log                 # last 7 days
    python3 scripts/uptime_report.py probe.log --since 2026-11-01T00:00:00Z \
        --until 2026-12-01T00:00:00Z                                         # a calendar month
    python3 scripts/uptime_report.py probe.log --days 30 --min-availability 99   # exit 1 below 99%

``infra/deploy/uptime-probe.sh`` appends one line per probe (every 30 s from a systemd timer):

    2026-11-04T10:00:00Z up 200 0.084
    2026-11-04T10:00:30Z down 502 0.012

Two figures are printed, because a probe that did not run proves nothing either way:
  * observed availability = successful probes / probes that ran;
  * conservative availability = successful probes / probes that SHOULD have run in the window
    (window length / interval). Every missing probe counts as down: the host, its clock or the timer
    was not running, and the service cannot be shown to have been up. This is the NFR figure.
Incidents are runs of consecutive failed probes; gaps are stretches with no probe at all.
Standard library only, so it runs on the staging host's system Python. Exit codes: 0 report printed
(and at or above --min-availability if given), 1 below --min-availability, 2 no probe in the window
or unreadable input.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import math
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_INTERVAL_S = 30
GAP_FACTOR = 2  # a silence longer than two intervals is a gap (one late timer tick is not)


@dataclass(frozen=True, slots=True)
class Sample:
    at: dt.datetime
    up: bool
    code: str


@dataclass(frozen=True, slots=True)
class Incident:
    start: dt.datetime
    end: dt.datetime | None  # first successful probe afterwards; None while still failing
    failed_probes: int
    codes: tuple[str, ...]

    def duration(self, window_end: dt.datetime) -> dt.timedelta:
        return (self.end or window_end) - self.start


@dataclass(frozen=True, slots=True)
class Gap:
    start: dt.datetime
    end: dt.datetime

    @property
    def duration(self) -> dt.timedelta:
        return self.end - self.start


@dataclass(slots=True)
class Report:
    start: dt.datetime
    end: dt.datetime
    interval_s: int
    up: int = 0
    down: int = 0
    malformed: int = 0
    incidents: list[Incident] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)

    @property
    def probes(self) -> int:
        return self.up + self.down

    @property
    def expected(self) -> int:
        """Probes the timer should have run; never fewer than the probes that did run."""
        return max(
            self.probes, math.floor((self.end - self.start).total_seconds() / self.interval_s)
        )

    @property
    def missing(self) -> int:
        return self.expected - self.probes

    @property
    def observed_pct(self) -> float | None:
        return 100.0 * self.up / self.probes if self.probes else None

    @property
    def conservative_pct(self) -> float | None:
        return 100.0 * self.up / self.expected if self.expected else None


def parse_time(value: str) -> dt.datetime:
    """ISO-8601 with an explicit offset or ``Z``; naive times are rejected (no guessing a zone)."""
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"time without a UTC offset: {value!r}")
    return parsed.astimezone(dt.UTC)


def parse_line(line: str) -> Sample | None:
    """One probe line, or None for a blank or malformed line."""
    parts = line.split()
    if len(parts) != 4 or parts[1] not in ("up", "down"):
        return None
    try:
        at = parse_time(parts[0])
    except ValueError:
        return None
    return Sample(at=at, up=parts[1] == "up", code=parts[2])


def _gaps(
    times: Sequence[dt.datetime], start: dt.datetime, end: dt.datetime, interval_s: int
) -> list[Gap]:
    limit = dt.timedelta(seconds=GAP_FACTOR * interval_s)
    edges = [start, *times, end]
    return [Gap(a, b) for a, b in itertools.pairwise(edges) if b - a > limit]


def _incidents(samples: Sequence[Sample]) -> list[Incident]:
    incidents: list[Incident] = []
    failing: list[Sample] = []
    for sample in samples:
        if not sample.up:
            failing.append(sample)
            continue
        if failing:
            codes = tuple(sorted({s.code for s in failing}))
            incidents.append(Incident(failing[0].at, sample.at, len(failing), codes))
            failing = []
    if failing:
        incidents.append(
            Incident(failing[0].at, None, len(failing), tuple(sorted({s.code for s in failing})))
        )
    return incidents


def build_report(
    lines: Iterable[str], start: dt.datetime, end: dt.datetime, interval_s: int = DEFAULT_INTERVAL_S
) -> Report:
    report = Report(start=start, end=end, interval_s=interval_s)
    samples: list[Sample] = []
    for line in lines:
        if not line.strip():
            continue
        sample = parse_line(line)
        if sample is None:
            report.malformed += 1
        elif start <= sample.at < end:
            samples.append(sample)
    samples.sort(key=lambda s: s.at)
    report.up = sum(1 for s in samples if s.up)
    report.down = len(samples) - report.up
    report.incidents = _incidents(samples)
    report.gaps = _gaps([s.at for s in samples], start, end, interval_s)
    return report


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}%"


def _stamp(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _span(value: dt.timedelta) -> str:
    total = int(value.total_seconds())
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m{seconds:02d}s"


def format_report(report: Report) -> str:
    r = report
    out = [
        f"Window             {_stamp(r.start)} .. {_stamp(r.end)} (probe every {r.interval_s} s)",
        f"Probes             {r.probes} ran, {r.expected} expected, {r.missing} missing",
        f"Successful         {r.up}; failed {r.down}; malformed lines {r.malformed}",
        f"Observed           {_pct(r.observed_pct)}  (successful / probes that ran)",
        f"Conservative       {_pct(r.conservative_pct)}  (successful / expected; missing = down)",
        f"Incidents          {len(r.incidents)}",
    ]
    for incident in report.incidents:
        end = _stamp(incident.end) if incident.end else "ongoing"
        out.append(
            f"  {_stamp(incident.start)} .. {end}  {_span(incident.duration(report.end))}  "
            f"{incident.failed_probes} failed probe(s), HTTP {','.join(incident.codes)}"
        )
    out.append(f"Gaps (no probe)    {len(report.gaps)}")
    out.extend(f"  {_stamp(g.start)} .. {_stamp(g.end)}  {_span(g.duration)}" for g in report.gaps)
    return "\n".join(out)


def _window(args: argparse.Namespace, now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    end = parse_time(args.until) if args.until else now
    start = parse_time(args.since) if args.since else end - dt.timedelta(days=args.days)
    if start >= end:
        raise ValueError("the window is empty: --since must be before --until")
    return start, end


def main(argv: Sequence[str] | None = None, now: dt.datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "logs", nargs="+", type=Path, help="probe log file(s) written by uptime-probe.sh"
    )
    parser.add_argument(
        "--days", type=float, default=7.0, help="window length ending at --until/now"
    )
    parser.add_argument(
        "--since", help="window start, ISO-8601 with offset (e.g. 2026-11-01T00:00:00Z)"
    )
    parser.add_argument("--until", help="window end, ISO-8601 with offset (default: now)")
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL_S, help="probe interval in seconds"
    )
    parser.add_argument(
        "--min-availability", type=float, help="exit 1 if the conservative figure is lower"
    )
    args = parser.parse_args(argv)
    if args.interval <= 0 or args.days <= 0:
        parser.error("--interval and --days must be positive")
    try:
        start, end = _window(args, now or dt.datetime.now(dt.UTC))
        lines = [
            line for path in args.logs for line in path.read_text(encoding="utf-8").splitlines()
        ]
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"uptime_report: {exc}\n")
        return 2
    report = build_report(lines, start, end, args.interval)
    sys.stdout.write(format_report(report) + "\n")
    if report.probes == 0:
        sys.stderr.write("uptime_report: no probe in the window\n")
        return 2
    pct = report.conservative_pct
    if args.min_availability is not None and pct is not None and pct < args.min_availability:
        sys.stderr.write(
            f"uptime_report: availability {pct:.3f}% is below {args.min_availability}%\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
