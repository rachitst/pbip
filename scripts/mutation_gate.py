"""TASK-086 mutation-testing gate for the financial core (docs/testing/mutation-report.md).

Runs mutmut over ``modules/ledger/service.py`` and ``modules/payments/service.py`` (configured in
``apps/api/pyproject.toml``) against the TEST database, then fails unless the mutation score is at
least the threshold and every mutant was actually run.

    uv run python -m scripts.mutation_gate                # full run (~3 h on a 2-CPU laptop)
    uv run python -m scripts.mutation_gate --score-only   # just score the last run

ONE worker (``--max-children 1``) is deliberate: the integration tests share one database and
truncate it per test, so parallel mutants kill each other's tests and report false kills.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass

from scripts.dev import ROOT, build_test_env

API_DIR = ROOT / "apps" / "api"
DEFAULT_THRESHOLD = 0.85


@dataclass(frozen=True, slots=True)
class Score:
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def killed(self) -> int:
        return self.counts.get("killed", 0)

    @property
    def ratio(self) -> float:
        return self.killed / self.total if self.total else 0.0


def parse_results(text: str) -> Score:
    """Count statuses in ``mutmut results --all true`` output (``<mutant name>: <status>``)."""
    counts: Counter[str] = Counter()
    for line in text.splitlines():
        name, sep, status = line.strip().rpartition(": ")
        if sep and "__mutmut_" in name:
            counts[status.strip()] += 1
    return Score(dict(counts))


def verdict(score: Score, threshold: float) -> tuple[bool, str]:
    """Pass only on a complete run whose killed share meets the threshold."""
    unrun = score.counts.get("not checked", 0)
    summary = (
        f"mutation score {score.killed}/{score.total} = {score.ratio:.1%} "
        f"(threshold {threshold:.0%}); statuses: {dict(sorted(score.counts.items()))}"
    )
    if score.total == 0:
        return False, "no mutants found: run mutmut first"
    if unrun:
        return False, f"{summary}; {unrun} mutants were not run, so the score is incomplete"
    return score.ratio >= threshold, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--score-only", action="store_true", help="score the last run only")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args(argv)
    env = build_test_env()
    env["DATABASE_URL"] = env["TEST_DATABASE_URL"]  # never mutate against the dev database
    if not args.score_only:
        subprocess.run(
            ["uv", "run", "mutmut", "run", "--max-children", "1"], cwd=API_DIR, env=env, check=False
        )
    results = subprocess.run(
        ["uv", "run", "mutmut", "results", "--all", "true"],
        cwd=API_DIR,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    ok, message = verdict(parse_results(results.stdout), args.threshold)
    sys.stdout.write(("PASS: " if ok else "FAIL: ") + message + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
