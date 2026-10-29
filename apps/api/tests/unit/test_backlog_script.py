"""Unit tests for PBIP backlog script CLI options and project number handling.

Verifies:
- Argument parsing supports both --project-number and --capacity-days.
- Default project-number is None and capacity-days has the expected default.
- Backlog importer receives the project_number argument correctly.
- gh project item-add is invoked when project_number is supplied.
"""

from unittest.mock import MagicMock, patch

import pytest

from scripts.backlog import (
    BACKLOG_FILE,
    Task,
    _create_single_issue,
    build_parser,
    filter_tasks_by_sprint,
    format_issue_body,
    import_tasks,
    load_backlog,
    load_state,
    save_state,
    validate_backlog,
)

pytestmark = pytest.mark.unit


def _create_sample_task(
    task_id: str = "TASK-001",
    title: str = "Test Task",
    epic: str = "Platform",
    sprint: int = 1,
    priority: str = "must",
    size: str = "S",
    labels: list[str] | None = None,
    outcome: str = "Test outcome",
    acceptance_criteria: list[str] | None = None,
    dependencies: list[str] | None = None,
    definition_of_done: str = "Done when tested",
) -> Task:
    return Task(
        id=task_id,
        title=title,
        epic=epic,
        sprint=sprint,
        priority=priority,
        size=size,
        labels=labels or ["infra"],
        outcome=outcome,
        acceptance_criteria=acceptance_criteria or ["AC 1", "AC 2"],
        dependencies=dependencies or [],
        definition_of_done=definition_of_done,
    )


def test_build_parser_options_and_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.project_number is None
    assert args.sprint is None
    assert args.capacity_days == 15.0
    assert args.repo is None


def test_build_parser_with_project_number_and_capacity_days() -> None:
    parser = build_parser()
    args = parser.parse_args(["myorg/pbip", "--project-number", "42", "--capacity-days", "20.5"])
    assert args.repo == "myorg/pbip"
    assert args.project_number == 42
    assert args.capacity_days == 20.5


def test_build_parser_with_sprint_option() -> None:
    parser = build_parser()
    args = parser.parse_args(["rachitst/pbip", "--sprint", "1"])
    assert args.sprint == 1

    args_sprint_7 = parser.parse_args(["rachitst/pbip", "--sprint", "7"])
    assert args_sprint_7.sprint == 7


def test_build_parser_rejects_invalid_sprint() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["rachitst/pbip", "--sprint", "0"])

    with pytest.raises(SystemExit):
        parser.parse_args(["rachitst/pbip", "--sprint", "8"])

    with pytest.raises(SystemExit):
        parser.parse_args(["rachitst/pbip", "--sprint", "foo"])


def test_filter_tasks_by_sprint() -> None:
    t1 = _create_sample_task(task_id="TASK-001", sprint=1)
    t2 = _create_sample_task(task_id="TASK-002", sprint=1)
    t3 = _create_sample_task(task_id="TASK-014", sprint=2)
    t4 = _create_sample_task(task_id="TASK-029", sprint=3)
    tasks = [t1, t2, t3, t4]

    # None returns all tasks
    assert filter_tasks_by_sprint(tasks, None) == tasks

    # Specific sprint returns only matching tasks
    sprint_1_tasks = filter_tasks_by_sprint(tasks, 1)
    assert sprint_1_tasks == [t1, t2]

    sprint_2_tasks = filter_tasks_by_sprint(tasks, 2)
    assert sprint_2_tasks == [t3]

    # Non-existent sprint returns empty list
    assert filter_tasks_by_sprint(tasks, 4) == []


def test_main_passes_project_number_to_import_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.backlog as backlog_mod

    mock_import = MagicMock(return_value=0)
    monkeypatch.setattr(backlog_mod, "import_tasks", mock_import)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(
        "sys.argv",
        ["backlog.py", "myorg/pbip", "--project-number", "10", "--dry-run"],
    )

    exit_code = backlog_mod.main()
    assert exit_code == 0
    assert mock_import.call_count == 1
    call_args, _ = mock_import.call_args
    assert call_args[1] == "myorg/pbip"
    assert call_args[2] == 10
    assert call_args[3] is True


def test_main_passes_sprint_filtered_tasks_to_import_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.backlog as backlog_mod

    mock_import = MagicMock(return_value=0)
    monkeypatch.setattr(backlog_mod, "import_tasks", mock_import)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(
        "sys.argv",
        ["backlog.py", "rachitst/pbip", "--sprint", "1", "--project-number", "1", "--dry-run"],
    )

    exit_code = backlog_mod.main()
    assert exit_code == 0
    assert mock_import.call_count == 1
    call_args, kwargs = mock_import.call_args

    filtered_tasks: list[Task] = call_args[0]
    assert len(filtered_tasks) == 13
    assert all(t.sprint == 1 for t in filtered_tasks)
    assert call_args[1] == "rachitst/pbip"
    assert call_args[2] == 1
    assert call_args[3] is True
    assert kwargs.get("sprint") == 1


def test_import_tasks_invokes_gh_project_item_add() -> None:
    task = _create_sample_task(task_id="TASK-999", sprint=1)

    with (
        patch("scripts.backlog.load_state", return_value={}),
        patch("scripts.backlog.save_state"),
        patch("subprocess.run") as mock_subproc,
    ):
        mock_res_create = MagicMock()
        mock_res_create.returncode = 0
        mock_res_create.stdout = "https://github.com/myorg/pbip/issues/101\n"
        mock_res_create.stderr = ""

        mock_res_proj = MagicMock()
        mock_res_proj.returncode = 0
        mock_res_proj.stdout = ""
        mock_res_proj.stderr = ""

        mock_subproc.side_effect = [mock_res_create, mock_res_proj]

        code = import_tasks([task], repo="myorg/pbip", project_number=7, dry_run=False)
        assert code == 0

        assert mock_subproc.call_count == 2
        proj_call_args = mock_subproc.call_args_list[1][0][0]
        assert proj_call_args == [
            "gh",
            "project",
            "item-add",
            "7",
            "--owner",
            "myorg",
            "--url",
            "https://github.com/myorg/pbip/issues/101",
        ]


def test_import_tasks_dry_run_prevents_github_mutations(capsys: pytest.CaptureFixture[str]) -> None:
    t1 = _create_sample_task(task_id="TASK-001", sprint=1)
    t2 = _create_sample_task(task_id="TASK-002", sprint=1)

    with (
        patch("scripts.backlog.load_state", return_value={}) as mock_load,
        patch("scripts.backlog.save_state") as mock_save,
        patch("subprocess.run") as mock_subproc,
    ):
        code = import_tasks(
            [t1, t2], repo="rachitst/pbip", project_number=1, dry_run=True, sprint=1
        )
        assert code == 0
        assert mock_load.call_count == 1
        assert mock_save.call_count == 0
        assert mock_subproc.call_count == 0

        out = capsys.readouterr().out
        assert "MODE: DRY RUN" in out
        assert "==> Importing 2 tasks (Sprint 1) into rachitst/pbip..." in out
        assert (
            "[DRY-RUN] Would create issue: [TASK-001] Test Task (Milestone: Sprint 1) (Project #1)"
            in out
        )
        assert (
            "[DRY-RUN] Would create issue: [TASK-002] Test Task (Milestone: Sprint 1) (Project #1)"
            in out
        )


def test_import_tasks_prevents_accidental_import_of_other_sprints(
    capsys: pytest.CaptureFixture[str],
) -> None:
    t_sprint1 = _create_sample_task(task_id="TASK-001", sprint=1)
    t_sprint2 = _create_sample_task(task_id="TASK-014", sprint=2)
    t_sprint3 = _create_sample_task(task_id="TASK-029", sprint=3)

    # Calling import_tasks with sprint=1 even when supplied with tasks from other sprints
    with (
        patch("scripts.backlog.load_state", return_value={}),
        patch("scripts.backlog.save_state"),
        patch("subprocess.run") as mock_subproc,
    ):
        mock_res_create = MagicMock(
            returncode=0, stdout="https://github.com/rachitst/pbip/issues/1", stderr=""
        )
        mock_subproc.return_value = mock_res_create

        code = import_tasks(
            [t_sprint1, t_sprint2, t_sprint3],
            repo="rachitst/pbip",
            project_number=None,
            dry_run=False,
            sprint=1,
        )
        assert code == 0
        # Exactly one issue created (for TASK-001), TASK-014 and TASK-029 are excluded
        assert mock_subproc.call_count == 1
        create_call = mock_subproc.call_args[0][0]
        title_idx = create_call.index("--title") + 1
        assert "[TASK-001]" in create_call[title_idx]
        assert "--milestone" in create_call
        milestone_idx = create_call.index("--milestone") + 1
        assert create_call[milestone_idx] == "Sprint 1"

        out = capsys.readouterr().out
        assert "TASK-001" in out
        assert "TASK-014" not in out
        assert "TASK-029" not in out


def test_validate_backlog_full_repo_and_preserves_dependency_validation() -> None:
    # Full backlog validation on the canonical 107 tasks (93 planned + 14 requirement-gap tasks)
    tasks = load_backlog(BACKLOG_FILE)
    assert len(tasks) == 107
    valid, errors = validate_backlog(tasks)
    assert valid is True
    assert errors == []

    # Dependency validation detects non-existent dependency in any task
    broken_tasks = list(tasks)
    broken_task = _create_sample_task(
        task_id="TASK-998",
        sprint=3,
        dependencies=["TASK-NONEXISTENT"],
    )
    broken_tasks.append(broken_task)
    valid_broken, errors_broken = validate_backlog(broken_tasks)
    assert valid_broken is False
    assert any("depends on non-existent task" in err for err in errors_broken)

    # Cycle detection
    c1 = _create_sample_task(task_id="TASK-901", sprint=2, dependencies=["TASK-902"])
    c2 = _create_sample_task(task_id="TASK-902", sprint=2, dependencies=["TASK-901"])
    valid_cycle, errors_cycle = validate_backlog([c1, c2])
    assert valid_cycle is False
    assert any("Circular dependency detected" in err for err in errors_cycle)


def test_main_aborts_on_invalid_backlog_before_sprint_filtering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    import scripts.backlog as backlog_mod

    # Create temporary invalid backlog TOML
    invalid_toml = tmp_path / "invalid_backlog.toml"  # type: ignore[operator]
    invalid_toml.write_text(
        """
[[tasks]]
id = "TASK-001"
title = "Valid Sprint 1 Task"
epic = "Platform"
sprint = 1
priority = "must"
size = "S"
labels = ["infra"]
outcome = "Outcome"
acceptance_criteria = ["Criterion 1"]
dependencies = []
definition_of_done = "DoD"

[[tasks]]
id = "TASK-050"
title = "Sprint 3 Task With Bad Dep"
epic = "Platform"
sprint = 3
priority = "must"
size = "S"
labels = ["infra"]
outcome = "Outcome"
acceptance_criteria = ["Criterion 1"]
dependencies = ["TASK-MISSING-DEP"]
definition_of_done = "DoD"
""",
        encoding="utf-8",
    )

    mock_import = MagicMock(return_value=0)
    monkeypatch.setattr(backlog_mod, "import_tasks", mock_import)
    monkeypatch.setattr(
        "sys.argv",
        [
            "backlog.py",
            "rachitst/pbip",
            "--file",
            str(invalid_toml),
            "--sprint",
            "1",
            "--dry-run",
        ],
    )

    exit_code = backlog_mod.main()
    assert exit_code == 1
    # Full backlog validation failed, so import_tasks was NEVER reached
    assert mock_import.call_count == 0


def test_format_issue_body_and_metadata_preservation() -> None:
    task = _create_sample_task(
        task_id="TASK-001",
        title="Cross-Platform Developer Command Runner",
        epic="Platform & DevOps",
        sprint=1,
        priority="must",
        size="M",
        labels=["infra"],
        outcome="A single, cross-platform CLI runner that works identically on Ubuntu/Linux and Windows.",
        acceptance_criteria=[
            "scripts/dev.py implements setup, doctor, infra-up, test, check",
            "Clean command purges cache directories cross-platform",
        ],
        dependencies=["TASK-000"],
        definition_of_done="Strictly typed, ruff clean, tested on Linux and Windows.",
    )

    body = format_issue_body(task)
    assert "### Outcome\n" in body
    assert task.outcome in body
    assert "### Acceptance Criteria\n" in body
    assert "- [ ] scripts/dev.py implements setup, doctor, infra-up, test, check" in body
    assert "- [ ] Clean command purges cache directories cross-platform" in body
    assert "- **Task ID:** `TASK-001`" in body
    assert "- **Epic:** Platform & DevOps" in body
    assert "- **Sprint:** Sprint 1" in body
    assert "- **Priority:** `must`" in body
    assert "- **Size:** `M` (2 points, ~1.5 person-days)" in body
    assert "- **Dependencies:** TASK-000" in body
    assert "### Definition of Done\n" in body
    assert task.definition_of_done in body


def test_load_state_malformed_json_recovery(tmp_path: pytest.TempPathFactory) -> None:
    # 1. Normal valid JSON
    state_file = tmp_path / "valid_state.json"  # type: ignore[operator]
    state_file.write_text('{"TASK-001": 12, "TASK-002": 13, "TASK-003": 14}', encoding="utf-8")
    loaded = load_state(state_file)
    assert loaded == {"TASK-001": 12, "TASK-002": 13, "TASK-003": 14}

    # 2. Corrupted / truncated JSON (missing closing brace and trailing comma)
    corrupt_file = tmp_path / "corrupt_state.json"  # type: ignore[operator]
    corrupt_file.write_text(
        '{\n  "TASK-001": 12,\n  "TASK-002": 13,\n  "TASK-003": 14', encoding="utf-8"
    )
    recovered = load_state(corrupt_file)
    assert recovered == {"TASK-001": 12, "TASK-002": 13, "TASK-003": 14}

    # 3. JSON containing invalid entries (non-numeric, negative, invalid IDs)
    dirty_file = tmp_path / "dirty_state.json"  # type: ignore[operator]
    dirty_file.write_text(
        '{"TASK-001": 12, "TASK-002": "invalid", "TASK-003": -1, "NOT-A-TASK": 99}',
        encoding="utf-8",
    )
    cleaned = load_state(dirty_file)
    assert cleaned == {"TASK-001": 12}

    # 4. Non-existent file
    assert load_state(tmp_path / "nonexistent.json") == {}  # type: ignore[operator]


def test_save_state_atomic(tmp_path: pytest.TempPathFactory) -> None:
    state_file = tmp_path / "test_state.json"  # type: ignore[operator]
    data = {"TASK-001": 12, "TASK-002": 13, "TASK-003": 14}
    save_state(data, state_file)

    assert state_file.exists()
    assert not state_file.with_suffix(".json.tmp").exists()
    loaded = load_state(state_file)
    assert loaded == data


def test_safe_resume_after_partial_import(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify importer skips already imported tasks and only creates remaining ones."""
    initial_state = {"TASK-001": 12, "TASK-002": 13, "TASK-003": 14}
    t1 = _create_sample_task(task_id="TASK-001", sprint=1)
    t2 = _create_sample_task(task_id="TASK-002", sprint=1)
    t3 = _create_sample_task(task_id="TASK-003", sprint=1)
    t4 = _create_sample_task(task_id="TASK-004", sprint=1)

    with (
        patch("scripts.backlog.load_state", return_value=dict(initial_state)),
        patch("scripts.backlog.save_state") as mock_save,
        patch("subprocess.run") as mock_subproc,
    ):
        mock_res_create = MagicMock(
            returncode=0, stdout="https://github.com/rachitst/pbip/issues/15", stderr=""
        )
        mock_res_proj = MagicMock(returncode=0, stdout="", stderr="")
        mock_subproc.side_effect = [mock_res_create, mock_res_proj]

        code = import_tasks(
            [t1, t2, t3, t4],
            repo="rachitst/pbip",
            project_number=1,
            dry_run=False,
            sprint=1,
        )
        assert code == 0

        # Tasks 1, 2, 3 were skipped; only Task 4 triggered issue creation and project add
        assert mock_subproc.call_count == 2
        create_args = mock_subproc.call_args_list[0][0][0]
        assert "[TASK-004]" in create_args[create_args.index("--title") + 1]

        out = capsys.readouterr().out
        assert "[EXISTS] TASK-001 -> Issue #12. Skipping." in out
        assert "[EXISTS] TASK-002 -> Issue #13. Skipping." in out
        assert "[EXISTS] TASK-003 -> Issue #14. Skipping." in out
        assert "[CREATED] TASK-004 -> Issue #15" in out

        # State was updated with TASK-004
        assert mock_save.call_count >= 1
        saved_state = mock_save.call_args[0][0]
        assert saved_state["TASK-004"] == 15
        assert saved_state["TASK-001"] == 12


def test_create_single_issue_self_heals_missing_labels(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify _create_single_issue retries and auto-creates missing labels if gh fails on label."""
    task = _create_sample_task(task_id="TASK-004", labels=["api", "security"])
    state: dict[str, int] = {}

    with (
        patch("scripts.backlog.save_state"),
        patch("subprocess.run") as mock_subproc,
    ):
        # 1. First gh issue create fails due to missing area:security
        mock_fail_label = MagicMock(
            returncode=1,
            stdout="",
            stderr="GraphQL: Could not resolve to a Label with the name 'area:security'",
        )
        # 2. _auto_create_missing_labels creates labels
        mock_label_create = MagicMock(returncode=0, stdout="", stderr="")
        # 3. Retry gh issue create succeeds
        mock_retry_success = MagicMock(
            returncode=0,
            stdout="https://github.com/rachitst/pbip/issues/15",
            stderr="",
        )

        mock_subproc.side_effect = [
            mock_fail_label,
            mock_label_create,
            mock_label_create,
            mock_label_create,
            mock_label_create,
            mock_retry_success,
        ]

        success = _create_single_issue(
            task, repo="rachitst/pbip", project_number=None, owner="rachitst", state=state
        )
        assert success is True
        assert state["TASK-004"] == 15

        out = capsys.readouterr().out
        assert "[LABEL-FIX] Ensuring labels on rachitst/pbip and retrying TASK-004..." in out
        assert "[CREATED] TASK-004 -> Issue #15" in out
