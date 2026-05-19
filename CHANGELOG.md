# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `tests/integration/test_modification_dates.py` — durability regression test for the undocumented `CNContact` runtime selectors `creationDate` and `modificationDate`. Fetches a freshly-created contact via `unifiedContactWithIdentifier_keysToFetch_error_` with the selectors as string key paths, calls each selector, and asserts the return is a non-None `datetime`. On failure, the diagnostic points to the AppleScript fallback (`creation date of person`, `modification date of person`) per SKILL.md §2. Closes gap-analysis Q2; the question is now empirically gated rather than open (#33).
- `scripts/configure_branch_protection.sh` — one-shot, idempotent capture of the GitHub branch-protection rules on `main`: required `Tests / unit-tests` status check (strict — branch must be up to date), PR required (no direct pushes), zero required reviews (solo repo; GitHub prohibits self-approval), linear history, `enforce_admins=true`, force-pushes and deletions disallowed. Takes optional `[OWNER/REPO] [BRANCH]` args; defaults derive from `gh repo view`. The PUT payload lives in the script so the exact config is auditable in-repo. BOOTSTRAP.md §1.1 now points at the script instead of describing the manual `gh api` call (#32).
- `scripts/check_pyobjc_safety.sh` — static analyzer enforcing five PyObjC anti-patterns at PR time. (1) No KVC dynamic-key calls (`setValue_forKey_` / `valueForKey_`). (2) Every `dataWithContacts_error_` must be paired with `descriptorForRequiredKeys()` in the same function. (3) Every `imageData()` call must be guarded by `imageDataAvailable()`. (4) Every `@mcp.tool()` touching `connector._run_cn_*` must first call `_require_contacts_authorization()` (exception: `check_authorization`). (5) Every `@mcp.tool()` whose name is in `DESTRUCTIVE_OPERATIONS` must call `check_test_mode_safety()`. Pattern 1 is grep; patterns 2-5 are an `ast`-walker that scopes by function body. Wired into both the `make audit` target and `.github/workflows/release-hygiene.yml`. 17 unit tests (fixture-driven, one violation + one safe form per pattern, plus a smoke test on the real `src/`) lock in the checker's own behavior. Codebase is currently clean on all five patterns — this PR is preventive (#31).
- `OperationLogger` audit log in `security.py` — append-only in-memory record of every `@mcp.tool()` invocation with `{timestamp, operation, parameters, result}` shape. Four `result` values: `"success"` (logged by the tool body before the success return), `"rate_limited"` (by `check_rate_limit` on deny), `"safety_violation"` (by `_safety_error`, covering both `check_test_mode_safety` and `require_test_mode_for`, plus the elicit-unsupported branch of `_confirm_destructive`), and `"cancelled"` (by `_confirm_destructive` when the user declines, cancels, or explicitly answers "No"). Module singleton `operation_logger`; `get_recent_operations(limit=10)` returns the last N entries (#47).

### Changed

- All 21 `@mcp.tool()` entries now call `operation_logger.log_operation(<name>, <curated-params>, "success")` immediately before each success return. Logged parameters are curated to exclude PII and content: identifiers, container/group IDs, counts (phone/email/url/postal/date/social/relation/im), lengths (note bytes, vcard bytes, image bytes, name lengths), booleans (`has_birthday`, `has_photo`, `clearing`), and sorted field-name lists for partial updates. Actual name/email/phone/URL/note/image content is NEVER persisted to the audit log. `read_photo` logs at two distinct sites (one per success branch — has-photo vs. no-photo). The three security primitives (`check_rate_limit`, `_safety_error`, `_confirm_destructive`) emit denied/cancelled entries themselves — tool bodies stay single-purpose. New drift-guard test `test_every_tool_calls_log_operation_with_own_name` mirrors #46's rate-limit guard and fails CI if a new tool ships without an audit-log call (#47).
- `check_rate_limit` now consumes its `params` argument on deny: the call is appended to `operation_logger` with `result="rate_limited"`. The forward-compat docstring from #35 is removed. Tool-body callers pass no params today; the empty dict shapes the audit entry consistently (#47).
- `_safety_error` gained an optional `params` kwarg defaulting to `{"violation": <message>}` so safety-gate denies land an audit entry with a clear payload even when the caller doesn't thread its own params (#47).
- All 21 `@mcp.tool()` entries now call `check_rate_limit(<operation>)` after input validation and before the auth gate. The rate-limit primitive shipped in #35 is now actually enforced. Tools that exceed their tier's sliding window return `error_type: rate_limited` without triggering a TCC prompt or touching the connector. New drift-guard test `test_every_tool_calls_check_rate_limit_with_own_name` walks the server.py source and fails CI if a new tool ships without the gate (#46).

### Added

- Sliding-window rate limiter in `security.py` (`RateLimiter`, `TIER_LIMITS`, `OPERATION_TIERS`, `check_rate_limit`, `rate_limiter` singleton). Three tiers: `cheap_reads` (60/60s), `expensive_ops` (20/60s), `destructives` (5/60s). All 21 tools have tier mappings. **Built but not wired into any tool yet** — wiring is tracked under #46. Documented in TOOLS.md's error-types appendix so callers can prepare. Pattern mirrors apple-mail-mcp's security module (#35).

### Changed

- Dev dependency `mypy` bumped from 1.20.2 → 2.1.0. Mypy stays informational in CI (`continue-on-error: true`). Two type-annotation fixes in `security.py` were required to satisfy mypy 2.x's stricter overload resolution and parameter typing: `_confirm_destructive` now types its `preview_lookup` / `describe` callables as `Any` (the callers pass framework-specific types like `CNGroup`, not just `dict`), and the `ctx.elicit(response_type=[...])` call has a targeted `# type: ignore[arg-type]` because mypy 2.x picks the wrong FastMCP overload for list literals — runtime dispatch is correct.
- Coverage gate raised from **90% → 95%** (`fail_under` in `pyproject.toml`; the redundant `--cov-fail-under=90` CLI flag was dropped from the CI workflow so pyproject is the single source of truth). Actual coverage is ~96.7% — the new floor locks in the gains from v0.1.0–v0.4.0's real test coverage without flaking on incremental PRs (#30).

### Removed

- `tests/unit/test_smoke.py` — bootstrap scaffolding that gave the empty skeleton ≥90% coverage on stub modules. Real implementations from v0.1.0 onward replaced its purpose; the six trivial tests were all subsumed by feature-specific tests (`test_version_sync.sh` covers the version check, every connector test exercises `ContactsConnector()`, etc.) (#30).
- `sanitize_input(value)` from `security.py` — bootstrap stub that returned its input unchanged and was referenced only by the smoke test. The real AppleScript-escape logic lives in `escape_applescript_string` in `utils.py`; no production code path called `sanitize_input` (#30).

### Changed

- **Authorization revocation mid-process is now caught.** Every data tool already re-checked TCC status on entry; v0.4.0 adds `_verify_authorization_still_granted()` as a post-call check that closes the race window. For read tools (`list_contacts`, `search_contacts`, `get_contact`, `list_groups`, `get_contacts_in_group`, `list_containers`), suspicious results (empty list, None contact) now trigger a status re-check before being dispatched as empty or `not_found`. For destructive tools (`create_contact`, `update_contact`, `delete_contact`, `create_group`, `rename_group`, `delete_group`, `write_note`, `write_photo`, `add_contact_to_group`, `remove_contact_from_group`, `import_vcard`), the post-call check runs unconditionally — if TCC was revoked during the save, the persistence is undefined and the caller now learns about it via `authorization_denied` instead of a misleading `success: true`. Closes gap-analysis Q6; #37.
- TOOLS.md error-types appendix entry for `authorization_denied` updated to document the two surfacing points (entry-check and post-call).
- New manual integration runbook at `tests/integration/test_authorization_revocation.md` — TCC revocation isn't scriptable from CI, so this documents the procedure for verifying real-CN behavior after auth-related changes.
- `delete_contact` and `delete_group` are now `async` and accept the FastMCP `Context` so they can elicit confirmation from the user. Outside test mode they prompt with `Delete <kind> '<name>' (<id>)? This cannot be undone.` (Yes / No); inside test mode the existing test-group safety gate applies unchanged. The entity name is pre-fetched so the prompt isn't an opaque UUID; a missing identifier short-circuits to `not_found` without prompting. New `_confirm_destructive` helper in `security.py` centralizes the elicitation logic and the unsupported-client fallback (#36).

### Added

- `user_declined` error type for the two delete tools when the user declines or cancels the confirmation prompt. Documented in TOOLS.md's error-types appendix.

### Removed

- `require_test_mode_for` is no longer wired into any tool. The helper remains in `security.py` for forward-compatibility but the v0.1.0–v0.3.x posture of "destructive ops refuse outright outside test mode" has been replaced by the elicitation flow. Clients that don't support elicitation fall back to a clear `safety_violation` pointing at `CONTACTS_TEST_MODE` (#36).

## [0.3.0] - 2026-05-12

Phase 3 release. Six issues closed: container-aware tools (#26), photo read/write (#25), group CRUD (#24), niche fields (#27), per-tool performance baselines (#28), and the multi-container write round-trip research (#29). Plus a release-gate cleanup PR (#73) clearing pre-existing IDE lint/schema warnings. Tool count: 16 → **21**.

### Fixed

- `_run_cn_update_contact` now fetches the four P3 niche keys (`CNContactDatesKey`, `CNContactSocialProfilesKey`, `CNContactRelationsKey`, `CNContactInstantMessageAddressesKey`) so partial-field updates of any niche field don't trigger `CNPropertyNotFetchedException` at the setter. Caught in the v0.3.0 release-gate review. Locked in by a regression test that asserts the keysToFetch list contains all four.

### Security

- Bumped transitive dependency urllib3 2.6.3 → 2.7.0 (CVE-2026-44431, CVE-2026-44432). Pulled in via requests; no direct caller affected, but the release-gate dependency scan caught it.

### Added

- `list_containers` — enumerate contact containers (accounts: iCloud, Gmail, Exchange, On-My-Mac). Returns `{id, name, type, is_default}` per entry, capped at 10. `type` is one of `"local"` / `"exchange"` / `"cardDAV"` (even iCloud reports as `cardDAV` — the sync protocol). `is_default` flags the container new contacts go into when `container_identifier` isn't specified (#26).
- `create_contact` gained an optional `container_identifier` parameter — pass a container UUID from `list_containers` to write to a non-default account; default `None` keeps current iCloud-default behavior. Response now echoes both `group_id` and `container_id` (both `null` when input absent, per the v0.2.1 response-shape convention). Empirical basis: [`docs/research/multi-container-write-decision.md`](docs/research/multi-container-write-decision.md). **Non-breaking** for existing callers (#26).
- `create_group(name, container_identifier=None)` — create a new contact group via `CNMutableGroup` + `CNSaveRequest.addGroup:toContainerWithIdentifier:`. Lands in the default container unless `container_identifier` is supplied. Returns `{id, name, container_id}`. Test-mode gated like `create_contact` (#24).
- `rename_group(identifier, new_name)` — rename an existing group via `CNSaveRequest.updateGroup:`. Returns the updated `{id, name, container_id}`. Test-mode gated like `update_contact` (#24).
- `delete_group(identifier)` — delete a group via `CNSaveRequest.deleteGroup:`. **Test-mode-only in v0.3.x** (same posture as `delete_contact`); confirmation UX ships in v0.4.0 (#36). Member contacts are NOT deleted — they remain in the address book, just lose membership in the now-removed group (#24).
- `create_group` / `rename_group` / `delete_group` added to `DESTRUCTIVE_OPERATIONS` so the test-mode safety gate covers them.
- `read_photo(identifier)` — read a contact's photo. Returns `{image_data: <base64>, format: "jpeg" | "png" | "gif" | "heic" | "unknown", size_bytes: N}` when a photo is set; `{image_data: null, format: null, size_bytes: 0}` when the contact exists but has no photo. The contact-not-found case dispatches `not_found` distinctly. Per the gap analysis gotcha, the connector always checks `imageDataAvailable()` before calling `imageData()` (#25).
- `write_photo(identifier, image_data, group_identifier=None)` — set or clear a contact's photo via `setImageData_`. `image_data` is base64-encoded; `None` clears. Permissive on format — Apple is the authority on accepted bytes. Test-mode gated like `update_contact` (#25).
- `detect_image_format(bytes) -> str` helper in `utils.py` — magic-byte detector returning one of `"jpeg"` / `"png"` / `"gif"` / `"heic"` / `"unknown"`. The HEIC bucket covers all HEIF-family ISOBMFF brands Apple emits. Pure function; no PyObjC dependency.
- Four P3 niche labeled-value families wired through `get_contact` / `create_contact` / `update_contact`: `dates` (custom dates), `social_profiles`, `relations`, `instant_messages`. Each follows the existing labeled-value shape (`{label, label_raw, ...value fields}` on read; `{label, ...value fields}` on write). Per-entry validation: dates need ≥1 component in range; social profiles need ≥1 of username/url; relations need name; instant messages need username (#27).
- `get_contact` gained an `include_niche: bool = False` parameter. When True, the four niche keys appear in the response (possibly as empty lists); when False (default), the keys are absent — keeps default responses compact (#27).
- Per-tool benchmark suite under [`tests/benchmarks/`](tests/benchmarks/) — 14 ops timed via `make benchmark` (compare) and `make benchmark-baseline` (capture). Opt-in via `--run-benchmark`; CI doesn't run them. Median timings committed under `tests/benchmarks/baseline.json`; compare mode asserts measured ≤ 3× baseline. Fills in the "per-tool baselines still untaken" gap left at Phase 0; `contacts-performance` skill now carries the captured numbers (#28).

### Fixed

- Stale `(#24)` reference in `require_test_mode_for`'s error message and `delete_contact`'s docstring — pointed at the Group CRUD issue (this PR) instead of the actual v0.4.0 confirmation-UX issue. Now correctly reads `(#36)` (#24).

## [0.2.1] - 2026-05-10

Patch release covering release-gate follow-ups from v0.2.0. One breaking shape change (`create_contact` `group_id`) and one infrastructure fix (`check_complexity.sh` actually enforces the documented threshold now).

### Changed

- `create_contact` success response: `group_id` is now always present, `null` when `group_identifier` was not supplied. Aligns with `import_vcard`'s existing shape so callers can use one detection idiom (`response["group_id"] is not None`) across both tools. **Breaking shape change** vs v0.2.0; callers using `"group_id" in response` to detect group assignment must switch (#62).
- `_validate_create_contact_input` and `_validate_update_contact_input` refactored — duplicated per-field-type checks (phones / emails / urls / postal addresses / birthday) extracted into shared module-level helpers (`_validate_phones`, `_validate_emails`, `_validate_urls`, `_validate_postal_addresses`, `_validate_birthday`, `_validate_labeled_value_fields`). Behavior preserved; both outer validators drop from CC=32/29 to single digits, and ~50 lines of duplicated body collapse into one place (#61).

### Fixed

- `scripts/check_complexity.sh` had been silently failing on every PR since #53 introduced a Python 3.10 `match` statement. Two underlying bugs: (1) the script invoked whatever `radon` was on `PATH`, which on developer machines often resolved to a system-Python-3.9 install that can't parse `match` — switch to `uv run radon` so it always uses the project's pinned Python (3.10+); (2) the gate used `radon -n F` (CC≥41) despite documenting `THRESHOLD=20` — switch to `-n A` and apply the documented threshold honestly. Also drop `continue-on-error: true` from `.github/workflows/test.yml` so future regressions actually fail CI (#61).

### Documentation

- TOOLS.md gained a top-of-file **Response-shape convention** note: optional id-echo keys (`group_id`, etc.) in success responses are always present, set to `null` when input was absent. Single source of truth for all current and future tools (#62).

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
