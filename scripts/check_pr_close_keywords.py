#!/usr/bin/env python3
"""Parse a PR title + body and report `(#N)` references in the title that
lack a corresponding GitHub close keyword (`closes`, `fixes`, `resolves`,
case-insensitive, all tenses).

Output:
  - If any references are unmatched: print a Markdown comment-body to
    stdout that the GitHub Action can post on the PR. Exit 0.
  - Otherwise: no output. Exit 0.

Exit code is always 0; the workflow uses presence-of-stdout as the
signal, so this check never fails CI (issue #83 — non-blocking by
design).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# All GitHub close-keyword tenses, case-insensitive. See:
# https://docs.github.com/en/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue
CLOSE_KEYWORD_PATTERN = re.compile(
    r"(?i)\b(close[ds]?|fix(?:e[ds])?|resolve[ds]?)\s+#(\d+)"
)

# Issue references in the title we want to check, e.g. `feat: foo (#35)`.
TITLE_REF_PATTERN = re.compile(r"\(#(\d+)\)")

MARKER = "<!-- pr-close-keyword-check -->"


def find_unmatched_refs(title: str, body: str) -> list[str]:
    """Return the sorted list of issue numbers referenced in the title
    via `(#N)` that don't have a close keyword in title OR body."""
    title_refs = set(TITLE_REF_PATTERN.findall(title))
    if not title_refs:
        return []

    combined = f"{title}\n{body}"
    closed = {n for _kw, n in CLOSE_KEYWORD_PATTERN.findall(combined)}

    unmatched = title_refs - closed
    return sorted(unmatched, key=int)


def build_comment(unmatched: list[str]) -> str:
    """Render the PR comment body for the given unmatched refs."""
    if len(unmatched) == 1:
        n = unmatched[0]
        ref_phrase = f"issue **#{n}**"
        fix_phrase = f"add `closes #{n}` to the PR title or body"
    else:
        ref_phrase = "issues " + ", ".join(f"**#{n}**" for n in unmatched)
        fix_phrase = (
            "add a close keyword (e.g. `closes #N`) for each in the PR "
            "title or body"
        )

    return (
        f"> [!NOTE]\n"
        f"> This PR's title references {ref_phrase} but no close keyword "
        f"(`closes`, `fixes`, `resolves`) was found in the title or body. "
        f"GitHub will not auto-close on merge.\n"
        f">\n"
        f"> To auto-close: {fix_phrase}. To leave open: ignore this comment.\n"
        f">\n"
        f"> {MARKER}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--title",
        default=os.environ.get("PR_TITLE", ""),
        help="PR title (defaults to $PR_TITLE)",
    )
    parser.add_argument(
        "--body",
        default=os.environ.get("PR_BODY", ""),
        help="PR body (defaults to $PR_BODY)",
    )
    args = parser.parse_args()

    unmatched = find_unmatched_refs(args.title, args.body or "")
    if unmatched:
        sys.stdout.write(build_comment(unmatched))
    return 0


if __name__ == "__main__":
    sys.exit(main())
