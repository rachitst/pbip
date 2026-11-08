"""The staging availability probe and its report (TASK-103, docs/operations/deploy-oracle-cloud-arm.md).

infra/deploy/uptime-probe.sh runs against a fake `curl` on PATH, so the log line format, the up/down
decision and the options it passes are pinned without a network; scripts/uptime_report.py is fed
hand-written logs whose availability is known.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.uptime_report as report

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
PROBE = ROOT / "infra" / "deploy" / "uptime-probe.sh"
T0 = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)


def _line(offset_s: int, state: str = "up", code: str = "200") -> str:
    at = (T0 + dt.timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{at} {state} {code} 0.050"


def _hour(down: set[int] | None = None, skip: set[int] | None = None) -> list[str]:
    """One hour of 30 s probes (120), with the given probe indexes failing (502) or missing."""
    down, skip = down or set(), skip or set()
    return [
        _line(i * 30, "down" if i in down else "up", "502" if i in down else "200")
        for i in range(120)
        if i not in skip
    ]


def _build(lines: list[str]) -> report.Report:
    return report.build_report(lines, T0, T0 + dt.timedelta(hours=1))


# --- uptime_report.py -------------------------------------------------------------------------------


def test_a_clean_hour_is_100_percent_with_no_incident_or_gap() -> None:
    result = _build(_hour())
    assert (result.up, result.down, result.expected, result.missing) == (120, 0, 120, 0)
    assert result.observed_pct == result.conservative_pct == 100.0
    assert result.incidents == [] and result.gaps == []


def test_consecutive_failures_are_one_incident_ending_at_the_first_success() -> None:
    result = _build(_hour(down={10, 11, 12, 50}))
    assert result.down == 4
    assert [(i.failed_probes, i.codes) for i in result.incidents] == [(3, ("502",)), (1, ("502",))]
    first = result.incidents[0]
    assert first.start == T0 + dt.timedelta(seconds=300)
    assert first.end == T0 + dt.timedelta(seconds=390)  # probe 13 succeeded
    assert first.duration(result.end) == dt.timedelta(seconds=90)
    assert result.observed_pct == pytest.approx(100 * 116 / 120)


def test_missing_probes_lower_only_the_conservative_figure_and_show_as_a_gap() -> None:
    result = _build(_hour(skip=set(range(20, 40))))  # ten minutes with no probe: host down
    assert (result.up, result.missing) == (100, 20)
    assert result.observed_pct == 100.0
    assert result.conservative_pct == pytest.approx(100 * 100 / 120)
    assert [g.duration for g in result.gaps] == [dt.timedelta(seconds=630)]


def test_a_single_late_tick_is_not_a_gap() -> None:
    lines = _hour(skip={30})  # 60 s between two probes = 2 intervals: not a gap
    assert _build(lines).gaps == []


def test_an_ongoing_incident_has_no_end_and_runs_to_the_window_end() -> None:
    result = _build(_hour(down=set(range(110, 120))))
    incident = result.incidents[-1]
    assert incident.end is None
    assert incident.duration(result.end) == dt.timedelta(seconds=300)
    assert "ongoing" in report.format_report(result)


def test_lines_outside_the_window_and_malformed_lines_are_not_counted() -> None:
    lines = [*_hour(), _line(-30), _line(3600), "garbage", "2026-11-01T00:00:00 up 200 0.1", ""]
    result = _build(lines)
    assert result.probes == 120
    assert (
        result.malformed == 2
    )  # "garbage" and the time without an offset; blank lines are skipped


def test_main_prints_the_report_and_gates_on_the_conservative_figure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "probe.log"
    log.write_text("\n".join(_hour(down={1, 2})) + "\n", encoding="utf-8")  # 118/120 = 98.333%
    window = ["--since", "2026-11-01T00:00:00Z", "--until", "2026-11-01T01:00:00Z"]
    assert report.main([str(log), *window]) == 0
    out = capsys.readouterr().out
    assert "98.333%" in out and "Incidents          1" in out
    assert report.main([str(log), *window, "--min-availability", "99"]) == 1
    assert report.main([str(log), *window, "--min-availability", "98"]) == 0


def test_main_uses_the_last_days_before_now_by_default(tmp_path: Path) -> None:
    log = tmp_path / "probe.log"
    log.write_text("\n".join(_hour()) + "\n", encoding="utf-8")
    now = T0 + dt.timedelta(hours=1)
    assert report.main([str(log), "--days", str(1 / 24)], now=now) == 0
    assert report.main([str(log), "--days", "1"], now=now + dt.timedelta(days=2)) == 2  # no probe


def test_main_rejects_unreadable_input_and_an_empty_window(tmp_path: Path) -> None:
    assert report.main([str(tmp_path / "missing.log")]) == 2
    log = tmp_path / "probe.log"
    log.write_text(_line(0) + "\n", encoding="utf-8")
    window = ["--since", "2026-11-02T00:00:00Z", "--until", "2026-11-01T00:00:00Z"]
    assert report.main([str(log), *window]) == 2
    assert report.main([str(log), "--since", "2026-11-01T00:00:00"]) == 2  # no offset: not guessed


# --- uptime-probe.sh --------------------------------------------------------------------------------

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="the probe runs on the Linux host",
)


def _fake_curl(bin_dir: Path, output: str, exit_code: int) -> Path:
    """A `curl` that records its arguments and prints a fixed --write-out result."""
    calls = bin_dir / "curl-args"
    script = bin_dir / "curl"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{calls}"\n'
        f"printf '%s' '{output}'\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return calls


def _probe(tmp_path: Path, output: str, exit_code: int = 0, **env: str) -> tuple[str, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = _fake_curl(bin_dir, output, exit_code)
    log = tmp_path / "logs" / "probe.log"  # the directory does not exist yet
    clean = {k: v for k, v in os.environ.items() if not k.startswith("PROBE_")}
    run_env = {**clean, **env, "PATH": f"{bin_dir}{os.pathsep}{clean['PATH']}"}
    subprocess.run(  # noqa: S603 - fixed arguments, test-owned script
        ["bash", str(PROBE), "https://staging.example/api/health/ready", str(log)],  # noqa: S607
        env=run_env,
        check=True,
        timeout=30,
    )
    return log.read_text(encoding="utf-8"), calls.read_text(encoding="utf-8").splitlines()


@needs_bash
def test_probe_logs_up_for_200_in_the_report_format(tmp_path: Path) -> None:
    text, args = _probe(tmp_path, "200 0.084")
    sample = report.parse_line(text)
    assert sample is not None and sample.up and sample.code == "200"
    assert text.endswith(" up 200 0.084\n")
    assert args[-1] == "https://staging.example/api/health/ready"
    assert "--max-time" in args and "-k" not in args and "--insecure" not in args
    assert "--location" not in args and "-L" not in args  # a redirect is not "up"


@needs_bash
@pytest.mark.parametrize(
    ("output", "exit_code", "expected"),
    [
        ("503 0.010", 0, " down 503 0.010"),
        ("000 10.001", 28, " down 000 10.001"),
        ("", 7, " down 000 0"),
    ],
)
def test_probe_logs_down_for_errors_timeouts_and_no_answer(
    tmp_path: Path, output: str, exit_code: int, expected: str
) -> None:
    text, _ = _probe(tmp_path, output, exit_code)
    assert text.rstrip("\n").endswith(expected)
    assert report.parse_line(text) is not None


@needs_bash
def test_probe_passes_the_private_ca_and_the_local_connect_target(tmp_path: Path) -> None:
    _, args = _probe(
        tmp_path, "200 0.1", PROBE_CACERT="/etc/pbip/ca.crt", PROBE_CONNECT_TO="::127.0.0.1:"
    )
    assert args[args.index("--cacert") + 1] == "/etc/pbip/ca.crt"
    assert args[args.index("--connect-to") + 1] == "::127.0.0.1:"


def test_probe_script_is_strict_bash() -> None:
    text = PROBE.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text


def test_the_guides_probe_timer_ticks_on_the_wall_clock_at_the_reports_interval() -> None:
    """Regression (staging host, 8 Nov 2026): the guide's timer used OnUnitActiveSec=30s, which ran
    about every 30.9 s, so the report counted about 3% of a perfect week as missing probes and the
    conservative figure could never reach 99%. The timer must fire on wall-clock ticks every
    DEFAULT_INTERVAL_S seconds."""
    guide = (ROOT / "docs" / "operations" / "deploy-oracle-cloud-arm.md").read_text(
        encoding="utf-8"
    )
    timer = guide.split("pbip-uptime-probe.timer >/dev/null", 1)[1].split("EOF", 2)[1]
    directives = [
        line.strip() for line in timer.splitlines() if "=" in line and not line.startswith("#")
    ]
    assert not [d for d in directives if d.startswith(("OnUnitActiveSec", "OnBootSec"))]
    assert f"OnCalendar=*-*-* *:*:00/{report.DEFAULT_INTERVAL_S}" in directives
