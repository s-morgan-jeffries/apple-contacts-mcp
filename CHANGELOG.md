# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `search_contacts` — phone, email, and organization predicate modes alongside the existing name search. Phone matching uses Apple's format-tolerant `predicateForContactsMatchingPhoneNumber:` (with `CNPhoneNumber` wrapping); email uses `predicateForContactsMatchingEmailAddress:`; organization uses a custom `CONTAINS[cd]` `NSPredicate` to mirror name-mode case- and diacritic-insensitive substring behavior (#16).
- `read_note(identifier)` and `write_note(identifier, note, group_identifier=None)` — first AppleScript-fallback tools. The `note` field is entitlement-gated in `Contacts.framework` so we route through `osascript` against Contacts.app. `write_note(id, note="")` clears the note; the connector also issues `save` so writes persist to disk (#19).
- `escape_applescript_string()` helper in `utils.py` — backslash-then-quote escape for safe interpolation inside AppleScript double-quoted literals. Used by `write_note` and any future AppleScript callers (#21).
- `write_note` added to `DESTRUCTIVE_OPERATIONS` (test-mode gated like `update_contact`).
- `list_groups()` — enumerate all contact groups across all containers; returns `{id, name, container_id}` per entry, capped at 200 (#17).
- `get_contacts_in_group(identifier)` — list contacts whose membership includes the given group; same 4-field shape as `list_contacts`, capped at 200; pre-flights via `_run_cn_fetch_group` so unknown identifiers return `not_found` distinctly from real-but-empty groups (#17).

### Changed

- `search_contacts` signature: `query: str` is replaced by four mutually-exclusive parameters (`name`, `phone`, `email`, `organization`). Exactly one must be set; whitespace-only counts as unset. **Breaking change** vs v0.1.0 (#16).
- `search_contacts` success response: the `query` key is replaced by flat `search_field` + `search_value` keys. `search_value` echoes the stripped value (#16).

## [0.1.0] - 2026-05-06

First feature release. Seven CRUD tools backed by `Contacts.framework` via PyObjC, gated by TCC authorization checks and test-mode safety.

### Added

- `check_authorization` — query the current TCC authorization status without triggering the system permission prompt. Returns `success: true` for every status (status-query semantics) with status-specific remediation copy when access is not granted (#9).
- `list_contacts(offset, limit)` — paged read-only listing returning `{id, given_name, family_name, organization}` per entry. Default 50/page, hard cap 200 (#10).
- `get_contact(identifier)` — full P1 contact dict including name parts, organization triplet, phones, emails, urls, postal addresses, and birthday. Each labeled-value entry carries both the raw Apple token (e.g. `_$!<Mobile>!$_`) and the localized string (`mobile`) (#11).
- `search_contacts(query)` — substring/case-insensitive search via `predicateForContactsMatchingName:`. Same 4-field shape as `list_contacts`, hard cap 200 results (#12).
- `create_contact(...)` — write via `CNMutableContact` + `CNSaveRequest.addContact:toContainerWithIdentifier:` to the user's default container. Optional `group_identifier` adds the new contact to a group atomically. Returns the new contact's CN identifier (#13).
- `update_contact(identifier, ...)` — partial-field update with presence semantics (`None` = don't touch, `""` = explicitly clear). Multi-valued lists (phones / emails / urls / postal_addresses) follow REST-PUT replace semantics (#14).
- `delete_contact(identifier)` — destructive delete via `CNSaveRequest.deleteContact:`. **v0.1.0 only allows delete in test mode**; the full destructive UX (with confirmation prompts) ships in v0.4.0 (#14, #24).
- Test-mode safety gate (`CONTACTS_TEST_MODE` + `CONTACTS_TEST_GROUP`): destructive ops are constrained to a designated test group, and `delete_contact` is refused entirely outside test mode (#6, #14).
- Mock-boundary helpers in `contacts_connector.py` (`_run_cn_*`, `_run_applescript`) so unit tests mock at the connector edge and integration tests hit real `CNContactStore` (#5).
- Integration test rig under `tests/integration/` covering every `_run_cn_*` helper. Skip-by-default via `--run-integration` flag; session-scoped `MCP-Test` group fixture handles setup and cleanup against a real address book (#15).
- API reference at [docs/reference/TOOLS.md](docs/reference/TOOLS.md) — every tool's signature, parameters, success/error response shapes, and error_type catalog (#8).
- Phase 0 API gap analysis at `docs/research/contacts-api-gap-analysis.md` documenting the empirical basis for choosing `Contacts.framework` over AppleScript / JXA / vCard (#2).
- Claude skills `contacts-framework` and `contacts-performance` capturing PyObjC bridging gotchas and per-tool perf baselines (#7).

### Fixed

- `_run_cn_enumerate_contacts` no longer crashes on real PyObjC: the `BOOL *stop` argument arrives as `None` for the `enumerateContactsWithFetchRequest:error:usingBlock:` selector. Caught by the integration test rig on its first run; defensively guarded the assignment and added a unit-level regression test (#15).
