#!/usr/bin/env python3
"""PBIP Backlog Management and GitHub Importer.

Validates docs/backlog/backlog.toml, analyzes sprint workload, and imports
actionable tasks into GitHub Issues and Projects.

Usage:
    python scripts/backlog.py --validate
    python scripts/backlog.py OWNER/REPO --dry-run
    python scripts/backlog.py OWNER/REPO --project-number 1
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKLOG_FILE = ROOT / "docs" / "backlog" / "backlog.toml"
STATE_FILE = ROOT / ".backlog-state.json"

POINT_MAP: dict[str, int] = {"S": 1, "M": 2, "L": 4}
PERSON_DAYS_MAP: dict[str, float] = {"S": 0.5, "M": 1.5, "L": 3.5}
VALID_PRIORITIES = {"must", "should", "could"}
VALID_SIZES = {"S", "M", "L"}
VALID_LABELS = {
    "api",
    "web",
    "ml",
    "infra",
    "docs",
    "security",
    "test",
    "chore",
    "compliance",
    "analytics",
}
BUFFER_CAPACITY_DAYS: float = 15.0


@dataclass
class Task:
    id: str
    title: str
    epic: str
    sprint: int
    priority: str
    size: str
    labels: list[str]
    outcome: str
    acceptance_criteria: list[str]
    dependencies: list[str]
    definition_of_done: str

    @property
    def points(self) -> int:
        return POINT_MAP.get(self.size, 1)

    @property
    def person_days(self) -> float:
        return PERSON_DAYS_MAP.get(self.size, 0.5)


def load_backlog(path: Path = BACKLOG_FILE) -> list[Task]:
    """Load and parse tasks from backlog.toml."""
    if not path.exists():
        sys.stderr.write(f"Error: Backlog file not found at {path}\n")
        sys.exit(1)

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except Exception as exc:
        sys.stderr.write(f"Error parsing TOML in {path}: {exc}\n")
        sys.exit(1)

    raw_tasks = data.get("tasks", [])
    if not isinstance(raw_tasks, list):
        sys.stderr.write("Error: 'tasks' must be an array of tables in backlog.toml\n")
        sys.exit(1)

    tasks: list[Task] = []
    for raw in raw_tasks:
        tasks.append(
            Task(
                id=str(raw.get("id", "")).strip(),
                title=str(raw.get("title", "")).strip(),
                epic=str(raw.get("epic", "")).strip(),
                sprint=int(raw.get("sprint", 1)),
                priority=str(raw.get("priority", "")).lower().strip(),
                size=str(raw.get("size", "")).upper().strip(),
                labels=[str(lbl).strip().lower() for lbl in raw.get("labels", [])],
                outcome=str(raw.get("outcome", "")).strip(),
                acceptance_criteria=[
                    str(c).strip() for c in raw.get("acceptance_criteria", []) if str(c).strip()
                ],
                dependencies=[
                    str(d).strip() for d in raw.get("dependencies", []) if str(d).strip()
                ],
                definition_of_done=str(raw.get("definition_of_done", "")).strip(),
            )
        )
    return tasks


def _validate_task_identity(t: Task, task_map: dict[str, Task], errors: list[str]) -> bool:
    """Validate task ID format, uniqueness, and title."""
    if not t.id:
        errors.append(f"Task with title '{t.title}' is missing an 'id'.")
        return False
    if not re.match(r"^TASK-\d{3}$", t.id):
        errors.append(f"Task ID '{t.id}' must match format 'TASK-###'.")
    if t.id in task_map:
        errors.append(f"Duplicate task ID found: '{t.id}'.")
    task_map[t.id] = t
    if not t.title:
        errors.append(f"Task {t.id} is missing a title.")
    return True


def _validate_task_metadata(t: Task, errors: list[str]) -> None:
    """Validate sprint, priority, size, criteria, outcome, and labels."""
    if not (1 <= t.sprint <= 7):
        errors.append(f"Task {t.id} has invalid sprint {t.sprint} (must be 1-7).")
    if t.priority not in VALID_PRIORITIES:
        errors.append(
            f"Task {t.id} has invalid priority '{t.priority}' (allowed: {VALID_PRIORITIES})."
        )
    if t.size not in VALID_SIZES:
        errors.append(f"Task {t.id} has invalid size '{t.size}' (allowed: {VALID_SIZES}).")
    if not t.acceptance_criteria:
        errors.append(f"Task {t.id} must have at least one acceptance criterion.")
    if not t.outcome:
        errors.append(f"Task {t.id} must have a defined outcome.")
    if not t.definition_of_done:
        errors.append(f"Task {t.id} must have a Definition of Done.")

    invalid_labels = set(t.labels) - VALID_LABELS
    if invalid_labels:
        errors.append(
            f"Task {t.id} has unsupported labels: {invalid_labels} (allowed: {VALID_LABELS})."
        )


def _validate_task_fields(t: Task, task_map: dict[str, Task], errors: list[str]) -> None:
    """Validate scalar fields and label values on a single task."""
    if _validate_task_identity(t, task_map, errors):
        _validate_task_metadata(t, errors)


def _validate_dependencies(
    tasks: list[Task], task_map: dict[str, Task], errors: list[str]
) -> dict[str, list[str]]:
    """Verify that dependencies exist, are well-sequenced, and build graph."""
    graph: dict[str, list[str]] = {t.id: [] for t in tasks}
    for t in tasks:
        for dep in t.dependencies:
            if dep not in task_map:
                errors.append(f"Task {t.id} depends on non-existent task '{dep}'.")
            elif dep == t.id:
                errors.append(f"Task {t.id} cannot depend on itself.")
            else:
                graph[t.id].append(dep)
                dep_sprint = task_map[dep].sprint
                if dep_sprint > t.sprint:
                    errors.append(
                        f"Sequence warning: Task {t.id} (Sprint {t.sprint}) depends on "
                        f"Task {dep} scheduled later in Sprint {dep_sprint}."
                    )
    return graph


def _check_cycles(graph: dict[str, list[str]], errors: list[str]) -> None:
    """Detect cycles in dependency graph using depth-first search."""
    visited: dict[str, int] = {}

    def dfs(node: str, path: list[str]) -> bool:
        visited[node] = 1
        path.append(node)
        for neighbor in graph.get(node, []):
            if visited.get(neighbor) == 1:
                cycle_str = " -> ".join([*path, neighbor])
                errors.append(f"Circular dependency detected: {cycle_str}")
                return True
            if visited.get(neighbor, 0) == 0 and dfs(neighbor, path):
                return True
        path.pop()
        visited[node] = 2
        return False

    for node in graph:
        if visited.get(node, 0) == 0:
            dfs(node, [])


def validate_backlog(tasks: list[Task]) -> tuple[bool, list[str]]:
    """Comprehensive validation of task definitions, schema, and dependency DAG."""
    errors: list[str] = []
    task_map: dict[str, Task] = {}

    for t in tasks:
        _validate_task_fields(t, task_map, errors)

    graph = _validate_dependencies(tasks, task_map, errors)
    _check_cycles(graph, errors)

    return len(errors) == 0, errors


def print_backlog_statistics(
    tasks: list[Task], capacity_days: float = BUFFER_CAPACITY_DAYS
) -> None:
    """Print detailed workload breakdown and capacity analysis."""
    sys.stdout.write("\n=======================================================\n")
    sys.stdout.write("      PBIP 7-Sprint Backlog Workload & Capacity Report\n")
    sys.stdout.write("=======================================================\n\n")

    sprint_tasks: dict[int, list[Task]] = defaultdict(list)
    for t in tasks:
        sprint_tasks[t.sprint].append(t)

    sys.stdout.write(
        f"{'Sprint':<8} | {'Tasks':<6} | {'Points':<7} | {'Must Pts':<9} | "
        f"{'Should':<7} | {'Could':<6} | {'Person-Days':<12} | {'Load Status'}\n"
    )
    sys.stdout.write("-" * 84 + "\n")

    total_tasks = 0
    total_points = 0
    total_must_points = 0
    total_person_days = 0.0

    for s in range(1, 8):
        s_list = sprint_tasks.get(s, [])
        count = len(s_list)
        pts = sum(t.points for t in s_list)
        must_pts = sum(t.points for t in s_list if t.priority == "must")
        should_pts = sum(t.points for t in s_list if t.priority == "should")
        could_pts = sum(t.points for t in s_list if t.priority == "could")
        p_days = sum(t.person_days for t in s_list)

        total_tasks += count
        total_points += pts
        total_must_points += must_pts
        total_person_days += p_days

        status = "BALANCED"
        if p_days > capacity_days * 1.5:
            status = "OVERLOADED (Requires deferral/split)"
        elif p_days > capacity_days:
            status = "TIGHT (Near capacity)"

        sys.stdout.write(
            f"Sprint {s:<2} | {count:<6} | {pts:<7} | {must_pts:<9} | "
            f"{should_pts:<7} | {could_pts:<6} | {p_days:<12.1f} | {status}\n"
        )

    sys.stdout.write("-" * 84 + "\n")
    sys.stdout.write(
        f"{'TOTAL':<8} | {total_tasks:<6} | {total_points:<7} | {total_must_points:<9} | "
        f"{sum(t.points for t in tasks if t.priority == 'should'):<7} | "
        f"{sum(t.points for t in tasks if t.priority == 'could'):<6} | "
        f"{total_person_days:<12.1f} | 14-Week Total\n\n"
    )

    area_counts: dict[str, int] = defaultdict(int)
    for t in tasks:
        for lbl in t.labels:
            area_counts[lbl] += 1

    sys.stdout.write("Technical Area Distribution:\n")
    for area, cnt in sorted(area_counts.items(), key=lambda x: x[1], reverse=True):
        sys.stdout.write(f"  area:{area:<12} : {cnt} tasks\n")

    must_cnt = sum(1 for t in tasks if t.priority == "must")
    should_cnt = sum(1 for t in tasks if t.priority == "should")
    could_cnt = sum(1 for t in tasks if t.priority == "could")
    sys.stdout.write(
        f"\nPriority Distribution: Must={must_cnt}, Should={should_cnt}, Could={could_cnt}\n"
    )
    sys.stdout.write(
        "Planning Guideline: Capacity scales with available team size and sprint duration.\n"
        "Reserve: 20-25% buffer recommended for peer review, integration testing,\n"
        "and unforeseen complexity.\n"
        f"Baseline Reference Capacity: ~{capacity_days:.1f} person-days per sprint.\n\n"
    )


def load_state() -> dict[str, int]:
    """Load local task ID to GitHub issue number mapping."""
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return {k: int(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        return {}
    return {}


def save_state(state: dict[str, int]) -> None:
    """Save task ID to issue number mapping locally."""
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def format_issue_body(task: Task) -> str:
    """Format markdown description for GitHub Issue."""
    criteria_lines = "\n".join(f"- [ ] {c}" for c in task.acceptance_criteria)
    deps_text = ", ".join(task.dependencies) if task.dependencies else "None"

    return f"""### Outcome
{task.outcome}

### Acceptance Criteria
{criteria_lines}

### Technical Details
- **Task ID:** `{task.id}`
- **Epic:** {task.epic}
- **Sprint:** Sprint {task.sprint}
- **Priority:** `{task.priority}`
- **Size:** `{task.size}` ({task.points} points, ~{task.person_days} person-days)
- **Dependencies:** {deps_text}

### Definition of Done
{task.definition_of_done}
"""


def import_tasks(
    tasks: list[Task],
    repo: str,
    project_number: int | None = None,
    dry_run: bool = False,
) -> int:
    """Import tasks into GitHub Issues and optionally link to Project board."""
    sys.stdout.write(f"==> Importing {len(tasks)} tasks into {repo}...\n")
    if dry_run:
        sys.stdout.write("MODE: DRY RUN (no GitHub Issues will be created)\n\n")

    state = load_state()
    owner = repo.split("/")[0]

    for t in tasks:
        if t.id in state:
            sys.stdout.write(f"  [EXISTS] {t.id} -> Issue #{state[t.id]}. Skipping.\n")
            continue

        title = f"[{t.id}] {t.title}"
        body = format_issue_body(t)
        milestone = f"Sprint {t.sprint}"
        labels = [f"type:{'feat' if t.priority == 'must' else 'chore'}", f"tier:{t.priority}"]
        for lbl in t.labels:
            labels.append(f"area:{lbl}")

        if dry_run:
            sys.stdout.write(f"  [DRY-RUN] Would create issue: {title} (Milestone: {milestone})\n")
            continue

        cmd = [
            "gh",
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            "--body",
            body,
            "--milestone",
            milestone,
        ]
        for lbl in labels:
            cmd.extend(["--label", lbl])

        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            sys.stderr.write(f"  [FAIL] Failed to create issue for {t.id}: {res.stderr.strip()}\n")
            save_state(state)
            return 1

        issue_url = res.stdout.strip()
        match = re.search(r"/issues/(\d+)$", issue_url)
        issue_num = int(match.group(1)) if match else 0
        state[t.id] = issue_num
        sys.stdout.write(f"  [CREATED] {t.id} -> Issue #{issue_num} ({issue_url})\n")

        if project_number and issue_url:
            p_cmd = [
                "gh",
                "project",
                "item-add",
                str(project_number),
                "--owner",
                owner,
                "--url",
                issue_url,
            ]
            subprocess.run(p_cmd, capture_output=True, check=False)

    if not dry_run:
        save_state(state)
        sys.stdout.write(f"\nSaved state to {STATE_FILE}. Import complete!\n")

    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser for backlog CLI."""
    parser = argparse.ArgumentParser(description="PBIP Backlog Validator & GitHub Importer")
    parser.add_argument("repo", nargs="?", help="Target GitHub repository (OWNER/REPO)")
    parser.add_argument(
        "--validate", action="store_true", help="Validate backlog.toml schema and dependencies"
    )
    parser.add_argument(
        "--stats", action="store_true", help="Display sprint workload and capacity report"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview GitHub Issue creation without executing"
    )
    parser.add_argument(
        "--project-number",
        type=int,
        default=None,
        help="Optional GitHub Project (v2) number to add issues to",
    )
    parser.add_argument(
        "--capacity-days",
        type=float,
        default=BUFFER_CAPACITY_DAYS,
        help=f"Configurable sprint person-day capacity threshold (default: {BUFFER_CAPACITY_DAYS})",
    )
    parser.add_argument("--file", type=Path, default=BACKLOG_FILE, help="Path to backlog.toml")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    tasks = load_backlog(args.file)
    valid, errors = validate_backlog(tasks)

    if not valid:
        sys.stderr.write(f"Validation FAILED ({len(errors)} issues found):\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        return 1

    if args.validate or args.stats or not args.repo:
        sys.stdout.write(f"[VALID] Successfully validated {len(tasks)} tasks in {args.file}.\n")
        print_backlog_statistics(tasks, capacity_days=args.capacity_days)
        if not args.repo:
            return 0

    if args.repo:
        if not shutil.which("gh") and not args.dry_run:
            sys.stderr.write("Error: GitHub CLI ('gh') is required to import issues.\n")
            return 1
        return import_tasks(tasks, args.repo, args.project_number, args.dry_run)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
