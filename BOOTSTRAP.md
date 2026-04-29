# apple-contacts-mcp — Bootstrap

This document is the entry point for the very first Claude Code session in this repository. It instructs a fresh session how to bring this empty repo to a state where it has a working MCP server, a roadmap, a release process, and the same disciplined workflow as its sibling [apple-mail-mcp](/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp).

`MCP_PLAYBOOK.md` lives next to this file and is the **authoritative** reference for project-agnostic best practices. This BOOTSTRAP is orchestration on top — pointing back to the playbook rather than duplicating it. When the two conflict, the playbook wins on workflow; this BOOTSTRAP wins on contacts-specific decisions.

---

## Audience & Prerequisites

**Audience:** A Claude Code session opening this repository for the first time.

**Required on the host machine:**
- macOS (Contacts.app is macOS-only)
- Xcode Command Line Tools (`xcode-select --install`)
- `uv` (Python dependency manager)
- `gh` CLI authenticated against GitHub
- `apple-mail-mcp` checked out at `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/` — source-of-truth for portable artifacts (Makefile, scripts, hooks, skills). Once Step 1 is done this is just a reference, not a runtime dependency.

---

## How to Use This Document

1. Read this document end-to-end before doing anything.
2. Work through the steps in order. **Do not skip Phase 0** — it drives the entire roadmap.
3. Commit after each step (or each meaningful sub-step). Use the branch convention `{type}/issue-{N}-{slug}` from the playbook.
4. After bootstrap is complete, this document becomes a historical record. New work is driven by GitHub issues + milestones, not by this file.

---

## Step 1 — Repo Bootstrap

**Goal:** an empty but structurally-complete repo with CI green on a no-op test pass.

### 1.1 Create the GitHub repo

```bash
cd /Users/Morgan/Development/agent-tools/mcp-servers/apple-contacts-mcp
git init -b main
gh repo create apple-contacts-mcp --public --source=. \
  --description "MCP server for Apple Contacts on macOS"
```

Set branch protection on `main` (required CI checks, no direct pushes) once CI is green.

### 1.2 Scaffold the layout (per `MCP_PLAYBOOK.md §2`)

```
src/apple_contacts_mcp/
├── __init__.py            # __version__ = "0.0.0"
├── server.py              # FastMCP entry point (mcp = FastMCP(...))
├── contacts_connector.py  # ContactsConnector class with __init__ only
├── exceptions.py          # base ContactsError hierarchy
├── security.py            # placeholder (sanitize_input, audit, confirmation stubs)
└── utils.py               # placeholder (escape helpers, parsing)

tests/
├── conftest.py            # fixtures, markers, CLI options
├── unit/
├── integration/
├── e2e/
└── benchmarks/

evals/agent_tool_usability/
docs/{guides,reference,research,plans}/
```

### 1.3 Copy portable artifacts from apple-mail-mcp

Copy verbatim, then perform these string substitutions across all copied files:
- `apple_mail_mcp` → `apple_contacts_mcp`
- `apple-mail-mcp` → `apple-contacts-mcp`
- `mail_connector` → `contacts_connector`
- `MailConnector` → `ContactsConnector`
- `MAIL_TEST_MODE` → `CONTACTS_TEST_MODE`
- `MAIL_TEST_ACCOUNT` → `CONTACTS_TEST_GROUP`

| Source | Destination |
|---|---|
| `apple-mail-mcp/Makefile` | `Makefile` |
| `apple-mail-mcp/pyproject.toml` | `pyproject.toml` (drop `imapclient`; `pyobjc-framework-Contacts` deps added in Step 2) |
| `apple-mail-mcp/.github/` | `.github/` (workflows, ISSUE_TEMPLATE, PULL_REQUEST_TEMPLATE) |
| `apple-mail-mcp/.claude/settings.json` | `.claude/settings.json` (rewrite hardcoded paths) |
| `apple-mail-mcp/.claude/commands/merge-and-status.md` | `.claude/commands/merge-and-status.md` |
| `apple-mail-mcp/.claude/skills/api-design/` | `.claude/skills/api-design/` |
| `apple-mail-mcp/.claude/skills/integration-testing/` | `.claude/skills/integration-testing/` |
| `apple-mail-mcp/.claude/skills/release/` | `.claude/skills/release/` |
| `apple-mail-mcp/scripts/check_complexity.sh` | `scripts/check_complexity.sh` |
| `apple-mail-mcp/scripts/check_client_server_parity.sh` | `scripts/check_client_server_parity.sh` |
| `apple-mail-mcp/scripts/check_version_sync.sh` | `scripts/check_version_sync.sh` |
| `apple-mail-mcp/scripts/check_dependencies.sh` | `scripts/check_dependencies.sh` |
| `apple-mail-mcp/scripts/check_changelog_date.sh` | `scripts/check_changelog_date.sh` |
| `apple-mail-mcp/scripts/create_tag.sh` | `scripts/create_tag.sh` |
| `apple-mail-mcp/scripts/install-git-hooks.sh` | `scripts/install-git-hooks.sh` |
| `apple-mail-mcp/scripts/hooks/{session_start,pre_bash,post_bash}.sh` | `scripts/hooks/` |
| `apple-mail-mcp/scripts/git-hooks/{pre-commit,pre-push,pre-tag}` | `scripts/git-hooks/` |
| `apple-mail-mcp/CONTRIBUTING.md` | `CONTRIBUTING.md` |
| `apple-mail-mcp/SECURITY.md` | `SECURITY.md` |
| `apple-mail-mcp/LICENSE` | `LICENSE` |

Initialize fresh:
- Empty `CHANGELOG.md` (the release skill will manage it)
- `README.md` skeleton (one paragraph + status badges + install + usage placeholders)
- `.claude/CLAUDE.md` initial pointer:

```markdown
# Apple Contacts MCP Server

This repo is in **bootstrap phase**.

- Read `BOOTSTRAP.md` first if this is your first session in this repo.
- `MCP_PLAYBOOK.md` is the authoritative project-agnostic reference.
- This file accrues contacts-specific guidance as the project grows.
```

### 1.4 Skipped at bootstrap

Do **not** copy these — they need post-Phase-0 decisions:

- `check_applescript_safety.sh` — defer until Phase 0 picks the API surface. May become `check_pyobjc_safety.sh`.
- `applescript-mail` skill (mail-specific). The contacts equivalent is written in Step 4.
- `performance-patterns` skill (mail-specific timings). The contacts equivalent is written in Step 4.

### 1.5 Install hooks and verify

```bash
./scripts/install-git-hooks.sh
make check-all   # should pass on the empty skeleton (no-op tests, no AppleScript yet)
git add -A
git commit -m "infra: bootstrap repo skeleton"
git push -u origin main
gh run watch     # verify CI is green
```

---

## Step 2 — Phase 0: API Research (do not skip)

Contacts has multiple plausible API surfaces, each with different capabilities. The roadmap depends on knowing what's actually possible. File this work as `[research] Phase 0 API gap analysis` (label: `research`, `documentation`, `priority:high`) before starting.

### 2.1 Surfaces to investigate

#### 2.1.1 AppleScript SDEF

```bash
sdef /System/Applications/Contacts.app
sdef /System/Applications/Contacts.app | sdp -fh --basename Contacts > /tmp/Contacts.h
```

Document every class, property, command, enumeration. Test each capability against a real Contacts instance. Contacts.app's SDEF is historically thin — Apple has invested less in scripting Contacts than scripting Mail or Calendar. Expect significant gaps.

#### 2.1.2 Contacts.framework via PyObjC

The modern API. Replaces `AddressBook.framework` (deprecated in macOS 10.11). Probe:

- `CNContactStore` — top-level entry point
- `CNContact` / `CNMutableContact` — contact CRUD
- `CNGroup` / `CNMutableGroup` — group CRUD
- Predicate-based fetches (`CNContact.predicateForContactsMatchingName:`, etc.)
- `CNContactVCardSerialization` — vCard import/export
- `CNContactStore.requestAccessForEntityType:completionHandler:` — TCC authorization
- `CNSaveRequest` — atomic write batching

**TCC caveat (critical):** Contacts is a TCC-protected data class. The first read triggers a system permission prompt. The user can grant or revoke access via *System Settings → Privacy & Security → Contacts*. Document:
- The auth flow (`CNAuthorizationStatus`: `notDetermined`, `restricted`, `denied`, `authorized`)
- How to handle each state in error messages returned to the LLM (use `error_type: "authorization_denied"`)
- How `Info.plist` keys (`NSContactsUsageDescription`) interact with permission prompts when packaged
- Whether running unbundled (`uv run python -m apple_contacts_mcp.server`) prompts correctly. Test under both Claude Desktop launch and standalone invocation.

#### 2.1.3 JXA (JavaScript for Automation)

```bash
osascript -l JavaScript -e 'Application("Contacts").people()'
```

Probe whether JXA exposes anything SDEF doesn't. Historically JXA wraps the same scripting bridge but occasionally reveals extra properties.

#### 2.1.4 vCard 3.0/4.0

Portable interchange format. `CNContactVCardSerialization` is the canonical encoder/decoder. Investigate:
- Round-trip fidelity (does export-then-import preserve all properties?)
- Embedded photos (base64 in vCard 3, separate `BINARY;ENCODING=B` in 4)
- Custom labels and unrecognized properties
- Group membership (vCard 4 only)

### 2.2 Deliverable

`docs/research/contacts-api-gap-analysis.md` — committed to the repo and merged via PR. Required content:

1. **Inventory** — every Contacts UI feature visible to a user
2. **Per-surface accessibility matrix** — SDEF / Contacts.framework / JXA / vCard / none
3. **Working code samples** for each capability that's accessible (Python preferred for framework, AppleScript for SDEF)
4. **TCC notes** — how authorization is requested, granted, denied, revoked
5. **Priority tiers:**
   - **P1** core CRUD: read/create/update/delete contact, list contacts
   - **P2** filters/queries, group operations, vCard import/export
   - **P3** niche: custom properties, social profiles, image manipulation, etc.
6. **Open empirical questions** still outstanding

### 2.3 Decision point

At the end of Phase 0, **pick a primary API**. Likely `Contacts.framework` via PyObjC if its bindings are usable; AppleScript as fallback for capabilities the framework doesn't expose. This decision drives:

- Skill name in Step 4: `applescript-contacts` vs `contacts-framework`
- Whether `check_applescript_safety.sh` is copied as-is or paired with `check_pyobjc_safety.sh`
- Shape of the `_run_*` mock boundary in `contacts_connector.py`
- v0.1.0 scope (Step 3)

Land Phase 0 on a `research/issue-N-api-gap-analysis` branch and merge via PR before continuing.

---

## Step 3 — Roadmap via GitHub Milestones + Issues

After Phase 0:

### 3.1 Create milestones

```bash
gh api repos/:owner/:repo/milestones -f title="v0.1.0" -f description="Core CRUD (P1)"
gh api repos/:owner/:repo/milestones -f title="v0.2.0" -f description="Filters and queries (P2)"
gh api repos/:owner/:repo/milestones -f title="v0.3.0" -f description="Groups + vCard (P3)"
gh api repos/:owner/:repo/milestones -f title="v0.4.0" -f description="Infrastructure hardening — coverage to 90%, rate limiting, confirmation UX, test-mode safety"
```

### 3.2 Draft `INITIAL_ISSUES.md`

Mirror the format of [apple-mail-mcp/INITIAL_ISSUES.md](/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/INITIAL_ISSUES.md):

- Top-level: `## Milestone: vX.Y.Z — Title`
- Sub-sections by category: Infrastructure, New Tools, Quality, Performance, Research, Testing, Security
- Numbered issues with prefix tag: `[infra]`, `[feature]`, `[quality]`, `[perf]`, `[research]`, `[security]`
- Each issue: title, labels, 1-paragraph description (becomes the GitHub issue body)
- Label set: `infrastructure`, `testing`, `enhancement`, `security`, `refactor`, `documentation`, `research`, `performance`, `priority:high`

Each issue body should reference the corresponding section of `docs/research/contacts-api-gap-analysis.md` so the implementer knows the empirical basis.

### 3.3 File issues

```bash
gh issue create --milestone v0.1.0 --label feature,priority:high \
  --title "[feature] list_contacts: read-only paged listing" \
  --body "$(cat <<'EOF'
Description from INITIAL_ISSUES.md.

Refers to: docs/research/contacts-api-gap-analysis.md §X
EOF
)"
```

Commit `INITIAL_ISSUES.md` to the repo as a historical record of the v0.1–v0.4 plan.

---

## Step 4 — Skills Catalog

Two-tier setup.

### 4.1 Copied verbatim from apple-mail-mcp (portable)

Already done in Step 1.3:

| Skill | Purpose |
|---|---|
| `api-design` | Tool design philosophy + decision tree (when to add a new tool vs. extending an existing one). Read this **before** adding any new `@mcp.tool()`. |
| `integration-testing` | Three-tier strategy (unit / integration / e2e) and why mocked tests miss real-API bugs. |
| `release` | 12-phase release workflow: milestone check, version bump, changelog, validation, tagging, PR. |

Adapt only project-name strings; preserve the philosophy verbatim.

### 4.2 Write fresh after Phase 0 (domain-specific)

Defer until Phase 0 is complete and the primary API is chosen.

#### `applescript-contacts` OR `contacts-framework`

(Choose name based on Phase 0 outcome.) Should cover:
- Object model overview (CNContactStore vs. AppleScript "person")
- String escaping (AppleScript) or NSString interop (PyObjC)
- JSON emission patterns (ASObjC + NSJSONSerialization for AppleScript; native dict→json for PyObjC)
- TCC authorization flow and error mapping
- Known limitations of the chosen API
- Common gotchas (e.g., quote `|name|` keys for ASObjC; coerce `missing value` defaults; PyObjC retain-cycle pitfalls if applicable)

#### `contacts-performance`

Operation baselines and batch patterns:
- Single contact read/write timings on the chosen API
- Batch operation patterns (single script vs. N subprocess loops; `CNSaveRequest` batching for the framework)
- Pagination strategy
- When to use predicate fetches vs. iterating all contacts

**Establish baselines empirically.** Do not copy mail timings — the performance characteristics differ between AppleScript-based and PyObjC-based access (no subprocess overhead in the latter).

---

## Step 5 — Workflow Conventions

For each item, see `MCP_PLAYBOOK.md` for the canonical version. Contacts-specific deltas only:

- **Branch naming** — `{type}/issue-{N}-{slug}` (playbook §2)
- **TDD** — RED → GREEN → REFACTOR; test precedes code (playbook §3)
- **Backend + frontend together** — every feature touches both `contacts_connector.py` and `server.py`. `check_client_server_parity.sh` enforces this. (playbook §1)
- **Sanitize twice** — `sanitize_input()` → escape function. The escape function name is set after Phase 0 (e.g., `escape_applescript_string()` if AppleScript wins; PyObjC may not need a separate escape if all calls use bound methods rather than string templates). (playbook §4)
- **Structured responses** — every tool returns `{"success": bool, ...}`; errors include `error` and `error_type`. (playbook §1)
- **Security checklist per feature** — the 5 from playbook §4 (input sanitization, escaping, path-traversal-safe name validation, rate limiting, audit logging) **plus a 6th, contacts-specific**: TCC authorization status check before any read or write. Map `CNAuthorizationStatus` → `error_type` consistently.
- **Test-mode safety** — `CONTACTS_TEST_MODE=true` + `CONTACTS_TEST_GROUP=<test group name>`. Destructive operations (create/update/delete contact, modify group) verify the target group via the API before proceeding. Pattern: `apple-mail-mcp/src/apple_mail_mcp/security.py:check_test_mode_safety`. (playbook §4)
- **Hard rule** — any AppleScript or framework call written → integration tests must cover it before merge. Unit mocks at `_run_*` cannot catch real-API bugs (variable naming collisions, NSJSONSerialization gotchas, predicate semantics). (playbook §3)
- **CHANGELOG only on release branches** — never feature branches. (playbook §5)
- **Version sync** — across `pyproject.toml`, `__init__.py`, `CLAUDE.md`, `README.md`. `check_version_sync.sh` enforces. (playbook §5)

---

## Step 6 — Documentation (accrue, do not pre-write)

| File | Owner | Notes |
|---|---|---|
| `.claude/CLAUDE.md` | This project | Initially a pointer to BOOTSTRAP + PLAYBOOK. Accrues dense contacts-specific guidance as work proceeds. |
| `docs/reference/TOOLS.md` | This project | Complete API reference. Kept in sync with `@mcp.tool()` registrations (parity check enforces). |
| `docs/research/contacts-api-gap-analysis.md` | Phase 0 | The empirical foundation of the roadmap. |
| `docs/guides/SECURITY_CHECKLIST.md` | This project | Copy from `apple-mail-mcp/docs/guides/SECURITY_CHECKLIST.md` and add the TCC concern. |

---

## Quick-Start Checklist

- [ ] **Step 1** — repo created, skeleton scaffolded, hooks installed, CI green
- [ ] **Step 2** — Phase 0 research complete; gap analysis filed and merged
- [ ] **Step 3** — milestones created on GitHub; `INITIAL_ISSUES.md` drafted; issues filed and assigned
- [ ] **Step 4** — domain-specific skills (`applescript-contacts` or `contacts-framework`, plus `contacts-performance`) written based on chosen API
- [ ] **Step 5** — first feature TDD'd. Suggested first feature: `list_contacts` (read-only, trivially scoped, exercises the auth flow once and unblocks everything else).
- [ ] **Step 6** — first release `v0.1.0` cut via the release skill

---

## Anti-Patterns (carried lessons)

- **Don't pre-decide v0.1.0 scope before Phase 0.** Contacts.app's API surface is not symmetric with Mail.app's. The roadmap derives from what's possible, not from a sibling project's structure.
- **Don't copy the `applescript-mail` skill verbatim.** Contacts has a different object model. Some patterns transfer (escaping, JSON emission) but the named entities and quirks are different — write fresh.
- **Don't skip TCC authorization handling.** A first failed read with no granted permission is a confusing UX — surface a clear error with `error_type: "authorization_denied"` and instructions to grant access.
- **Don't add specialized search/getter functions before exhausting parameter additions to existing tools.** See the `api-design` decision tree.
- **Don't update CHANGELOG on feature branches.** Only on `release/*` branches.
- **Don't use raw `Path(user_input)` for any name-derived filesystem path.** Validate with regex first. See `_validate_name` in `apple-mail-mcp/src/apple_mail_mcp/templates.py`.
- **Don't trust unit tests to catch AppleScript or framework bugs.** They mock at `_run_*` and miss everything below it. Integration tests are mandatory for any code that touches the real API.
- **Don't formatted-text-return from tools.** Always structured dicts. The agent decides presentation.

---

## Source-of-Truth File Index

Paths Claude Code in this repo will need during Step 1:

- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/Makefile`
- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/pyproject.toml`
- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/.claude/CLAUDE.md`
- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/.claude/settings.json`
- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/.claude/commands/merge-and-status.md`
- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/.claude/skills/{api-design,integration-testing,release}/`
- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/scripts/check_*.sh`
- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/scripts/install-git-hooks.sh`
- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/scripts/create_tag.sh`
- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/scripts/hooks/`
- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/scripts/git-hooks/`
- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/.github/`
- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/INITIAL_ISSUES.md`
- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/CONTRIBUTING.md`
- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/SECURITY.md`
- `/Users/Morgan/Development/agent-tools/mcp-servers/apple-mail-mcp/docs/guides/SECURITY_CHECKLIST.md`

After Step 1 completes, this repo is self-sufficient. apple-mail-mcp is a reference, not a runtime dependency.
