#!/bin/bash
# Configure branch protection on the target branch.
#
# Locks in the PR-only, CI-gated, squash-merge workflow we've been
# following by convention. Idempotent — re-run to surface drift or
# reapply after an accidental settings change.
#
# Settings applied:
#   - Required status checks: "unit-tests" (strict: branch must be up to
#     date with the base before merge). Context name is the job-id from
#     .github/workflows/test.yml, NOT the "<workflow> / <job>" form GitHub
#     sometimes shows in the UI — the GraphQL CheckRun "name" is the
#     authoritative match.
#   - PR required (no direct pushes); zero reviews required (solo repo —
#     GitHub prohibits self-approval, so requiring reviews would block
#     self-merges).
#   - Linear history (squash- or rebase-merge only; no merge commits).
#   - enforce_admins: even repo owners cannot bypass.
#   - Force-pushes and deletions disallowed.
#
# Usage:
#   ./scripts/configure_branch_protection.sh                   # uses current repo, main
#   ./scripts/configure_branch_protection.sh OWNER/REPO        # custom repo, main
#   ./scripts/configure_branch_protection.sh OWNER/REPO BRANCH # custom branch
#
# Requires `gh auth login` with admin access on the target repo.
set -euo pipefail

REPO="${1:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
BRANCH="${2:-main}"

echo "Configuring branch protection on $REPO @ $BRANCH..."

# The PUT payload. Note: `gh api` with -F flags would be JSON-typed, but
# nested objects (required_status_checks) are simpler to pass via --input.
PAYLOAD=$(cat <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["unit-tests"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": false
}
JSON
)

echo "$PAYLOAD" | gh api \
    -X PUT \
    -H "Accept: application/vnd.github+json" \
    "repos/$REPO/branches/$BRANCH/protection" \
    --input - \
    >/dev/null

echo "Applied. Current state:"
gh api "repos/$REPO/branches/$BRANCH/protection" --jq '{
  required_status_checks: {
    contexts: .required_status_checks.contexts,
    strict: .required_status_checks.strict
  },
  enforce_admins: .enforce_admins.enabled,
  required_pull_request_reviews: .required_pull_request_reviews,
  required_linear_history: .required_linear_history.enabled,
  allow_force_pushes: .allow_force_pushes.enabled,
  allow_deletions: .allow_deletions.enabled,
  required_conversation_resolution: .required_conversation_resolution.enabled
}'
