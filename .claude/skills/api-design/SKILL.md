---
name: api-design
description: Use BEFORE adding any new tool, parameter, or endpoint to the Apple Contacts MCP server. Also use when considering API changes, evaluating feature requests, or when tempted to create a specialized operation. Contains the decision tree that prevents tool sprawl and the anti-pattern catalog.
---

# Apple Contacts MCP API Design

The current API has 17 tools: the contact CRUD core (`list_contacts`, `get_contact`,
`search_contacts`, `create_contact`, `update_contact`, `delete_contact`), the
group/container surface (`list_groups`, `get_contacts_in_group`, `create_group`,
`rename_group`, `delete_group`, `add_contact_to_group`, `remove_contact_from_group`,
`list_containers`), vCard I/O (`export_vcard`, `import_vcard`), and `check_authorization`.
Tool count should grow slowly and deliberately. Every new tool request must pass through
the decision tree.

## Decision Tree: Use BEFORE Adding Any Tool

```
Can an existing tool handle this with current parameters?  (70% of cases: YES)
  |-- Searching with a known filter? -> use search_contacts(name/phone/email/organization)
  |-- Reading a contact? -> get_contact(identifier)
  |-- Editing a field? -> update_contact(identifier, <field>=...)
  |
Can an existing tool handle this with a NEW parameter?  (20% of cases: YES)
  |-- Example: search by a new axis -> add a parameter to search_contacts()
  |-- Example: return an extra field set -> add an include_* flag to get_contact()
  |              (mirrors the real include_niche / include_photo / include_note flags)
  |-- Example: write a new field -> add a parameter to update_contact()
  |
Is this truly a distinct operation?  (10% of cases: MAYBE)
  |-- Different Contacts object? (containers, groups, group membership)
  |-- Different action type? (not CRUD on a contact -- e.g. vCard import/export)
  |
If NO to all three: You might need a new tool. This is rare.
```

## Anti-Patterns (Never Do These)

### Field-Specific Tools
```python
# WRONG - a field of a contact becomes its own tool (creates tool sprawl)
read_photo(identifier)
write_photo(identifier, image_data)
read_note(identifier)
write_note(identifier, note)

# RIGHT - the field is a parameter / flag on the contact's CRUD tools
get_contact(identifier, include_photo=True)   # photo appears on the contact payload
update_contact(identifier, photo="<base64>")  # "" clears, value sets
get_contact(identifier, include_note=True)
update_contact(identifier, note="...")
```
This is exactly the consolidation made in #98 — see **Past Decisions** below.

### Specialized Search Tools
```python
# WRONG - each filter becomes a tool
get_contacts_with_birthday_this_month()
get_contacts_in_organization("Acme")

# RIGHT - parameters on the existing search
search_contacts(organization="Acme")
# and if a birthday filter is ever needed, it is a NEW PARAMETER on search_contacts(),
# e.g. search_contacts(birthday_window=(start, end)) -- not a new tool.
# (That parameter does not ship today; it illustrates the right shape to add.)
```

### Formatted Text Returns
```python
# WRONG - returns human-readable string
def list_groups():
    return "Family (12 contacts)\nWork (34 contacts)"

# RIGHT - returns structured data
def list_groups():
    return {"success": True, "groups": [{"name": "Family", "count": 12}, ...]}
```

## Response Format Rules

1. **Always return `dict[str, Any]`** with `"success": bool`
2. **Errors include `"error"` (message) and `"error_type"` (category)** — e.g.
   `validation_error`, `not_found`, `authorization_denied`, `partial_success`,
   `safety_violation`, `user_declined`, `rate_limited`
3. **Never return formatted text strings** — return structured data
4. **Include context fields** — e.g. `search_contacts` returns `"count"`,
   `"search_field"`, and `"search_value"` alongside `"contacts"`; `create_contact`
   returns nullable `"group_id"` / `"container_id"` so the shape is stable

## Tool Naming Convention

- Verb + noun: `search_contacts`, `get_contact`, `create_contact`, `update_contact`
- Specific verb over generic: `rename_group` not `update_group_name`
- Most operations act on a single contact/group by `identifier` (singular). The batch
  tools are the vCard pair — `export_vcard(identifiers)` takes a list, `import_vcard`
  takes vCard text — so reach for a list parameter only when the operation is genuinely
  set-oriented, not by default.

## Adding a New Tool Checklist

1. Verify it doesn't fit in an existing tool (run through the decision tree above)
2. Write unit tests (mock the `_run_cn_*` / `_run_applescript_*` boundaries in
   `contacts_connector.py`)
3. Write integration tests (real Contacts.app)
4. Implement in `contacts_connector.py` — primary path is Contacts.framework via PyObjC;
   the AppleScript fallback is only for the `note` field and creation/modification dates
   (see CLAUDE.md Phase 0)
5. Expose in `server.py` with `@mcp.tool()`
6. Run `./scripts/check_tools_md_parity.sh` (`@mcp.tool()` ↔ TOOLS.md parity) and
   `./scripts/check_pyobjc_safety.sh` (PyObjC anti-patterns)
7. Update [`docs/reference/TOOLS.md`](../../../docs/reference/TOOLS.md)
8. Add blind eval scenarios if the tool has non-obvious parameters

## Past Decisions

A breadcrumb log so an already-adjudicated anti-pattern isn't reintroduced.

- **2026-05-30 — photo + note I/O consolidated (#98, PR #102).** Removed the four
  field-specific tools `read_photo`, `write_photo`, `read_note`, `write_note` and folded
  them into `get_contact(include_photo=…, include_note=…)` and
  `update_contact(photo=…, note=…)`. They were textbook **Field-Specific Tool**
  violations of the catalog above. Rule of thumb: **a field of an existing resource is a
  parameter, not a tool.** Surface dropped 21 → 17. See
  [`CHANGELOG.md`](../../../CHANGELOG.md).
