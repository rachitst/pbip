"""Unit tests for PBIP GitHub repository setup script.

Verifies:
- Label coverage: Every label used in docs/backlog/backlog.toml exists in setup_github.LABELS.
- Existing project board detection and reuse: Preserves Project #1 and avoids duplicates.
- Plain-text fallback parsing for project detection.
- Prerequisite and setup steps execution.
"""

from __future__ import annotations

import json
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

import pytest

from scripts.backlog import BACKLOG_FILE, load_backlog
from scripts.setup_github import (
    LABELS,
    find_existing_project,
    setup_labels,
    setup_project_board,
)

pytestmark = pytest.mark.unit


def test_label_coverage_for_all_backlog_tasks() -> None:
    """Ensure every label referenced in docs/backlog/backlog.toml exists in setup_github.LABELS."""
    tasks = load_backlog(BACKLOG_FILE)
    configured_label_names = {name for name, _color, _desc in LABELS}

    all_required_labels: set[str] = set()
    for t in tasks:
        all_required_labels.add(f"type:{'feat' if t.priority == 'must' else 'chore'}")
        all_required_labels.add(f"tier:{t.priority}")
        for lbl in t.labels:
            all_required_labels.add(f"area:{lbl}")

    missing_labels = all_required_labels - configured_label_names
    assert not missing_labels, (
        f"The following backlog labels are missing from setup_github.LABELS: {missing_labels}"
    )


def test_area_security_label_is_configured() -> None:
    """Specifically verify area:security is properly configured with color and description."""
    label_dict = {name: (color, desc) for name, color, desc in LABELS}
    assert "area:security" in label_dict
    color, desc = label_dict["area:security"]
    assert color == "B60205"
    assert "security" in desc.lower() or "auth" in desc.lower()


def test_find_existing_project_via_json() -> None:
    """Verify JSON project list parsing finds existing project by title (case-insensitive)."""
    fake_json_output = json.dumps(
        {
            "projects": [
                {
                    "number": 1,
                    "title": "PBIP Delivery Board",
                    "id": "PVT_kwDO12345",
                    "url": "https://github.com/users/rachitst/projects/1",
                },
                {
                    "number": 2,
                    "title": "Another Board",
                    "id": "PVT_kwDO67890",
                    "url": "https://github.com/users/rachitst/projects/2",
                },
            ]
        }
    )

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = CompletedProcess(
            args=["gh", "project", "list"], returncode=0, stdout=fake_json_output, stderr=""
        )

        project = find_existing_project("rachitst", "PBIP Delivery Board")
        assert project is not None
        assert project["number"] == 1
        assert project["title"] == "PBIP Delivery Board"
        assert project["id"] == "PVT_kwDO12345"

        # Case-insensitive match
        project_lower = find_existing_project("rachitst", "pbip delivery board")
        assert project_lower is not None
        assert project_lower["number"] == 1

        # Non-existent project
        non_existent = find_existing_project("rachitst", "Non-Existent Board")
        assert non_existent is None


def test_find_existing_project_via_plain_text_fallback() -> None:
    """Verify fallback to plain text list when json output is empty or unavailable."""
    plain_text_output = (
        "1\tPBIP Delivery Board\thttps://github.com/users/rachitst/projects/1\topen\n"
        "2\tInternal Ops\thttps://github.com/users/rachitst/projects/2\topen\n"
    )

    def side_effect(cmd: list[str], **_kwargs: object) -> CompletedProcess[str]:
        if "--format" in cmd:
            return CompletedProcess(cmd, returncode=1, stdout="", stderr="Unknown flag --format")
        return CompletedProcess(cmd, returncode=0, stdout=plain_text_output, stderr="")

    with patch("subprocess.run", side_effect=side_effect):
        project = find_existing_project("rachitst", "PBIP Delivery Board")
        assert project is not None
        assert project["number"] == 1
        assert project["title"] == "PBIP Delivery Board"


def test_setup_project_board_reuses_existing_project(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify setup_project_board reuses existing project and does NOT call gh project create."""
    existing_project = {
        "number": 1,
        "title": "PBIP Delivery Board",
        "id": "PVT_kwDO12345",
        "url": "https://github.com/users/rachitst/projects/1",
    }

    with (
        patch("scripts.setup_github.find_existing_project", return_value=existing_project),
        patch("scripts.setup_github.run_gh") as mock_run_gh,
    ):
        setup_project_board("rachitst/pbip", "PBIP Delivery Board", dry_run=False)

        # gh project create must NOT be called
        assert mock_run_gh.call_count == 0

        out = capsys.readouterr().out
        assert "Found existing project board 'PBIP Delivery Board' (#1" in out
        assert "Reusing existing project" in out


def test_setup_project_board_creates_when_not_found() -> None:
    """Verify setup_project_board calls project create only when project does not exist."""
    with (
        patch("scripts.setup_github.find_existing_project", return_value=None),
        patch("scripts.setup_github.run_gh") as mock_run_gh,
    ):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "https://github.com/users/rachitst/projects/1"
        mock_run_gh.return_value = mock_res

        setup_project_board("rachitst/pbip", "PBIP Delivery Board", dry_run=False)
        assert mock_run_gh.call_count == 1
        cmd = mock_run_gh.call_args[0][0]
        assert cmd == ["project", "create", "--owner", "rachitst", "--title", "PBIP Delivery Board"]


def test_setup_labels_creates_all_configured_labels() -> None:
    """Verify setup_labels calls gh label create for each label in LABELS."""
    with patch("scripts.setup_github.run_gh") as mock_run_gh:
        mock_res = MagicMock(returncode=0)
        mock_run_gh.return_value = mock_res

        setup_labels("rachitst/pbip", dry_run=False)
        assert mock_run_gh.call_count == len(LABELS)

        created_names = [call[0][0][2] for call in mock_run_gh.call_args_list]
        assert "area:security" in created_names
        assert "area:compliance" in created_names
        assert "area:analytics" in created_names
