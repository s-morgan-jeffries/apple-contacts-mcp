# Apple Contacts MCP Server

**Version:** v0.4.0

v0.4.0 is the infrastructure-hardening milestone. **No new tools** — the 21-tool
surface from v0.3.0 is unchanged. Every `@mcp.tool()` now sits behind a uniform
gate stack: input validation → sliding-window rate limit (`security.py`:
`TIER_LIMITS`, `OPERATION_TIERS`, three tiers) → TCC entry check → operation →
post-call TCC re-verification (catches mid-call revocation, #37) → audit log
(`OperationLogger`, PII-curated params only, #47). Destructive ops
(`delete_contact`, `delete_group`) gained FastMCP-elicitation confirmation UX
(#36). Coverage gate raised 90% → 95% (#30). `scripts/check_pyobjc_safety.sh`
statically enforces five PyObjC anti-patterns at PR time (#31), and
`scripts/check_tools_md_parity.sh` enforces `@mcp.tool()` ↔ TOOLS.md parity
(#51). Empirically resolved gap-analysis §3: macOS attributes Contacts TCC to
the venv `python-3.11` interpreter, not to anything this repo ships — the
`packaging/Info.plist` scaffold was shipped (#34) and reverted (#97) within the
same release cycle. See [docs/reference/TOOLS.md](../docs/reference/TOOLS.md)
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
Decision drives: skill name `contacts-framework` (BOOTSTRAP §4.2); `scripts/check_pyobjc_safety.sh` enforcing five anti-patterns (KVC dynamic keys, vCard descriptor, photo-data guard, TCC pre-check, test-mode safety) — shipped #31; paired `check_applescript_safety.sh` still deferred; `_run_cn_*` + `_run_applescript_*` mock boundaries in `contacts_connector.py`.
