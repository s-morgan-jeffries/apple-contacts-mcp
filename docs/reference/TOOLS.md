# Tools Documentation

Reference for every MCP tool the apple-contacts-mcp server exposes.

**Version:** v0.1.0 (tracks the package version)
**Tools:** 7

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
def get_contact(identifier: str) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `identifier` | str | — | The contact's CN identifier (UUID-shaped string from `list_contacts` or `search_contacts`). Required, non-empty. |

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

Find contacts whose name matches `query` (substring, case-insensitive).

```python
def search_contacts(query: str) -> dict[str, Any]
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `query` | str | — | Substring to match. Must be a non-empty string (the empty string would silently return all contacts in CN's predicate; we reject it). |

**Returns:**

```jsonc
{
  "success": true,
  "contacts": [
    {"id": "ABCD-…", "given_name": "John", "family_name": "Smith", "organization": "Acme"}
  ],
  "count": 28,
  "query": "john",
  "limit": 200
}
```

**Error types:** `validation_error`, `authorization_denied`, `unknown`.

**Notes:**
- Matches given/family/organization names via Apple's built-in `predicateForContactsMatchingName:` — substring, case-insensitive.
- Hard cap at **200 results**. `count == limit` indicates the cap was hit and there may be more matches; narrow the query.
- Returns the same 4-field shape as `list_contacts`. Use `get_contact(id)` to fetch full details for a specific result.
- Order is not guaranteed.
- Phone / email / organization predicate variants land in v0.2.0 (issue #12 in INITIAL_ISSUES.md).

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
    group_identifier: str | None = None,
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
| `phones` | list[dict] \| None | None | List of `{"label_raw": str, "value": str}`. |
| `emails` | list[dict] \| None | None | List of `{"label_raw": str, "value": str}`. `value` must contain `@`. |
| `urls` | list[dict] \| None | None | List of `{"label_raw": str, "value": str}`. |
| `postal_addresses` | list[dict] \| None | None | List of dicts with `label_raw` + 8 postal sub-fields (`street`, `sub_locality`, `city`, `sub_administrative_area`, `state`, `postal_code`, `country`, `iso_country_code`). At least one geographic field must be non-empty. |
| `birthday` | dict \| None | None | `{"year": int, "month": int, "day": int}` (any subset). Month 1-12, day 1-31. |
| `group_identifier` | str \| None | None | If set, adds the new contact to this group atomically. **Required when `CONTACTS_TEST_MODE=true`** (must equal `CONTACTS_TEST_GROUP`). |

At least one of `given_name`, `family_name`, or `organization` must be non-empty (after `.strip()`).

**Returns:**

```jsonc
// Without group
{"success": true, "identifier": "ABCD-…"}

// With group
{"success": true, "identifier": "ABCD-…", "group_id": "GROUP-XYZ"}
```

**Error types:** `validation_error`, `authorization_denied`, `safety_violation`, `not_found`, `unknown`.

`not_found` here means `group_identifier` was supplied but didn't match any group.

**Notes:**
- New contact lands in the `defaultContainerIdentifier` (typically iCloud).
- Labeled-value `label_raw` is passed to CN as-is. Use Apple tokens like `_$!<Mobile>!$_` for built-in labels (round-trippable from `get_contact`'s `label_raw` field) or any custom string. The reverse mapping (`"mobile"` → `_$!<Mobile>!$_`) is v0.2.0 (#18); for now, prefer raw tokens.
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
- `group_identifier` is consumed only by the test-mode safety gate — the underlying connector ignores it. Group membership changes go through future `add_member_to_group` / `remove_member_from_group` tools (v0.2.0+).

---

### delete_contact

Delete an existing contact by identifier.

```python
def delete_contact(
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

**Error types:** `validation_error`, `safety_violation`, `authorization_denied`, `not_found`, `unknown`.

**Notes:**
- **v0.1.0 only allows delete in test mode.** Outside `CONTACTS_TEST_MODE=true` this returns `error_type: "safety_violation"` — the full destructive UX (with confirmation prompts) ships in v0.4.0 (#24).
- The `require_test_mode_for` gate fires **before** the auth check, so users outside test mode never see a TCC prompt for a call that's about to be refused.
- In test mode, the existing test-group gate also enforces that `group_identifier` matches `CONTACTS_TEST_GROUP`.

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
| `safety_violation` | The destructive-op gate refused: either `CONTACTS_TEST_MODE=true` was set without a matching `group_identifier`, or `delete_contact` was called outside test mode. | — |
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
