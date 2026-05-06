# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
