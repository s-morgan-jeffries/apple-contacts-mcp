# Initial Issues

Issues to file after merge. Organized by milestone. Every issue references the relevant section of [`docs/research/contacts-api-gap-analysis.md`](docs/research/contacts-api-gap-analysis.md) so the implementer has the empirical basis.

The four milestones (v0.1.0 → v0.4.0) already exist on GitHub. Tracking issue: [#3](https://github.com/s-morgan-jeffries/apple-contacts-mcp/issues/3).

---

## Milestone: v0.1.0 — Core CRUD (P1)

Maps to [gap analysis §5](docs/research/contacts-api-gap-analysis.md#5-priority-tiers-for-the-v010-roadmap), tier P1, and §7 (the API decision). Goal: an MCP server that can read/create/update/delete a contact, with a working TCC authorization path. No groups, no vCard, no notes (notes are v0.2.0 via AppleScript fallback).

### Infrastructure

1. **[infra] Wire `_run_cn_*` and `_run_applescript_*` mock boundaries in `contacts_connector.py`**
   Labels: `infrastructure`, `priority:high`
   Establish the two helper methods that wrap every Contacts.framework call and every `osascript` invocation. Unit tests mock at this boundary. Pattern: gap analysis §7 ("Connector mock boundary"). One PR, no public methods yet.

2. **[infra] Test-mode safety: `CONTACTS_TEST_MODE` + `CONTACTS_TEST_GROUP` env vars**
   Labels: `infrastructure`, `security`, `priority:high`
   Implement `check_test_mode_safety()` in `security.py` modeled on `apple-mail-mcp/src/apple_mail_mcp/security.py:check_test_mode_safety`. Destructive ops (create/update/delete contact, modify group) verify the target group via the API before proceeding. Required before any write tool ships. Reference: BOOTSTRAP §5 ("Test-mode safety") and gap analysis §6 question 7.

3. **[infra] Author `.claude/skills/contacts-framework/SKILL.md`**
   Labels: `infrastructure`, `documentation`
   Per BOOTSTRAP §4.2: object model overview (`CNContactStore`, `CNContact`, `CNGroup`, `CNContainer`), key descriptors and the `descriptorForRequiredKeys` gotcha, JSON emission patterns, TCC authorization flow + `error_type: "authorization_denied"` mapping, known limitations (notes entitlement, mod-date undocumented selectors, vCard 3.0-only export). Cross-references gap analysis §3, §4, §6.

4. **[docs] `docs/reference/TOOLS.md` initial scaffold**
   Labels: `documentation`
   Per `MCP_PLAYBOOK.md §6` and the parity-check expectation: every `@mcp.tool()` registration has an entry. Initially empty list with the format spec at the top so v0.1.0 features can fill in as they land.

### New Tools

5. **[feature] `check_authorization` — surface TCC status to the LLM**
   Labels: `feature`, `priority:high`
   Returns `{"success": true, "status": "authorized"|"limited"|"denied"|"restricted"|"notDetermined"}` plus remediation copy when not granted. The LLM uses this proactively before any other tool, and recovers gracefully when other tools return `error_type: "authorization_denied"` mid-flow. Code sample in gap analysis §3.

6. **[feature] `list_contacts` — paged read-only listing**
   Labels: `feature`, `priority:high`
   First feature. `enumerateContactsWithFetchRequest` against `CNContactStore`, returning structured `[{id, given_name, family_name, organization}, ...]`. Pagination via `offset`/`limit` parameters, default 50/page. Cap at 200/call. Reference: gap analysis §4 ("List contacts (Contacts.framework)") and §2 row "List/enumerate" — 1696 contacts in 53 ms baseline.

7. **[feature] `get_contact` — fetch a single contact by identifier**
   Labels: `feature`, `priority:high`
   `unifiedContactWithIdentifier:keysToFetch:error:` with the full P1 key set: name parts, organization, phones, emails, postal addresses, birthday, urls, job title, department, nickname. Returns full contact dict. Phone labels emitted as both raw token and translated string (see issue #15 for the bidirectional table — for v0.1.0 just translate on output via `CNLabeledValue.localizedStringForLabel:`). Reference: gap analysis §2 ("Phone label localization").

8. **[feature] `search_contacts` — predicate by name**
   Labels: `feature`, `priority:high`
   `predicateForContactsMatchingName:`. Single string param `query`. Same return shape as `list_contacts`. Cap at 200 results. Reference: gap analysis §4 ("Search by name (predicate)") and §2 ("Predicate by name"). Phone/email/organization variants are v0.2.0 (issue #11).

9. **[feature] `create_contact` — write via `CNSaveRequest`**
   Labels: `feature`, `priority:high`, `security`
   `CNMutableContact` + `CNSaveRequest.addContact:toContainerWithIdentifier:`. Default to `defaultContainerIdentifier`. Accept all P1 fields. Returns the new contact's identifier. **Must apply** the full security checklist (sanitize_input, validation, rate limit, audit log, TCC check, test-mode safety). Gap analysis §4 ("Create + save contact"); BOOTSTRAP §5 security checklist; gap analysis §6 question 6 (auth revocation mid-process).

10. **[feature] `update_contact` and `delete_contact`**
    Labels: `feature`, `priority:high`, `security`
    `updateContact:` and `deleteContact:` on `CNSaveRequest`. Same security checklist as #9. Update accepts a partial dict — only-modified-keys semantics. Delete requires confirmation flow when implemented (v0.4.0 issue #24); for v0.1.0 it gates on `CONTACTS_TEST_MODE`.

### Quality

11. **[testing] Integration test rig for v0.1.0 tools**
    Labels: `testing`, `priority:high`
    Per `MCP_PLAYBOOK.md §3` and the integration-testing skill, every `_run_cn_*` call must have an integration test that hits a real `CNContactStore`. Set up `CONTACTS_TEST_GROUP=MCP-Test` fixture: creates the group on first run, contacts created in tests are added to it, teardown removes the group. Skip-by-default via `--run-integration` flag.

---

## Milestone: v0.2.0 — Filters, Groups, vCard, Notes (P2)

Maps to [gap analysis §5](docs/research/contacts-api-gap-analysis.md#5-priority-tiers-for-the-v010-roadmap), tier P2. Goal: round out reads with all common filter axes, group operations, vCard import/export, and the notes-via-AppleScript fallback that the framework can't provide.

### New Tools

12. **[feature] `search_contacts` — by phone, email, organization**
    Labels: `feature`
    Add three predicate variants on top of the v0.1.0 name predicate: `predicateForContactsMatchingPhoneNumber:`, `predicateForContactsMatchingEmailAddress:`, organization via custom `NSPredicate`. Single tool with mutually-exclusive params (`name=`, `phone=`, `email=`, `organization=`); validate exactly one is set. Reference: gap analysis §2 ("Predicate by phone/email").

13. **[feature] `list_groups` and `get_contacts_in_group`**
    Labels: `feature`
    `groupsMatchingPredicate:None:None:` returns `[{identifier, name, container_id}, ...]`. `get_contacts_in_group` uses `CNContact.predicateForContactsInGroupWithIdentifier:`. Reference: gap analysis §4 ("Group membership predicate") and §2.

14. **[feature] `add_contact_to_group` and `remove_contact_from_group`**
    Labels: `feature`, `security`
    `CNSaveRequest.addMember:toGroup:` and `removeMember:fromGroup:`. Full security checklist applies (issue #2). Reference: gap analysis §2 ("Group membership"). Gap analysis §6 question 8 notes that group ownership is per-container — surface a clear error if the contact and group live in different containers.

15. **[feature] `read_note` and `write_note` — AppleScript fallback**
    Labels: `feature`, `security`, `priority:high`
    `note` field is entitlement-gated in Contacts.framework; AppleScript is the only path. Wrap in `_run_applescript_*`. **Mandatory** input escaping via `escape_applescript_string` (utils.py — see issue #16). Gap analysis §4 ("Read note (AppleScript fallback)"), §6 question 1.

16. **[feature] `export_vcard` and `import_vcard`**
    Labels: `feature`
    `CNContactVCardSerialization.dataWithContacts:error:` for export (3.0 only — emit a clear note in the response that NOTE field is stripped per gap analysis §6 question 1). `contactsWithData:error:` for import; accepts both 3.0 and 4.0. Round-trip verified in gap analysis §4.

### Infrastructure

17. **[infra] AppleScript escape helpers in `utils.py`**
    Labels: `infrastructure`, `security`
    Implement `escape_applescript_string()` and `_run_applescript_text()` / `_run_applescript_json_via_asobjc()`. Patterns from `apple-mail-mcp/src/apple_mail_mcp/utils.py`. Required by issue #15. Add `scripts/check_applescript_safety.sh` adapted for the small AppleScript surface (note + dates only). Reference: gap analysis §7 (AppleScript escape patterns).

### Research

18. **[research] Phone label translation table — closing gap analysis open Q4**
    Labels: `research`
    Apple emits raw `_$!<Mobile>!$_` tokens; macOS provides `CNLabeledValue.localizedStringForLabel:` for output but no inverse for input. Build the bidirectional table: `home`/`work`/`mobile`/`fax home`/`fax work`/`pager`/`other` ↔ Apple's tokens. Gate landing of `update_contact` phone-label edits on this. Reference: gap analysis §6 question 4.

19. **[research] vCard 4.0 export decision — closing gap analysis open Q3**
    Labels: `research`
    Three options: (a) post-process Apple's 3.0 output to upgrade, (b) ship `vobject` or similar 3rd-party, (c) document 3.0-only as the limitation. Decide before issue #16 ships export. Reference: gap analysis §6 question 3.

---

## Milestone: v0.3.0 — Niche Fields, Photo, Containers, Group CRUD (P3)

Maps to [gap analysis §5](docs/research/contacts-api-gap-analysis.md#5-priority-tiers-for-the-v010-roadmap), tier P3.

### New Tools

20. **[feature] Group CRUD — `create_group`, `rename_group`, `delete_group`**
    Labels: `feature`, `security`
    `CNMutableGroup` + `CNSaveRequest.addGroup:toContainerWithIdentifier:`. Full security checklist. `delete_group` requires confirmation (v0.4.0 issue #24). Reference: gap analysis §2 ("Group CRUD").

21. **[feature] Photo read/write with format detection**
    Labels: `feature`
    `imageData()` + `imageDataAvailable()` for read; `setImageData:` for write. On read, magic-byte detection (JPEG/PNG/HEIC) and report format in the response. On write, accept JPEG/PNG bytes (HEIC support depends on PyObjC bindings — verify). Reference: gap analysis §6 question 5.

22. **[feature] Container-aware tools — `list_containers`, `create_contact_in_container`**
    Labels: `feature`
    `containersMatchingPredicate:None:None:` returns `[{identifier, name, type}, ...]` where `type` is one of `local`/`exchange`/`cardDAV`. `create_contact_in_container` accepts an explicit container identifier on top of the v0.1.0 `create_contact`. Reference: gap analysis §6 question 8 and §2 ("Containers (multi-account)").

23. **[feature] Niche fields — custom dates, social profiles, related names, IM**
    Labels: `feature`
    Add to `get_contact`/`update_contact` as opt-in fields (`include_niche=True`). Most contacts won't have these populated; default off keeps responses small. Reference: gap analysis §1 ("Read/write social profiles, instant messages, related names, custom dates").

### Performance

24. **[perf] Performance baselines + `contacts-performance` skill**
    Labels: `performance`, `documentation`
    Per BOOTSTRAP §4.2, write the contacts-specific performance skill empirically — gap analysis §2 has the headline 53 ms for 1696 contacts but per-tool baselines (single read, predicate fetch, save, vCard export) are still untaken. Add benchmark tests under `tests/benchmarks/` with `--capture-baseline` flag.

### Research

25. **[research] Multi-container write round-trip — closing gap analysis open Q8a**
    Labels: `research`
    Verify `CNSaveRequest.addContact:toContainerWithIdentifier:` against the Gmail container and confirm the contact lands there (not in iCloud). Re-probe with a populated Google Contacts to see whether CardDAV groups surface (Q8b). Exchange and `On My Mac`/Local containers (Q8c) still untested.

---

## Milestone: v0.4.0 — Infrastructure Hardening

Cross-cutting concerns and open empirical questions that should not block v0.1.0 functionality but must land before any kind of "1.0" claim.

### Infrastructure

26. **[infra] Coverage gate to 90% with real tests (replace smoke test)**
    Labels: `testing`, `priority:high`
    `tests/unit/test_smoke.py` carries the 94.74% bootstrap coverage on stub modules. Once real implementations land in v0.1.0–v0.3.0, coverage will drop. Re-establish 90%+ on real code paths.

27. **[infra] `scripts/check_pyobjc_safety.sh`**
    Labels: `infrastructure`, `security`
    Static check for unsafe PyObjC patterns: passing user input into KVC keys, missing `descriptorForRequiredKeys` calls before vCard export, missing `imageDataAvailable()` guards before `imageData()` reads, missing TCC pre-check, missing test-mode-safety on writes. Wire into `make audit` and `release-hygiene.yml`. Reference: gap analysis §7.

28. **[infra] Branch protection on `main`**
    Labels: `infrastructure`
    Require `Tests / unit-tests` CI green, no direct pushes, require PR. Documented as a manual step in BOOTSTRAP §1.1; do this once via `gh api` and capture the config.

29. **[infra] `modificationDate` / `creationDate` macOS-version durability test — closing gap analysis open Q2**
    Labels: `testing`, `research`
    Both fields are accessible on `CNContact` only via undocumented runtime selectors. Add a regression test that hits both selectors and fails loudly if a future macOS removes them. If they break, the AppleScript fallback path activates.

30. **[infra] Packaging: `Info.plist` with `NSContactsUsageDescription` for Claude Desktop bundle**
    Labels: `infrastructure`
    For now we run unbundled (`uv run python -m apple_contacts_mcp.server`) and TCC prompts via the launching process's identity. Bundling for distribution requires the Info.plist key. Reference: gap analysis §3 ("Bundling note").

### Security

31. **[security] Real rate limiting (replace stub)**
    Labels: `security`, `enhancement`
    Pattern from `apple-mail-mcp/src/apple_mail_mcp/security.py`: token-bucket per-tool. Bootstrap stub `sanitize_input` already exists; rate limiter does not. Required before any production use.

32. **[security] Confirmation UX for destructive operations**
    Labels: `security`, `enhancement`
    `delete_contact`, `delete_group`, batch update — all need an out-of-band confirmation step. FastMCP elicitation pattern likely; verify against the apple-mail-mcp `templates.py` precedent.

33. **[security] Authorization revocation mid-process — closing gap analysis open Q6**
    Labels: `security`, `priority:high`
    If the user revokes Contacts permission while the server is running, the next call returns empty/error silently in some cases. Re-check `authorizationStatusForEntityType:` at the entry of every tool, not just at start-up. Add an integration test that revokes during a session.
