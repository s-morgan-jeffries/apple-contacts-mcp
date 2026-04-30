---
name: contacts-framework
description: Use BEFORE writing any code that touches Contacts.app data. Covers the Contacts.framework / PyObjC primary surface, the AppleScript fallback path for note + modification-date access, the TCC authorization flow, and the gotchas that aren't obvious from Apple's docs (entitlement-gated keys, raw label tokens, vCard 3.0-only export). Read together with `docs/research/contacts-api-gap-analysis.md` for the empirical basis.
---

# Apple Contacts via Contacts.framework

**Primary surface:** `Contacts.framework` via `pyobjc-framework-Contacts`.
**Fallback surface:** AppleScript via `osascript`, used only for `note` and modification/creation timestamps.

This decision was made empirically in [Phase 0](../../docs/research/contacts-api-gap-analysis.md). Do not revisit without new evidence.

## Object model — Contacts.framework

```
CNContactStore                — top-level handle, one per process is fine
├── containers (CNContainer)  — iCloud, Gmail, Exchange, On My Mac
├── contacts (CNContact)      — the unified address book (read-only objects)
└── groups (CNGroup)          — owned by exactly one container

CNMutableContact               — for create/update; pass to CNSaveRequest
CNMutableGroup                 — for group create/update
CNSaveRequest                  — batched mutation; submit via store.executeSaveRequest_error_

CNLabeledValue                 — wraps {label, value} for phones/emails/addresses
CNPhoneNumber                  — value type for phones
CNPostalAddress                — value type for postal addresses
CNContactVCardSerialization    — vCard 3.0 import + export (3.0 only on output, accepts 4.0 input)
```

## The fetch-keys gotcha (read this first)

Every `CNContact` you fetch is **lazy** about properties — you must declare upfront which keys you'll access. Touching an undeclared key raises `CNPropertyNotFetchedException`. Always pass an explicit `keysToFetch` list.

```python
from Contacts import (
    CNContactStore, CNContactFetchRequest,
    CNContactGivenNameKey, CNContactFamilyNameKey,
    CNContactPhoneNumbersKey, CNContactEmailAddressesKey,
)

store = CNContactStore.alloc().init()
keys = [
    CNContactGivenNameKey, CNContactFamilyNameKey,
    CNContactPhoneNumbersKey, CNContactEmailAddressesKey,
]
req = CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)

contacts = []
def collect(contact, stop_ptr):
    contacts.append(contact)

store.enumerateContactsWithFetchRequest_error_usingBlock_(req, None, collect)
```

For vCard export, use `CNContactVCardSerialization.descriptorForRequiredKeys()` instead of an explicit list — the descriptor already includes every key vCard needs:

```python
from Contacts import CNContactVCardSerialization
desc = CNContactVCardSerialization.descriptorForRequiredKeys()
req = CNContactFetchRequest.alloc().initWithKeysToFetch_([desc])
```

## TCC authorization

Contacts is a TCC-protected data class. **Always check status before doing anything else, and on every tool entry** (the user can revoke mid-process).

```python
from Contacts import CNContactStore, CNEntityTypeContacts

CN_STATUS = {
    0: "notDetermined",
    1: "restricted",
    2: "denied",
    3: "authorized",
    4: "limited",  # macOS 14+
}

def check_authorization() -> dict:
    status = CNContactStore.authorizationStatusForEntityType_(CNEntityTypeContacts)
    if status in (3, 4):
        return {"success": True, "status": CN_STATUS[status]}
    return {
        "success": False,
        "error": f"Contacts access not granted (status={CN_STATUS[status]}).",
        "error_type": "authorization_denied",
        "remediation": "Open System Settings → Privacy & Security → Contacts and grant access.",
    }
```

The first call to `requestAccessForEntityType_completionHandler_` triggers the system prompt. The completion handler runs on a background queue; for synchronous CLI flow, prefer surfacing the unauthorized state to the LLM with `error_type: "authorization_denied"` and let the user grant manually, then retry.

## Predicates — the right way to filter

`Contacts.framework` ships several pre-built predicates. Always prefer these over loops:

```python
from Contacts import CNContact

# Match by name
pred = CNContact.predicateForContactsMatchingName_("John")

# Match by phone number
pred = CNContact.predicateForContactsMatchingPhoneNumber_(phone)

# Match by email
pred = CNContact.predicateForContactsMatchingEmailAddress_(email)

# Match by group
pred = CNContact.predicateForContactsInGroupWithIdentifier_(group_uuid)

# Match by container
pred = CNContact.predicateForContactsInContainerWithIdentifier_(container_uuid)

results, err = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
    pred, keys, None
)
```

For organization-name matching, there's no canned predicate — use a custom `NSPredicate`. Document the workaround as you write it.

## Writes — `CNSaveRequest`

Mutations go through `CNSaveRequest`. Build the request, then submit it once.

```python
from Contacts import CNMutableContact, CNSaveRequest, CNLabeledValue, CNPhoneNumber

new_contact = CNMutableContact.alloc().init()
new_contact.setGivenName_("Jane")
new_contact.setFamilyName_("Smith")
phone = CNLabeledValue.labeledValueWithLabel_value_(
    "_$!<Mobile>!$_",  # Apple's internal token for "mobile"
    CNPhoneNumber.phoneNumberWithStringValue_("+15550123"),
)
new_contact.setPhoneNumbers_([phone])

save_req = CNSaveRequest.alloc().init()
save_req.addContact_toContainerWithIdentifier_(new_contact, None)  # None = default container

ok, err = store.executeSaveRequest_error_(save_req, None)
```

Multiple operations on a single `CNSaveRequest` are atomic. Use this for bulk updates rather than looping `executeSaveRequest_error_` per contact.

## Phone label tokens (and other labels)

Apple emits raw `_$!<Home>!$_` / `_$!<Work>!$_` / `_$!<Mobile>!$_` tokens — **never the user-visible string** — for built-in labels. Custom labels come back as plain strings. Translate on output for human readability:

```python
from Contacts import CNLabeledValue

label = phone.label()
human = CNLabeledValue.localizedStringForLabel_(label)  # "home", "work", "mobile", ...
```

For input, there is **no inverse helper** — you must build a bidirectional table mapping `home → _$!<Home>!$_` and friends. This is tracked under [issue #18](https://github.com/s-morgan-jeffries/apple-contacts-mcp/issues/18) (v0.2.0).

## What Contacts.framework can't do — and the AppleScript fallback

Two specific holes in the framework that AppleScript fills:

### 1. Notes (entitlement-gated)

`CNContactNoteKey` requires the `com.apple.developer.contacts.notes` entitlement, which Apple grants only to App Store apps after review. Without it:

- Adding `CNContactNoteKey` to `keysToFetch` silently drops it
- Accessing `contact.note()` raises `CNPropertyNotFetchedException`
- vCard exports via `CNContactVCardSerialization` strip the NOTE field

AppleScript reads/writes notes without entitlement:

```python
import subprocess

def read_note(contact_uuid: str) -> str:
    script = f'''
    tell application "Contacts"
      try
        set p to first person whose id is "{contact_uuid}"
        if note of p is missing value then return ""
        return note of p
      on error
        return ""
      end try
    end tell
    '''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout.rstrip("\n")
```

`{contact_uuid}` is alphanumeric+hyphen so safe to interpolate; any other AppleScript-bound string MUST go through `escape_applescript_string()` (utils.py).

### 2. Modification / creation timestamps

`CNContact.creationDate` and `CNContact.modificationDate` are accessible only via undocumented runtime selectors — not in the public `CNContactKey` constants. If a future macOS removes the selectors, the AppleScript fallback (`creation date of person`, `modification date of person`) is still standard scripting bridge.

A regression test that fails loudly if the selectors disappear is tracked under [issue #29](https://github.com/s-morgan-jeffries/apple-contacts-mcp/issues/29) (v0.4.0).

## Multi-container behavior

`store.containers()` returns every active account. `store.defaultContainerIdentifier()` is the implicit target for `addContact:toContainerWithIdentifier:None`.

Container types (`CNContainerType`):
- `1` — Local (legacy "On My Mac")
- `2` — Exchange
- `3` — CardDAV (iCloud, Google, Fastmail, Nextcloud, etc.)

In practice, **most macOS systems show only CardDAV containers** — even iCloud is CardDAV under the hood. `enumerateContactsWithFetchRequest` is auto-unified across containers; no extra work needed for cross-account reads. To scope, use `predicateForContactsInContainerWithIdentifier:`.

Groups belong to **exactly one** container. If you `add_contact_to_group` and the contact lives in a different container than the group, the framework returns an error. Surface this clearly — don't let it bubble up as an opaque `NSError`.

## vCard

`CNContactVCardSerialization` is asymmetric:
- **Export emits 3.0 only**, even on macOS 26 (PRODID line: `-//Apple Inc.//macOS X.Y.Z//EN`)
- **Import accepts both 3.0 and 4.0**

NOTE field is stripped from export (entitlement-gated, same as `CNContactNoteKey` reads). If notes need to survive a vCard round-trip, post-process the export to inject NOTE from the AppleScript-read note text.

```python
data, err = CNContactVCardSerialization.dataWithContacts_error_([contact], None)
vcard_text = bytes(data).decode("utf-8")

# Round-trip parse
new_contacts, err = CNContactVCardSerialization.contactsWithData_error_(data, None)
```

## Response shape — every tool

Per `MCP_PLAYBOOK.md §1`:

```python
{
    "success": True | False,
    # Success path: domain-specific result keys
    "contacts": [...],          # for list/search
    "contact": {...},           # for get
    "identifier": "UUID:...",   # for create
    # Error path
    "error": "human readable description",
    "error_type": "authorization_denied" | "not_found" | "validation" | "unknown",
}
```

## Gotchas checklist (every PyObjC-touching PR)

1. **Did you declare `keysToFetch` for every property you'll access?** If not, expect `CNPropertyNotFetchedException` at runtime.
2. **Did you call `check_authorization()` at tool entry?** Status can change mid-process.
3. **Did you translate phone/email/address label tokens** before returning to the LLM? Raw `_$!<...>!$_` tokens are gibberish.
4. **For writes: did you apply the security checklist** (sanitize, validate, rate-limit, audit, test-mode)? Pattern: `MCP_PLAYBOOK.md §4` + the contacts-specific TCC step.
5. **For `imageData()`: did you guard with `imageDataAvailable()` first?** Otherwise you get a confusing nil result on contacts without photos.
6. **For vCard export: did you fetch with `descriptorForRequiredKeys()`** rather than a manual key list? Manual lists usually miss something.
7. **For CNSaveRequest: did you batch?** One save per tool call, not one save per contact in a loop.
8. **Did you mock at `_run_cn_*` for unit tests AND write an integration test that hits a real `CNContactStore`?** Mocked tests miss real-API bugs.
