#!/usr/bin/env python3
"""Cross-platform GitHub repository setup script for PBIP using the GitHub CLI (gh).

Configures:
1. Squash-only merges and branch deletion on merge.
2. Standard PBIP labels with colors and descriptions.
3. 7 two-week sprint milestones starting from a given date.
4. Collaborator invites with write access.
5. Branch ruleset for 'main' (from .github/rulesets/main.json).
6. GitHub Project board creation and linking.

Usage:
    python scripts/setup_github.py OWNER/REPO --collaborators USER1 USER2 --start YYYY-MM-DD
    python scripts/setup_github.py OWNER/REPO --dry-run
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LABELS: list[tuple[str, str, str]] = [
    ("type:feat", "1D76DB", "New capability"),
    ("type:bug", "D73A4A", "Something is broken"),
    ("type:chore", "CFD3D7", "Maintenance / tooling"),
    ("type:docs", "0075CA", "Documentation"),
    ("type:security", "B60205", "Security-relevant"),
    ("type:test", "0E8A16", "Tests only"),
    ("area:api", "5319E7", "Backend"),
    ("area:web", "FBCA04", "Frontend"),
    ("area:ml", "0E8A16", "Data / ML"),
    ("area:infra", "006B75", "CI / Docker / ops"),
    ("tier:must", "B60205", "Must have"),
    ("tier:should", "D93F0B", "Should have"),
    ("tier:could", "FEF2C0", "Could have"),
    ("blocked", "000000", "Waiting on something"),
]


def run_gh(
    args: list[str],
    check: bool = True,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Execute a GitHub CLI command."""
    cmd = ["gh", *args]
    if dry_run:
        sys.stdout.write(f"[dry-run] would run: {' '.join(cmd)}\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        sys.stderr.write(f"Command failed ({result.returncode}): {' '.join(cmd)}\n")
        if result.stderr:
            sys.stderr.write(f"Error: {result.stderr.strip()}\n")
    return result


def check_prerequisites() -> bool:
    """Verify gh CLI is installed and authenticated."""
    if not shutil.which("gh"):
        sys.stderr.write(
            "Error: GitHub CLI ('gh') is not installed or not in PATH.\n"
            "Installation instructions:\n"
            "  Ubuntu/Debian: sudo apt install gh\n"
            "  Windows:       winget install --id GitHub.cli\n"
            "  macOS:         brew install gh\n"
        )
        return False

    auth_check = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True, check=False
    )
    if auth_check.returncode != 0:
        sys.stderr.write(
            "Error: gh is not authenticated. Please run:\n"
            "  gh auth login\n"
            "Ensure you grant permissions for 'repo', 'project', and 'workflow'.\n"
        )
        return False
    return True


def configure_merge_settings(repo: str, dry_run: bool) -> None:
    """Configure repository merge settings: squash only, delete branch on merge."""
    sys.stdout.write(f"==> Configuring merge settings on {repo}...\n")
    args = [
        "repo",
        "edit",
        repo,
        "--enable-squash-merge",
        "--enable-merge-commit=false",
        "--enable-rebase-merge=false",
        "--delete-branch-on-merge",
        "--enable-auto-merge",
    ]
    res = run_gh(args, check=False, dry_run=dry_run)
    if not dry_run and res.returncode == 0:
        sys.stdout.write("    Successfully updated merge settings.\n")
    elif not dry_run:
        sys.stdout.write("    Notice: could not update all repo settings (check admin rights).\n")


def setup_labels(repo: str, dry_run: bool) -> None:
    """Ensure all required PBIP labels exist."""
    sys.stdout.write(f"==> Configuring {len(LABELS)} labels...\n")
    for name, color, desc in LABELS:
        args = [
            "label",
            "create",
            name,
            "--repo",
            repo,
            "--color",
            color,
            "--description",
            desc,
            "--force",
        ]
        res = run_gh(args, check=False, dry_run=dry_run)
        if not dry_run and res.returncode == 0:
            sys.stdout.write(f"    Label created/updated: {name}\n")


def setup_milestones(repo: str, start_date_str: str, dry_run: bool) -> None:
    """Create 7 two-week sprint milestones."""
    start = datetime.date.fromisoformat(start_date_str)
    sys.stdout.write(f"==> Setting up 7 sprint milestones starting from {start}...\n")

    existing_milestones: set[str] = set()
    if not dry_run:
        res = run_gh(["api", f"repos/{repo}/milestones", "--paginate"], check=False)
        if res.returncode == 0 and res.stdout.strip():
            try:
                data = json.loads(res.stdout)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "title" in item:
                            existing_milestones.add(str(item["title"]))
            except json.JSONDecodeError:
                pass

    for n in range(1, 8):
        title = f"Sprint {n}"
        if title in existing_milestones:
            sys.stdout.write(f"    {title} already exists. Skipping.\n")
            continue
        due = start + datetime.timedelta(weeks=2 * n) - datetime.timedelta(days=1)
        due_iso = f"{due.isoformat()}T23:59:59Z"
        args = [
            "api",
            f"repos/{repo}/milestones",
            "-f",
            f"title={title}",
            "-f",
            f"due_on={due_iso}",
            "-f",
            f"description=Sprint {n} deliverable window ending {due.isoformat()}",
        ]
        res = run_gh(args, check=False, dry_run=dry_run)
        if not dry_run and res.returncode == 0:
            sys.stdout.write(f"    Created {title} (due {due.isoformat()})\n")


def invite_collaborator(repo: str, username: str, dry_run: bool) -> None:
    """Invite collaborator with push permissions."""
    sys.stdout.write(f"==> Inviting collaborator @{username} to {repo}...\n")
    args = [
        "api",
        "-X",
        "PUT",
        f"repos/{repo}/collaborators/{username}",
        "-f",
        "permission=push",
    ]
    res = run_gh(args, check=False, dry_run=dry_run)
    if not dry_run and res.returncode == 0:
        sys.stdout.write(f"    Invitation sent to @{username}.\n")
    elif not dry_run:
        sys.stdout.write(
            f"    Notice: could not invite @{username} automatically. "
            "Add manually via Settings > Collaborators.\n"
        )


def setup_ruleset(repo: str, dry_run: bool) -> None:
    """Import or create the protect-main ruleset."""
    sys.stdout.write("==> Setting up branch ruleset for 'main'...\n")
    ruleset_file = ROOT / ".github" / "rulesets" / "main.json"
    if not ruleset_file.exists():
        sys.stderr.write(f"    Warning: Ruleset file {ruleset_file} not found.\n")
        return

    if not dry_run:
        res = run_gh(["api", f"repos/{repo}/rulesets"], check=False)
        if res.returncode == 0 and res.stdout.strip():
            try:
                data = json.loads(res.stdout)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("name") == "protect-main":
                            sys.stdout.write(
                                "    Ruleset 'protect-main' already exists. Skipping.\n"
                            )
                            return
            except json.JSONDecodeError:
                pass

    args = [
        "api",
        "-X",
        "POST",
        f"repos/{repo}/rulesets",
        "--input",
        str(ruleset_file),
    ]
    res = run_gh(args, check=False, dry_run=dry_run)
    if not dry_run and res.returncode == 0:
        sys.stdout.write("    Successfully created ruleset 'protect-main'.\n")
    elif not dry_run:
        sys.stdout.write(
            "    Notice: Could not automatically create ruleset via API.\n"
            "    Private repos on free plans require GitHub Pro/Team or a public repo.\n"
            "    To configure manually: Settings > Rules > Rulesets > New > Import from JSON.\n"
        )


def setup_project_board(repo: str, title: str, dry_run: bool) -> None:
    """Create or link a GitHub Project board."""
    sys.stdout.write(f"==> Setting up GitHub Project board: '{title}'...\n")
    owner = repo.split("/")[0]

    if dry_run:
        sys.stdout.write(f"[dry-run] would create project '{title}' under owner '{owner}'\n")
        return

    res = run_gh(["project", "create", "--owner", owner, "--title", title], check=False)
    if res.returncode == 0:
        sys.stdout.write(f"    Project board created: {res.stdout.strip()}\n")
        sys.stdout.write(f"    Next: Link project to repository {repo} in GitHub Projects.\n")
    else:
        sys.stdout.write(
            "    Notice: Could not create Project v2 automatically via CLI.\n"
            f"    Please create one manually at https://github.com/{owner}?tab=projects\n"
        )


def build_parser() -> argparse.ArgumentParser:
    """Build command-line argument parser."""
    parser = argparse.ArgumentParser(description="Cross-platform GitHub setup for PBIP")
    parser.add_argument("repo", help="Target repository in OWNER/REPO format")
    parser.add_argument(
        "--collaborators",
        "--collaborator",
        "--teammate",
        nargs="*",
        default=[],
        dest="collaborators",
        help="GitHub username(s) of collaborator(s) to invite with push access",
    )
    parser.add_argument(
        "--start",
        default="2026-10-12",
        help="Sprint 1 start date in YYYY-MM-DD format (default: 2026-10-12)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview GitHub changes without executing real mutations",
    )
    parser.add_argument("--skip-collab", action="store_true", help="Skip collaborator invite")
    parser.add_argument(
        "--skip-settings", action="store_true", help="Skip merge settings configuration"
    )
    parser.add_argument("--skip-labels", action="store_true", help="Skip label creation")
    parser.add_argument("--skip-milestones", action="store_true", help="Skip milestone creation")
    parser.add_argument("--skip-ruleset", action="store_true", help="Skip ruleset creation")
    parser.add_argument("--skip-project", action="store_true", help="Skip project board creation")
    parser.add_argument(
        "--project-title",
        default="PBIP Delivery Board",
        help="Title for the GitHub Project board",
    )
    return parser


def _run_configuration_steps(args: argparse.Namespace) -> bool:
    """Run enabled configuration steps. Returns True on success, False on error."""
    if not args.skip_settings:
        configure_merge_settings(args.repo, args.dry_run)

    if not args.skip_labels:
        setup_labels(args.repo, args.dry_run)

    if not args.skip_milestones:
        try:
            setup_milestones(args.repo, args.start, args.dry_run)
        except ValueError as exc:
            sys.stderr.write(f"Error parsing start date: {exc}\n")
            return False

    if args.collaborators and not args.skip_collab:
        for collab in args.collaborators:
            invite_collaborator(args.repo, collab, args.dry_run)

    if not args.skip_ruleset:
        setup_ruleset(args.repo, args.dry_run)

    if not args.skip_project:
        setup_project_board(args.repo, args.project_title, args.dry_run)

    return True


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    sys.stdout.write(f"PBIP GitHub Repository Setup: {args.repo}\n")
    if args.dry_run:
        sys.stdout.write("MODE: DRY RUN (no modifications will be made to GitHub)\n\n")

    if not args.dry_run and not check_prerequisites():
        return 1

    if not _run_configuration_steps(args):
        return 1

    sys.stdout.write(
        "\nSetup completed successfully!\n"
        "Security reminder: In GitHub, go to Settings > Code security and enable:\n"
        "  - Dependabot alerts and security updates\n"
        "  - Secret scanning (if available on your repository plan)\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
