# Tools Documentation

Reference for every MCP tool the apple-contacts-mcp server exposes.

**Version:** v0.3.0 (tracks the package version)
**Tools:** 21

The source-of-truth for tool behavior is the docstrings in
[src/apple_contacts_mcp/server.py](../../src/apple_contacts_mcp/server.py)
— those are what FastMCP exposes to the LLM. This document is the
human-readable reference for developers integrating with the server,
security reviewers, and contributors.

## Format

Each tool entry has:

- **One-line summary** matching the docstring's first line.
- **Signature** — Python type hints.
- **Parameters** — table (name, type, default, description).
- **Returns** — JSON examples for the success path plus every named
  error variant.
- **Error types** — bullet list of every `error_type` value the tool
  can emit. Common types are explained once in the [Error types
  appendix](#error-types) at the bottom of this file.
- **Notes** — caveats, edge cases, and semantic gotchas.

When you add a new `@mcp.tool()`, mirror this layout. Group tools
under the version they shipped in (`## Phase 1 Tools (v0.1.0)`,
`## Phase 2 Tools (v0.2.0)`, …).

**Response-shape convention.** Optional id-echo keys (`group_id`, etc.) in
success responses are **always present**, set to `null` when the
corresponding input was not supplied. Callers should detect "was a group
assigned?" via `response["group_id"] is not None`, not via key presence.

---

## Phase 1 Tools (v0.1.0)

### check_authorization

Report current TCC authorization status for Contacts access.

```python
def check_authorization() -> dict[str, Any]
```

**Parameters:** none.

**Returns** — always `success: True` because this is a status query, not
an action. The `status` field tells you what to do.

```jsonc
// Granted (proceed with any tool)
{"success": true, "status": "authorized"}

// Granted with limits (macOS 14+; some contacts may be hidden)
{"success": true, "status": "limited"}

// Not granted yet — system prompt has not fired
{
  "success": true,
  "status": "notDetermined",
  "remediation": "Contacts access has not been requested yet. Run a data tool (e.g. list_contacts) to trigger the system permission prompt, or grant access manually in System Settings → Privacy & Security → Contacts."
}

// User explicitly denied
{
  "success": true,
  "status": "denied",
  "remediation": "Contacts access was denied. Open System Settings → Privacy & Security → Contacts and enable access for this server (macOS will not re-prompt automatically)."
}

// Locked by parental controls / MDM
{
  "success": true,
  "status": "restricted",
  "remediation": "Contacts access is locked by parental controls or device management. Contact your administrator."
}

// Pure tool failure (PyObjC import broken, etc.)
{"success": false, "error": "Failed to read TCC status: …", "error_type": "unknown"}
```

**Error types:** `unknown`.

**Notes:**
- Status-query tools return `success: true` for *all* status values, including denied/restricted, because the **query** succeeded. Data-fetching tools (`list_contacts`, etc.) use `success: false` + `error_type: "authorization_denied"` when TCC blocks their actual work.
- Does **not** trigger the system permission prompt. Call `list_contacts` (or any data tool) for that.

---

### list_contacts

Page through contacts; each entry has id, given_name, family_name, organization.

```python
def list_contacts(offset: int = 0, limit: int = 50) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `offset` | int | 0 | Number of contacts to skip from the start. Must be >= 0. |
| `limit` | int | 50 | Max contacts to return. Capped at 200. |

**Returns:**

```jsonc
{
  "success": true,
  "contacts": [
    {"id": "ABCD-…", "given_name": "Alice", "family_name": "Adams", "organization": "Acme"}
  ],
  "count": 1,
  "offset": 0,
  "limit": 50
}
```

Error responses follow the [common error envelope](#error-types):
`validation_error`, `authorization_denied`, `unknown`.

**Error types:** `validation_error`, `authorization_denied`, `unknown`.

**Notes:**
- Order is **not guaranteed** — Apple's enumeration order is implementation-defined. Use `search_contacts` to find a specific contact by name.
- The first call to this tool may trigger the system TCC prompt (if status was `notDetermined`).
- Does not include full P1 fields. Use `get_contact(id)` for the full record.

---

### get_contact

Fetch a single contact by its CN identifier with all P1 fields.

```python
def get_contact(
    identifier: str, include_niche: bool = False
) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `identifier` | str | — | The contact's CN identifier (UUID-shaped string from `list_contacts` or `search_contacts`). Required, non-empty. |
| `include_niche` | bool | False | When True, also fetches the P3 niche families (`dates`, `social_profiles`, `relations`, `instant_messages`). Off by default — most contacts won't have these populated and including them grows responses. |

**Returns:**

```jsonc
{
  "success": true,
  "contact": {
    "id": "ABCD-…",
    "given_name": "Alice", "family_name": "Adams", "middle_name": "",
    "name_prefix": "", "name_suffix": "", "nickname": "",
    "organization": "Acme", "job_title": "", "department": "",
    "phones": [
      {"label_raw": "_$!<Mobile>!$_", "label": "mobile", "value": "+1 555-1212"}
    ],
    "emails": [
      {"label_raw": "_$!<Home>!$_", "label": "home", "value": "alice@example.com"}
    ],
    "urls": [
      {"label_raw": "_$!<HomePage>!$_", "label": "homepage", "value": "https://example.com"}
    ],
    "postal_addresses": [
      {
        "label_raw": "_$!<Home>!$_", "label": "home",
        "street": "1 Loop", "sub_locality": "", "city": "Cupertino",
        "sub_administrative_area": "", "state": "CA",
        "postal_code": "95014", "country": "USA", "iso_country_code": "us"
      }
    ],
    "birthday": {"year": 1990, "month": 5, "day": 15}
  }
}
```

With `include_niche=True`, the contact dict also contains four additional labeled-value families (always present when the flag is set, possibly as empty lists):

```jsonc
{
  "dates": [
    {"label_raw": "_$!<Anniversary>!$_", "label": "anniversary",
     "year": 2010, "month": 6, "day": 1}
  ],
  "social_profiles": [
    {"label_raw": "_$!<Twitter>!$_", "label": "Twitter",
     "service": "Twitter", "username": "alice",
     "url": "https://t.example/alice", "user_identifier": ""}
  ],
  "relations": [
    {"label_raw": "_$!<Spouse>!$_", "label": "spouse", "name": "Bob"}
  ],
  "instant_messages": [
    {"label_raw": "_$!<Slack>!$_", "label": "Slack",
     "service": "Slack", "username": "alice"}
  ]
}
```

When `include_niche=False` (the default), these four keys are **absent** from the response — not `null`. Callers detect presence via `"dates" in contact`.

When the identifier doesn't resolve:

```jsonc
{
  "success": false,
  "error": "No contact found with identifier 'ZZZZ'",
  "error_type": "not_found"
}
```

**Error types:** `validation_error`, `not_found`, `authorization_denied`, `unknown`.

**Notes:**
- All single-valued string fields are always present, possibly `""`.
- All four labeled-value families (`phones`, `emails`, `urls`, `postal_addresses`) are always present, possibly `[]`.
- Each labeled-value entry carries **both** `label_raw` (the Apple token like `_$!<Mobile>!$_`) and `label` (the human-readable string from `CNLabeledValue.localizedStringForLabel:`). Custom labels round-trip as themselves on both keys.
- `birthday` is `null` when the contact has no birthday set; otherwise a dict with whichever of `year` / `month` / `day` are defined. Apple lets users set a birthday without a year — in that case the dict is `{"month": 5, "day": 15}` with no `year` key.

---

### search_contacts

Find contacts by name, phone, email, or organization (pick one).

```python
def search_contacts(
    name: str = "",
    phone: str = "",
    email: str = "",
    organization: str = "",
) -> dict[str, Any]
```

**Parameters:** exactly one must be set (non-empty after stripping); whitespace-only counts as unset.

| Name | Type | Default | Description |
|---|---|---|---|
| `name` | str | `""` | Substring to match against given/family/organization names. |
| `phone` | str | `""` | Phone number to match (any format). |
| `email` | str | `""` | Email address to match. |
| `organization` | str | `""` | Substring to match against organization name. |

**Returns:**

```jsonc
{
  "success": true,
  "contacts": [
    {"id": "ABCD-…", "given_name": "John", "family_name": "Smith", "organization": "Acme"}
  ],
  "count": 28,
  "search_field": "name",
  "search_value": "john",
  "limit": 200
}
```

**Error types:** `validation_error`, `authorization_denied`, `unknown`.

**Notes:**
- Setting zero or multiple fields returns `validation_error`. The error message lists the fields involved.
- `search_value` echoes the **stripped** value the predicate actually saw.
- Match semantics per field:
  - `name`: substring + case-insensitive across given/family/organization names (Apple's `predicateForContactsMatchingName:`).
  - `phone`: format-tolerant via Apple's `predicateForContactsMatchingPhoneNumber:` — punctuation, spacing, and country-code variants normalize, so `(555) 123-4567` and `+15551234567` match the same contact.
  - `email`: Apple's `predicateForContactsMatchingEmailAddress:`.
  - `organization`: substring, case- and diacritic-insensitive (custom `NSPredicate` with `CONTAINS[cd]`), since Apple ships no built-in organization predicate. Mirrors name-mode behavior.
- Hard cap at **200 results**. `count == limit` indicates the cap was hit and there may be more matches; narrow the query.
- Returns the same 4-field shape as `list_contacts`. Use `get_contact(id)` to fetch full details for a specific result.
- Order is not guaranteed.

---

### create_contact

Create a new contact in the user's default container.

```python
def create_contact(
    given_name: str = "",
    family_name: str = "",
    middle_name: str = "",
    name_prefix: str = "",
    name_suffix: str = "",
    nickname: str = "",
    organization: str = "",
    job_title: str = "",
    department: str = "",
    phones: list[dict[str, str]] | None = None,
    emails: list[dict[str, str]] | None = None,
    urls: list[dict[str, str]] | None = None,
    postal_addresses: list[dict[str, str]] | None = None,
    birthday: dict[str, int] | None = None,
    dates: list[dict[str, Any]] | None = None,
    social_profiles: list[dict[str, str]] | None = None,
    relations: list[dict[str, str]] | None = None,
    instant_messages: list[dict[str, str]] | None = None,
    group_identifier: str | None = None,
    container_identifier: str | None = None,
) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `given_name` | str | `""` | First name. |
| `family_name` | str | `""` | Last name. |
| `middle_name` | str | `""` | Middle name. |
| `name_prefix` | str | `""` | Title (Mr., Dr., …). |
| `name_suffix` | str | `""` | Suffix (Jr., III, …). |
| `nickname` | str | `""` | Nickname. |
| `organization` | str | `""` | Company / organization name. |
| `job_title` | str | `""` | Job title. |
| `department` | str | `""` | Department. |
| `phones` | list[dict] \| None | None | List of `{"label": str, "value": str}`. |
| `emails` | list[dict] \| None | None | List of `{"label": str, "value": str}`. `value` must contain `@`. |
| `urls` | list[dict] \| None | None | List of `{"label": str, "value": str}`. |
| `postal_addresses` | list[dict] \| None | None | List of dicts with `label` + 8 postal sub-fields (`street`, `sub_locality`, `city`, `sub_administrative_area`, `state`, `postal_code`, `country`, `iso_country_code`). At least one geographic field must be non-empty. |
| `birthday` | dict \| None | None | `{"year": int, "month": int, "day": int}` (any subset). Month 1-12, day 1-31. |
| `dates` | list[dict] \| None | None | Custom labeled dates (e.g., anniversaries). List of `{"label": str, "year"?: int, "month"?: int, "day"?: int}` dicts (any subset; at least one of year/month/day must be set per entry). |
| `social_profiles` | list[dict] \| None | None | List of `{"label": str, "service": str, "username": str, "url": str, "user_identifier": str}` dicts. At least one of `username`/`url` non-empty per entry. |
| `relations` | list[dict] \| None | None | Contact relations (spouse/child/etc.). List of `{"label": str, "name": str}` dicts. `name` non-empty per entry. |
| `instant_messages` | list[dict] \| None | None | List of `{"label": str, "service": str, "username": str}` dicts. `username` non-empty per entry. |
| `group_identifier` | str \| None | None | If set, adds the new contact to this group atomically. **Required when `CONTACTS_TEST_MODE=true`** (must equal `CONTACTS_TEST_GROUP`). |
| `container_identifier` | str \| None | None | If set, writes to this container instead of the user's default (typically iCloud). Use `list_containers` to find UUIDs. CN raises if the identifier is unknown — surfaces as `unknown`. |

At least one of `given_name`, `family_name`, or `organization` must be non-empty (after `.strip()`).

**Returns:**

```jsonc
// Without group, default container
{"success": true, "identifier": "ABCD-…", "group_id": null, "container_id": null}

// With group + non-default container
{
  "success": true,
  "identifier": "ABCD-…",
  "group_id": "GROUP-XYZ",
  "container_id": "WXYZ-…:ABAccount"
}
```

Both id-echo keys follow the [response-shape convention](#format) — always present, `null` when input was absent.

**Error types:** `validation_error`, `authorization_denied`, `safety_violation`, `not_found`, `unknown`.

`not_found` here means `group_identifier` was supplied but didn't match any group. Unknown `container_identifier` surfaces as `unknown` (CN's save error propagates).

**Notes:**
- New contact lands in the `defaultContainerIdentifier` (typically iCloud).
- Labeled-value `label` accepts three forms (case-insensitive):
  1. **Human form** like `"mobile"`, `"work"`, `"home fax"`, `"iPhone"`, `"homepage"` — translated to Apple's built-in token via the table in [`utils.label_to_apple_token`](../../src/apple_contacts_mcp/utils.py).
  2. **Apple token** like `"_$!<Mobile>!$_"` — passed through unchanged.
  3. **Custom string** like `"Spotify"` — passed through unchanged; Apple stores it as a custom label.
  English forms only on input; non-English human forms are treated as custom labels. See [`docs/research/label-translation-decision.md`](../research/label-translation-decision.md) for the full table and rationale.
- The new contact's identifier is populated by CN at save time and returned in the response.
- In test mode (`CONTACTS_TEST_MODE=true`), this tool refuses unless `group_identifier` matches `CONTACTS_TEST_GROUP` — protects the real address book during integration testing.

---

### update_contact

Update an existing contact by identifier with partial-field semantics.

```python
def update_contact(
    identifier: str,
    given_name: str | None = None,
    family_name: str | None = None,
    middle_name: str | None = None,
    name_prefix: str | None = None,
    name_suffix: str | None = None,
    nickname: str | None = None,
    organization: str | None = None,
    job_title: str | None = None,
    department: str | None = None,
    phones: list[dict[str, str]] | None = None,
    emails: list[dict[str, str]] | None = None,
    urls: list[dict[str, str]] | None = None,
    postal_addresses: list[dict[str, str]] | None = None,
    birthday: dict[str, int] | None = None,
    dates: list[dict[str, Any]] | None = None,
    social_profiles: list[dict[str, str]] | None = None,
    relations: list[dict[str, str]] | None = None,
    instant_messages: list[dict[str, str]] | None = None,
    group_identifier: str | None = None,
) -> dict[str, Any]
```

**Parameters:** every field defaults to `None` (asymmetric with `create_contact`'s `""` defaults — see Notes).

| Caller passes | Behavior |
|---|---|
| `given_name=None` (default) | Don't touch. |
| `given_name=""` | Explicitly clear. |
| `given_name="Alice"` | Set to "Alice". |
| `phones=None` | Don't touch the existing phones list. |
| `phones=[]` | Replace with empty list (clear all). |
| `phones=[{...}]` | **Replace** all phones (REST-PUT semantics, not append). |
| `birthday=None` | Don't touch. |
| `birthday={"month": 5}` | Replace components with the supplied subset. |
| `dates=None` / `social_profiles=None` / `relations=None` / `instant_messages=None` | Don't touch. |
| `dates=[]` (or any niche field `=[]`) | Replace with empty list (clear all). |
| `dates=[{...}]` / etc. | **Replace** all entries (REST-PUT semantics). Per-entry shape matches `create_contact`. |

`identifier` and `group_identifier` follow the same semantics as in `create_contact`. At least one mutating field must be supplied (besides `identifier` and `group_identifier`).

**Returns:**

```jsonc
{"success": true, "identifier": "ABCD-…"}
```

Use `get_contact(identifier)` to read back the updated record.

**Error types:** `validation_error`, `authorization_denied`, `safety_violation`, `not_found`, `unknown`.

`not_found` here means the `identifier` doesn't match any contact.

**Notes:**
- The `None`-vs-`""` asymmetry with `create_contact` is intentional: update needs to distinguish "not supplied" from "explicitly clear", whereas create has nothing to overwrite.
- **Clearing birthday entirely is not supported in v0.1.0.** Use Apple's Contacts.app to clear it, or pass a `birthday=` value to overwrite the components. A clear-via-sentinel API may land in v0.2.0+.
- `group_identifier` is consumed only by the test-mode safety gate — the underlying connector ignores it. Group membership changes go through `add_contact_to_group` / `remove_contact_from_group`.
- Labeled-value `label` follows the same three-form contract as `create_contact`: human form (`"mobile"`, `"home fax"`, …), Apple token (`"_$!<Mobile>!$_"`), or custom string. See `create_contact` notes and [`docs/research/label-translation-decision.md`](../research/label-translation-decision.md).

---

### delete_contact

Delete an existing contact by identifier.

```python
async def delete_contact(
    identifier: str,
    group_identifier: str | None = None,
) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `identifier` | str | — | The contact's CN identifier. |
| `group_identifier` | str \| None | None | Test-mode safety assertion. Required in test mode. No other use. |

**Returns:**

```jsonc
{"success": true, "identifier": "ABCD-…"}
```

**Error types:** `validation_error`, `safety_violation`, `user_declined`, `authorization_denied`, `not_found`, `unknown`.

**Notes:**
- **Outside test mode, the tool requires explicit user confirmation via FastMCP elicitation.** The client renders `Delete contact 'Alice Adams' (ABCD-…)? This cannot be undone.` with **Yes, delete** / **No, cancel** buttons. The contact's name is pre-fetched so the prompt isn't an opaque UUID.
  - **Yes** → delete proceeds.
  - **No / Declined / Cancelled** → `user_declined`.
  - **Client doesn't support elicitation** → `safety_violation`, pointing at `CONTACTS_TEST_MODE` as the bypass.
  - **Identifier doesn't match any contact** → `not_found` *before* prompting (no point asking the user about a non-existent record).
- **In test mode** (`CONTACTS_TEST_MODE=true`), confirmation is skipped and the existing test-group safety gate applies: `group_identifier` must match `CONTACTS_TEST_GROUP`. This preserves the existing harness path.
- Auth is checked before the test-mode branch, so users outside test mode in an unauthorized state still see the standard `authorization_denied` first.

---

## Phase 2 Tools (v0.2.0)

### read_note

Read a contact's note via AppleScript.

```python
def read_note(identifier: str) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `identifier` | str | — | Full CN identifier (e.g. `ABCD-1234-…:ABPerson`). Must be the suffixed form returned by other tools — bare UUIDs do not match. |

**Returns:**

```jsonc
{
  "success": true,
  "identifier": "ABCD-1234-…:ABPerson",
  "note": "free-form text the user wrote\non multiple lines"
}
```

`note == ""` indicates the contact exists but has no note set.

**Error types:** `validation_error`, `authorization_denied`, `not_found`, `unknown`.

**Notes:**
- **Backed by AppleScript via `osascript`, not Contacts.framework.** The `note` field requires the `com.apple.developer.contacts.notes` entitlement that Apple grants only to App Store apps; we're unbundled, so we route through Contacts.app's AppleScript scripting bridge instead.
- Pass the identifier verbatim. AppleScript's `id of person` includes the `:ABPerson` suffix — stripping it produces an `Invalid index` error that this tool maps to `not_found`.
- TCC: gated by `_require_contacts_authorization()` (Contacts privacy permission). On macOS, AppleScript→Contacts.app additionally needs Automation permission, which the OS prompts for on the first call.

---

### write_note

Write a contact's note via AppleScript. `note=""` clears the note.

```python
def write_note(
    identifier: str,
    note: str,
    group_identifier: str | None = None,
) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `identifier` | str | — | Full CN identifier (must include `:ABPerson` suffix; see `read_note`). |
| `note` | str | — | New note text. Empty string clears the note. |
| `group_identifier` | str \| None | None | Required in test mode for the safety gate; ignored otherwise. |

**Returns:**

```jsonc
{"success": true, "identifier": "ABCD-1234-…:ABPerson"}
```

**Error types:** `validation_error`, `safety_violation`, `authorization_denied`, `not_found`, `unknown`.

**Notes:**
- **Destructive: replaces the note in full** (no append/diff semantics). Use `read_note` first if you need to preserve existing content.
- Same AppleScript-routing caveats as `read_note`. The connector also issues a `save` after the write — without it, edits sit only in Contacts.app's in-memory state.
- Test-mode gate is the same shape as `update_contact` (`check_test_mode_safety`, *not* `require_test_mode_for`): outside test mode the call runs freely; in test mode the contact must belong to `CONTACTS_TEST_GROUP`.

---

### list_groups

Enumerate all contact groups across all containers.

```python
def list_groups() -> dict[str, Any]
```

**Parameters:** none.

**Returns:**

```jsonc
{
  "success": true,
  "groups": [
    {"id": "ABCD-…:ABGroup", "name": "Family", "container_id": "iCloud-…"}
  ],
  "count": 7,
  "limit": 200
}
```

**Error types:** `authorization_denied`, `unknown`.

**Notes:**
- Hard cap at **200 groups**. `count == limit` indicates the cap was hit; almost no real address book gets close.
- Order is not guaranteed (matches Apple's native `groupsMatchingPredicate:` enumeration).
- `container_id` resolves the parent `CNContainer` per group (one extra Contacts.framework call per group). Empty string is returned defensively when Apple's container lookup yields no result for a live group — shouldn't happen in practice.

---

### get_contacts_in_group

List contacts whose membership includes the given group.

```python
def get_contacts_in_group(identifier: str) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `identifier` | str | — | The group's CN identifier (the `id` field returned by `list_groups`). |

**Returns:**

```jsonc
{
  "success": true,
  "group_identifier": "ABCD-…:ABGroup",
  "contacts": [
    {"id": "WXYZ-…:ABPerson", "given_name": "Alice", "family_name": "Anderson", "organization": "Acme"}
  ],
  "count": 12,
  "limit": 200
}
```

**Error types:** `validation_error`, `authorization_denied`, `not_found`, `unknown`.

**Notes:**
- Returns the same 4-field shape as `list_contacts` / `search_contacts`. Use `get_contact(id)` to fetch full details for a result.
- Hard cap at **200 contacts**. `count == limit` indicates the cap was hit.
- **Pre-flights existence** via `_run_cn_fetch_group`: an unknown `identifier` returns `not_found` distinctly from a real-but-empty group. Costs one extra Contacts.framework call (~ms).

---

### add_contact_to_group

Add an existing contact to an existing group.

```python
def add_contact_to_group(
    contact_identifier: str,
    group_identifier: str,
) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `contact_identifier` | str | — | The contact's CN identifier (the suffixed `<UUID>:ABPerson` form). |
| `group_identifier` | str | — | The group's CN identifier (the `id` field from `list_groups`). Must match `CONTACTS_TEST_GROUP` when `CONTACTS_TEST_MODE=true`. |

**Returns:**

```jsonc
{
  "success": true,
  "contact_identifier": "ABCD-…:ABPerson",
  "group_identifier": "WXYZ-…:ABGroup"
}
```

**Error types:** `validation_error`, `safety_violation`, `authorization_denied`, `not_found`, `unknown`.

**Notes:**
- **Destructive (test-mode gated):** the contact's group memberships are mutated in place. The same contact may belong to multiple groups; this tool adds membership without disturbing existing memberships.
- `not_found` distinguishes between a missing contact and a missing group via the `error` text (`"Contact not found: ..."` vs `"Group not found: ..."`).
- Cross-container pairs (e.g., a contact in iCloud added to a group in CardDAV) surface as `unknown` with Apple's NSError text preserved. The integration suite probes this empirically; a typed `container_mismatch` envelope may follow in a later release if the wording is stable.

---

### remove_contact_from_group

Remove an existing contact from an existing group.

```python
def remove_contact_from_group(
    contact_identifier: str,
    group_identifier: str,
) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `contact_identifier` | str | — | The contact's CN identifier. |
| `group_identifier` | str | — | The group's CN identifier. Must match `CONTACTS_TEST_GROUP` when `CONTACTS_TEST_MODE=true`. |

**Returns:**

```jsonc
{
  "success": true,
  "contact_identifier": "ABCD-…:ABPerson",
  "group_identifier": "WXYZ-…:ABGroup"
}
```

**Error types:** `validation_error`, `safety_violation`, `authorization_denied`, `not_found`, `unknown`.

**Notes:**
- **Destructive (test-mode gated):** removes the membership edge only. The contact and the group themselves are untouched.
- **AppleScript fallback:** Apple's `CNSaveRequest.removeMember:fromGroup:` silently no-ops despite reporting success, so the connector routes through `osascript` (`remove p from g` followed by `save`) instead. Empirically discovered during #18 and locked in by the integration test rig. Apple's add path works fine; only the remove path is asymmetric.
- Pre-flights existence via the same fetch helper used by `add_contact_to_group`, so `not_found` is dispatched before the AppleScript runs.

---

### export_vcard

Export one or more contacts as a single vCard 3.0 payload.

```python
def export_vcard(identifiers: list[str]) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `identifiers` | list[str] | — | Non-empty list of contact CN identifiers (the suffixed `<UUID>:ABPerson` form). Single-contact callers pass `[id]`. |

**Returns:**

```jsonc
{
  "success": true,
  "vcard": "BEGIN:VCARD\nVERSION:3.0\nPRODID:-//Apple Inc.//macOS …\nN:Smith;John;;;\nFN:John Smith\nTEL;type=CELL;type=VOICE;type=pref:+15551234567\nEND:VCARD\n",
  "count": 1,
  "notes": [
    "NOTE field is omitted (entitlement-gated). Use read_note() and merge separately if needed.",
    "Year-less birthdays use Apple's X-APPLE-OMIT-YEAR=1604 hack; non-Apple consumers see 1604 as the literal year."
  ]
}
```

**Error types:** `validation_error`, `authorization_denied`, `not_found`, `unknown`.

**Notes:**
- **vCard 3.0 verbatim.** Apple's `CNContactVCardSerialization` emits 3.0 only; we pass it through unchanged. See [`docs/research/vcard-version-decision.md`](../research/vcard-version-decision.md) for the rationale and the full enumeration of Apple's specific quirks.
- **Atomic.** The first missing identifier aborts the call before serialization runs — the response is `not_found` with the offending id named in the `error` text.
- **Limitations are echoed in the response `notes` list** (rather than only in docs) so callers see them at runtime. The two limitations to flag for users:
  1. **NOTE field omitted** (entitlement-gated; use [`read_note`](#read_note) and merge separately).
  2. **Year-less birthdays corrupt to "1604"** for non-Apple consumers (Apple↔Apple round-trip preserves the year-less semantic via the `X-APPLE-OMIT-YEAR` marker).
- No vCard 4.0 emit; vCard 4.0 input is accepted by [`import_vcard`](#import_vcard).

---

### import_vcard

Parse a vCard payload and persist as new contacts.

```python
def import_vcard(
    vcard_text: str,
    group_identifier: str | None = None,
) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `vcard_text` | str | — | The vCard text. Non-empty after stripping. May contain one or more `BEGIN:VCARD…END:VCARD` blocks. Both vCard 3.0 and 4.0 input are accepted. |
| `group_identifier` | str \| None | None | Optional. If provided, every imported contact is added to the group atomically. **Required** in test mode for the safety gate (must match `CONTACTS_TEST_GROUP`). |

**Returns:**

```jsonc
{
  "success": true,
  "identifiers": ["ABCD-…:ABPerson", "EFGH-…:ABPerson"],
  "count": 2,
  "group_id": "WXYZ-…:ABGroup"
}
```

`group_id` is `null` when no group was specified. `identifiers` is in input order.

**Error types:** `validation_error`, `safety_violation`, `authorization_denied`, `not_found`, `unknown`.

**Notes:**
- **Destructive (test-mode gated):** creates new contacts. Test-mode posture matches `create_contact` (gated to `CONTACTS_TEST_GROUP` when test mode is on; freely allowed outside test mode).
- **Atomic.** Parse failure, empty input, group-not-found, or save failure aborts the whole call. A multi-contact vCard is committed as one unit.
- **Malformed input dispatches `validation_error`** (not `unknown`) — Apple's parser is the authority on validity, but the caller is responsible for handing us well-formed text. The `error` string preserves Apple's parser message for debuggability.
- vCard 4.0 input is accepted (Apple parses both); after import, our internal representation is the unified Apple model regardless of input version.

---

## Phase 3 Tools (v0.3.0)

### list_containers

List all contact containers (accounts).

```python
def list_containers() -> dict[str, Any]
```

**Parameters:** none.

**Returns:**

```jsonc
{
  "success": true,
  "containers": [
    {"id": "F7F61738-…:ABAccount", "name": "iCloud",
     "type": "cardDAV", "is_default": true},
    {"id": "797C8A05-…:ABAccount", "name": "Gmail",
     "type": "cardDAV", "is_default": false}
  ],
  "count": 2,
  "limit": 10
}
```

| Field | Description |
|---|---|
| `id` | CN identifier (`<UUID>:ABAccount`). Pass to `create_contact(..., container_identifier=...)`. |
| `name` | User-visible account name. |
| `type` | One of `"local"` / `"exchange"` / `"cardDAV"`. Even iCloud reports as `cardDAV` (the sync protocol). `"local"` is the legacy "On My Mac" account. |
| `is_default` | `true` for the container new contacts go into when `container_identifier` is not specified. Exactly one container has this flag. |

**Error types:** `authorization_denied`, `unknown`.

**Notes:**
- Hard cap at 10 (containers per user are typically <5). `count == limit` indicates the cap was hit.
- Read-only; no test-mode gating.
- Empirical basis for the multi-container write path: [`docs/research/multi-container-write-decision.md`](../research/multi-container-write-decision.md).

### read_photo

Read a contact's photo. Returns base64-encoded bytes plus the detected image format.

```python
def read_photo(identifier: str) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `identifier` | str | — | The contact's CN identifier. |

**Returns:**

```jsonc
// Contact exists, photo set
{
  "success": true,
  "identifier": "ABCD-…",
  "image_data": "<base64-encoded bytes>",
  "format": "jpeg",
  "size_bytes": 12345
}

// Contact exists, no photo set — distinct from not_found
{
  "success": true,
  "identifier": "ABCD-…",
  "image_data": null,
  "format": null,
  "size_bytes": 0
}
```

**Error types:** `validation_error`, `authorization_denied`, `not_found`, `unknown`.

**Notes:**
- **Binary transport:** `image_data` is base64-encoded (`base64.b64encode(bytes).decode("ascii")`). Callers decode via `base64.b64decode(image_data)` to recover the raw bytes.
- **Format detection** runs magic-byte detection on the raw bytes; the result is one of `"jpeg"`, `"png"`, `"gif"`, `"heic"`, or `"unknown"`. The HEIC bucket covers the HEIF-family ISOBMFF brands Apple emits (heic, heix, heif, hevc, hevx, mif1, msf1).
- **The no-photo case is a SUCCESS**, not `not_found`. Callers must check `image_data is not None` to detect "has photo," not key presence.
- Per the gap analysis gotcha, the connector always checks `imageDataAvailable()` before calling `imageData()` — directly reading on a photo-less contact misbehaves empirically.

### write_photo

Set or clear a contact's photo.

```python
def write_photo(
    identifier: str,
    image_data: str | None,
    group_identifier: str | None = None,
) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `identifier` | str | — | The contact's CN identifier. |
| `image_data` | str \| None | — | Base64-encoded image bytes (JPEG/PNG/HEIC supported by Apple). `null` clears the existing photo. |
| `group_identifier` | str \| None | None | Test-mode safety assertion. Required in test mode (must match `CONTACTS_TEST_GROUP`); ignored otherwise. |

**Returns:**

```jsonc
{"success": true, "identifier": "ABCD-…"}
```

**Error types:** `validation_error`, `authorization_denied`, `safety_violation`, `not_found`, `unknown`.

**Notes:**
- **Destructive (test-mode gated).** Same posture as `update_contact`.
- **Binary transport:** caller supplies base64-encoded text via `base64.b64encode(bytes).decode("ascii")`. The tool decodes via `base64.b64decode(..., validate=True)` and surfaces decode errors as `validation_error`.
- **Permissive on format:** the tool does not pre-validate that the bytes are a recognized image format. Apple is the authority — if it rejects the bytes, `CNSaveRequest` fails and we surface `unknown`.
- **`image_data=null` clears the photo** — the standard way to remove a contact's existing image. The clear path is atomic just like a write.

### create_group

Create a new contact group.

```python
def create_group(
    name: str,
    container_identifier: str | None = None,
    group_identifier: str | None = None,
) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `name` | str | — | The new group's name. Non-empty after stripping. |
| `container_identifier` | str \| None | None | If set, target this container instead of the default. Use `list_containers` to discover UUIDs. CN raises on unknown identifiers; surfaces as `unknown`. |
| `group_identifier` | str \| None | None | Test-mode safety assertion. Required in test mode (must match `CONTACTS_TEST_GROUP`); ignored otherwise. |

**Returns:**

```jsonc
{
  "success": true,
  "group": {
    "id": "NEW-…:ABGroup",
    "name": "MyGroup",
    "container_id": "F7F61738-…:ABAccount"
  }
}
```

**Error types:** `validation_error`, `authorization_denied`, `safety_violation`, `unknown`.

**Notes:**
- **Destructive (test-mode gated).** In test mode, `group_identifier` must equal `CONTACTS_TEST_GROUP`. The assertion is "I'm operating within the test-group scope" — same posture as `create_contact`.
- New group lands in the default container (typically iCloud) unless `container_identifier` is supplied.

### rename_group

Rename an existing contact group.

```python
def rename_group(
    identifier: str,
    new_name: str,
    group_identifier: str | None = None,
) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `identifier` | str | — | The CN identifier of the group to rename. |
| `new_name` | str | — | The new name. Non-empty after stripping. |
| `group_identifier` | str \| None | None | Test-mode safety assertion. Required in test mode. |

**Returns:**

```jsonc
{
  "success": true,
  "group": {
    "id": "GRP-…:ABGroup",
    "name": "Updated Name",
    "container_id": "F7F61738-…:ABAccount"
  }
}
```

`id` echoes the input.

**Error types:** `validation_error`, `authorization_denied`, `safety_violation`, `not_found`, `unknown`.

**Notes:**
- **Destructive (test-mode gated).** Same posture as `update_contact`.
- The asserted scope (`group_identifier`) is independent of the target (`identifier`) — a test harness may rename any group as long as it operates within test-mode scope.

### delete_group

Delete an existing contact group.

```python
async def delete_group(
    identifier: str,
    group_identifier: str | None = None,
) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `identifier` | str | — | The CN identifier of the group to delete. |
| `group_identifier` | str \| None | None | Test-mode safety assertion. Required in test mode (no other use). |

**Returns:**

```jsonc
{"success": true, "identifier": "GRP-…:ABGroup"}
```

**Error types:** `validation_error`, `safety_violation`, `user_declined`, `authorization_denied`, `not_found`, `unknown`.

**Notes:**
- **Outside test mode, requires explicit user confirmation via FastMCP elicitation** — same UX and error shape as `delete_contact`. The group's name is pre-fetched and shown in the prompt. `Yes` proceeds; `No` / `Declined` / `Cancelled` returns `user_declined`; client-without-elicit returns `safety_violation`.
- **Member contacts are NOT deleted** — they remain in the address book; they just lose membership in the now-removed group.

---

## Error types

Common envelope:

```jsonc
{
  "success": false,
  "error": "<human-readable explanation>",
  "error_type": "<one of the values below>",
  // …optional context fields per error_type
}
```

| `error_type` | Meaning | Optional context fields |
|---|---|---|
| `validation_error` | Caller violated the input contract — bad type, bad shape, empty required field, out-of-range birthday, email without `@`, etc. | — |
| `authorization_denied` | TCC blocked the operation. The LLM should call `check_authorization` to disambiguate (`notDetermined` / `denied` / `restricted`) and surface `remediation` to the user. | `status`, `remediation` |
| `safety_violation` | The destructive-op gate refused: in test mode, the asserted `group_identifier` didn't match `CONTACTS_TEST_GROUP`; outside test mode for `delete_contact` / `delete_group`, the client doesn't support FastMCP elicitation (no way to confirm). | — |
| `user_declined` | The user explicitly declined or cancelled an elicitation confirmation prompt (`delete_contact`, `delete_group`). The destructive op was not performed. | — |
| `not_found` | A referenced CN object (contact identifier or `group_identifier`) doesn't exist in the unified store. | — |
| `unknown` | Anything else — usually a CN save failure or an unexpected PyObjC error. The `error` field has the underlying message. | — |

The `success: true` envelope is tool-specific (see each tool's Returns
section above).

---

## Versioning

This file's tool list and the `Tools:` count at the top track the
package version in [pyproject.toml](../../pyproject.toml). When a new
`@mcp.tool()` ships, add an entry under the appropriate `## Phase N
Tools (vX.Y.Z)` heading and bump the count.
