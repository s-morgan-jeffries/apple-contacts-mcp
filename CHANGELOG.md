# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-05-09

Phase 2 release. Eight new tools spanning field-scoped search, group read/write, note read/write (AppleScript fallback), and vCard 3.0 import/export. Two breaking input-shape changes vs v0.1.0 (`search_contacts` predicates, `label` field on labeled values).

### Added

- `search_contacts` — phone, email, and organization predicate modes alongside the existing name search. Phone matching uses Apple's format-tolerant `predicateForContactsMatchingPhoneNumber:` (with `CNPhoneNumber` wrapping); email uses `predicateForContactsMatchingEmailAddress:`; organization uses a custom `CONTAINS[cd]` `NSPredicate` to mirror name-mode case- and diacritic-insensitive substring behavior (#16).
- `read_note(identifier)` and `write_note(identifier, note, group_identifier=None)` — first AppleScript-fallback tools. The `note` field is entitlement-gated in `Contacts.framework` so we route through `osascript` against Contacts.app. `write_note(id, note="")` clears the note; the connector also issues `save` so writes persist to disk (#19).
- `escape_applescript_string()` helper in `utils.py` — backslash-then-quote escape for safe interpolation inside AppleScript double-quoted literals. Used by `write_note` and any future AppleScript callers (#21).
- `write_note` added to `DESTRUCTIVE_OPERATIONS` (test-mode gated like `update_contact`).
- `list_groups()` — enumerate all contact groups across all containers; returns `{id, name, container_id}` per entry, capped at 200 (#17).
- `get_contacts_in_group(identifier)` — list contacts whose membership includes the given group; same 4-field shape as `list_contacts`, capped at 200; pre-flights via `_run_cn_fetch_group` so unknown identifiers return `not_found` distinctly from real-but-empty groups (#17).
- `add_contact_to_group(contact_identifier, group_identifier)` and `remove_contact_from_group(contact_identifier, group_identifier)` — destructive group-membership writes. Add uses `CNSaveRequest.addMember:toGroup:`; **remove uses AppleScript** (`remove p from g` + `save`) because Apple's `CNSaveRequest.removeMember:fromGroup:` silently no-ops despite reporting success — empirically discovered during #18 and locked in by the integration suite. Test-mode gated like `update_contact`; success returns both identifiers (#18).
- Both group-membership ops added to `DESTRUCTIVE_OPERATIONS`.
- `export_vcard(identifiers)` — vCard 3.0 export via `CNContactVCardSerialization.dataWithContacts:`. Atomic over the id list (first missing identifier aborts). Response includes a `notes` list calling out the NOTE-field omission and the year-less-BDAY corruption per #23. No transformation of Apple's output — see `docs/research/vcard-version-decision.md` (#20).
- `import_vcard(vcard_text, group_identifier=None)` — parse vCard 3.0 or 4.0 input via `contactsWithData:` and persist via a single `CNSaveRequest`. Atomic; multi-contact input commits as one unit. Test-mode gated like `create_contact`; success returns the new identifiers in input order. Malformed input dispatches `validation_error` (caller's input was bad), distinct from `unknown` for save failures (#20).
- `import_vcard` added to `DESTRUCTIVE_OPERATIONS`.
- `label_to_apple_token()` helper in `utils.py` — translates English human-form labels (`"mobile"`, `"home fax"`, `"iPhone"`) to Apple's raw token form (`_$!<Mobile>!$_`) for built-in labels. Apple tokens and custom strings pass through unchanged. 12-entry English table empirically probed against macOS 26.3.1. Closes gap-analysis open Q4 (#22).

### Changed

- `search_contacts` signature: `query: str` is replaced by four mutually-exclusive parameters (`name`, `phone`, `email`, `organization`). Exactly one must be set; whitespace-only counts as unset. **Breaking change** vs v0.1.0 (#16).
- `search_contacts` success response: the `query` key is replaced by flat `search_field` + `search_value` keys. `search_value` echoes the stripped value (#16).
- `create_contact` and `update_contact` input shape: phones / emails / urls / postal_addresses now take a `label` field instead of `label_raw`. The `label` field accepts human forms (`"mobile"`, `"home fax"`), Apple tokens (`"_$!<Mobile>!$_"`), or custom strings (`"Spotify"`); the helper translates as needed. Read-side response is unchanged — `get_contact` still emits both `label_raw` (token, identity) and `label` (Apple's localized display). **Breaking change** vs v0.1.0 (#22).

### Security

- Identifier escaping in AppleScript paths (`read_note`, `write_note`, `remove_contact_from_group`). CN-issued identifiers are UUID-shaped and contain no AppleScript metacharacters, so this is a no-op for legitimate input — but applies `escape_applescript_string` defensively at the connector boundary so adversarial input from an MCP caller can't inject AppleScript via the `identifier` parameter. Caught in release-gate code review.

### Documentation

- vCard version-export decision recorded in [`docs/research/vcard-version-decision.md`](docs/research/vcard-version-decision.md) — emit Apple's vCard 3.0 verbatim, document limitations (NOTE omitted, year-less BDAYs use Apple's `X-APPLE-OMIT-YEAR=1604` hack that corrupts to "1604" for non-Apple consumers). Empirically probed against macOS 26.3.1. Closes gap-analysis open Q3 and unblocks `export_vcard` / `import_vcard` work in #20 (#23).
- `read_note` tool docstring corrected — it previously claimed bare-UUID input worked, but AppleScript's `id of person` requires the `:ABPerson` suffix.

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
