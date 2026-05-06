# Apple Contacts MCP Server

**Version:** v0.1.0

v0.1.0 ships the seven core CRUD tools (`check_authorization`, `list_contacts`,
`get_contact`, `search_contacts`, `create_contact`, `update_contact`,
`delete_contact`). See [docs/reference/TOOLS.md](../docs/reference/TOOLS.md)
for the API surface and [CHANGELOG.md](../CHANGELOG.md) for release notes.

- `MCP_PLAYBOOK.md` is the authoritative project-agnostic reference.
- `BOOTSTRAP.md` documents the initial repo setup (mostly historical now).
- This file accrues contacts-specific guidance as the project grows.

## Phase 0 — API decision (2026-04-29)

**Primary:** `Contacts.framework` via PyObjC (`pyobjc-framework-Contacts`).
**Fallback:** AppleScript via `osascript` for two specific cases:
1. **`note` field** — entitlement-gated in CN, silently dropped on fetch + stripped from vCard export.
2. **`modificationDate` / `creationDate`** — accessible only via undocumented runtime selectors in CN.

JXA contributes nothing SDEF doesn't expose — out of scope.
vCard via `CNContactVCardSerialization` is a serialization helper (3.0 only, even on macOS 26).

Full empirical basis: [`docs/research/contacts-api-gap-analysis.md`](../docs/research/contacts-api-gap-analysis.md).
Decision drives: skill name `contacts-framework` (BOOTSTRAP §4.2); paired `check_pyobjc_safety.sh` alongside `check_applescript_safety.sh` (deferred to v0.4.0); `_run_cn_*` + `_run_applescript_*` mock boundaries in `contacts_connector.py`.
