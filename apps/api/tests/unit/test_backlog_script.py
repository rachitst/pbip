"""Unit tests for PBIP backlog script CLI options and project number handling.

Verifies:
- Argument parsing supports both --project-number and --capacity-days.
- Default project-number is None and capacity-days has the expected default.
- Backlog importer receives the project_number argument correctly.
- gh project item-add is invoked when project_number is supplied.
"""

from unittest.mock import MagicMock, patch

import pytest

from scripts.backlog import Task, build_parser, import_tasks

pytestmark = pytest.mark.unit


def test_build_parser_options_and_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.project_number is None
    assert args.capacity_days == 15.0
    assert args.repo is None


def test_build_parser_with_project_number_and_capacity_days() -> None:
    parser = build_parser()
    args = parser.parse_args(["myorg/pbip", "--project-number", "42", "--capacity-days", "20.5"])
    assert args.repo == "myorg/pbip"
    assert args.project_number == 42
    assert args.capacity_days == 20.5


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


def test_import_tasks_invokes_gh_project_item_add() -> None:
    task = Task(
        id="TASK-999",
        title="Test Task",
        epic="Test Epic",
        sprint=1,
        priority="must",
        size="S",
        labels=["test"],
        outcome="Test outcome",
        acceptance_criteria=["AC 1"],
        dependencies=[],
        definition_of_done="DoD test",
    )

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
