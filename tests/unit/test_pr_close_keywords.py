"""Tests for scripts/check_pr_close_keywords.py — the non-blocking PR
title close-keyword check (issue #83).

The script's contract:
  - Exit code is always 0 (non-blocking).
  - If `(#N)` references in the title lack any close keyword in title
    or body, stdout contains the Markdown comment-body for the workflow
    to post.
  - Otherwise stdout is empty.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_pr_close_keywords.py"


def _run(title: str, body: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), "--title", title, "--body", body],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


@pytest.mark.parametrize(
    "title, body, description",
    [
        ("feat: foo (closes #35)", "", "title has close keyword"),
        ("feat: foo (#35)", "Closes #35", "body has close keyword (capitalized)"),
        ("feat: foo (closes #35) (fixes #36)", "", "both refs have inline keywords"),
        ("feat: foo (resolves #35)", "", "resolves keyword"),
        ("feat: foo (FIXES #35)", "", "case-insensitive keyword"),
        ("feat: foo (#35)", "fix #35", "single-tense `fix` in body"),
        ("feat: foo (#35)", "Closed #35 yesterday", "past-tense `closed` in body"),
        ("feat: no refs at all", "", "no `(#N)` to flag"),
        ("just a regular commit message", "with no refs", "neither title nor body has refs"),
    ],
)
def test_clean_cases_produce_no_output(
    title: str, body: str, description: str
) -> None:
    result = _run(title, body)
    assert result.returncode == 0, f"{description}: non-zero exit {result.returncode}"
    assert result.stdout == "", (
        f"{description}: expected empty stdout, got:\n{result.stdout}"
    )


def test_unmatched_single_ref_is_flagged() -> None:
    result = _run("feat: foo (#35)", "")
    assert result.returncode == 0
    assert "#35" in result.stdout
    assert "no close keyword" in result.stdout
    assert "<!-- pr-close-keyword-check -->" in result.stdout


def test_unmatched_multi_ref_is_flagged_with_unmatched_only() -> None:
    """When title has (#35) and (#36) and body closes #35, only #36 should
    be flagged."""
    result = _run("feat: foo (#35) (#36)", "closes #35")
    assert result.returncode == 0
    # #36 should appear as unmatched
    assert "**#36**" in result.stdout
    # #35 should NOT be flagged as unmatched
    assert "**#35**" not in result.stdout


def test_multiple_unmatched_refs_listed() -> None:
    result = _run("feat: foo (#35) (#36) (#37)", "")
    assert result.returncode == 0
    for n in ("35", "36", "37"):
        assert f"**#{n}**" in result.stdout, f"missing #{n} in output"


def test_exit_code_always_zero_non_blocking_contract() -> None:
    """All paths (flagged, clean, no-refs) exit 0 — the workflow uses
    presence of stdout as the signal, not the exit code."""
    for title, body in [
        ("feat: foo (#999)", ""),  # flagged
        ("feat: foo (closes #999)", ""),  # clean
        ("feat: nothing", ""),  # no refs
    ]:
        result = _run(title, body)
        assert result.returncode == 0, (
            f"non-zero exit on {title!r}: {result.returncode}"
        )


def test_env_vars_work_as_fallback_for_flags() -> None:
    """The script reads PR_TITLE/PR_BODY from env if --title/--body aren't
    passed — matches the GitHub Actions invocation pattern."""
    result = subprocess.run(
        [str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={"PR_TITLE": "feat: foo (#42)", "PR_BODY": "", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0
    assert "#42" in result.stdout
