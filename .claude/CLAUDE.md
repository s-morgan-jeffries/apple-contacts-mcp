# Apple Contacts MCP Server

**Version:** v0.3.0

v0.3.0 ships the Phase 3 surface: containers (`list_containers` +
`container_identifier` on `create_contact`), group CRUD (`create_group`,
`rename_group`, `delete_group`), contact photo read/write with magic-byte
format detection (`read_photo`, `write_photo`), and four niche labeled-value
families (`dates`, `social_profiles`, `relations`, `instant_messages`) opt-in
via `include_niche=True` on `get_contact`. Also closes gap-analysis Q8a
(multi-container write round-trip empirically resolved) and lands the
per-tool performance baselines suite. See
[docs/reference/TOOLS.md](../docs/reference/TOOLS.md) for the API surface
and [CHANGELOG.md](../CHANGELOG.md) for release notes.

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
Decision drives: skill name `contacts-framework` (BOOTSTRAP §4.2); `scripts/check_pyobjc_safety.sh` enforcing five anti-patterns (KVC dynamic keys, vCard descriptor, photo-data guard, TCC pre-check, test-mode safety) — shipped #31; paired `check_applescript_safety.sh` still deferred; `_run_cn_*` + `_run_applescript_*` mock boundaries in `contacts_connector.py`.
