# Contacts API Gap Analysis (Phase 0)

> **Status:** authoritative as of 2026-04-29 on macOS 26.3.1.
> Probes were run against a real Contacts database (1696 people, 8 groups, single iCloud container).
> Probe scripts: `/tmp/contacts-probe/` (not committed — they're throwaway).

## Goal

Inventory every Contacts.app capability and map each to one of four API surfaces — AppleScript SDEF, Contacts.framework via PyObjC, JXA, vCard. Pick a primary API for v0.1.0+.

## TL;DR — API decision

**Primary: Contacts.framework via PyObjC. Fallback: AppleScript via `osascript`.**

Contacts.framework is faster (≈4–5×), produces structured data without ASObjC plumbing, and supports predicate fetches. AppleScript is the **mandatory** fallback for two specific cases:

1. **`note` field** — entitlement-gated in Contacts.framework (silently dropped on fetch, stripped on vCard export). Read/write via AppleScript works without entitlement.
2. **`modificationDate` / `creationDate`** — accessible via undocumented runtime selectors in Contacts.framework, but not part of the supported `CNContactKey` set. AppleScript exposes them via the standard scripting bridge. Use AppleScript for any "what changed since X" query if Apple removes the runtime selectors in a later macOS.

vCard via `CNContactVCardSerialization` is a serialization helper — emits 3.0 only (even on macOS 26), accepts 3.0 + 4.0 input. Treat it as transport, not as a primary surface.

JXA exposes nothing SDEF doesn't and adds JavaScript-bridge overhead — **drop it from consideration**.

## 1. Inventory of Contacts.app capabilities

User-visible features in Contacts.app and macOS Contacts data:

| Capability | Notes |
|---|---|
| Read/list contacts | 1696 in the test database |
| Create / update / delete contact | `CNSaveRequest` add/update/delete |
| Search by name | predicate or `whose name contains` |
| Filter by group, by attribute (email, phone, org) | predicate-based |
| Read/write fields: name parts (first/middle/last/suffix/maiden/phonetic), nickname, organization, department, job title | all surfaces |
| Read/write phones, emails, postal addresses, URLs (multi-valued, labeled) | all surfaces; **labels come back as `_$!<Home>!$_` tokens** that need translation |
| Read/write social profiles, instant messages, related names, custom dates | all surfaces |
| **Notes** (free-text) | **entitlement-gated in CNContacts**; AppleScript only |
| Photos (read/write image data) | both surfaces |
| Read birthday | both surfaces |
| Groups: create, rename, delete | both surfaces; `CNGroup` + `CNSaveRequest` |
| Group membership: add/remove person to/from group | both surfaces; `addMember:toGroup:` on `CNSaveRequest` |
| Containers (iCloud / On My Mac / Exchange) | `CNContainer` — Contacts.framework only |
| vCard import/export (3.0) | `CNContactVCardSerialization` |
| vCard 4.0 export | **not supported** — Apple emits 3.0 only |
| Mod/creation timestamps | undocumented in CN; standard in AppleScript |
| Smart groups / saved searches | UI only — no scripting bridge |
| Sharing / iCloud sync state | not exposed |
| Recents / interaction history | not exposed |

## 2. Per-surface accessibility matrix

Legend: ✅ supported · ⚠ supported with caveat · ❌ not supported · — not applicable

| Capability | Contacts.framework (PyObjC) | AppleScript SDEF | JXA | `CNContactVCardSerialization` |
|---|:-:|:-:|:-:|:-:|
| **Authorization (TCC)** | ✅ explicit `CNAuthorizationStatus` | ✅ implicit (Apple Events automation) | ✅ implicit | — |
| List/enumerate | ✅ ~0.05 s for 1696 | ✅ ~0.29 s for 1696 | ✅ same as AS | — |
| Read by identifier (UUID) | ✅ `unifiedContactWithIdentifier:` | ✅ via `id` property | ✅ | — |
| Predicate by name | ✅ `predicateForContactsMatchingName:` | ✅ `whose name contains` | ✅ | — |
| Predicate by group | ✅ `predicateForContactsInGroupWithIdentifier:` | ⚠ slow loop only | ⚠ same | — |
| Predicate by phone/email | ✅ `predicateForContactsMatchingPhoneNumber:` / `Email:` | ⚠ slow loop only | ⚠ same | — |
| Create / update / delete | ✅ `CNSaveRequest` | ✅ `make`, set, `delete` | ✅ same as AS | — |
| Read note | ❌ entitlement-gated (silent fail) | ✅ | ✅ | — |
| Write note | ❌ entitlement-gated | ✅ | ✅ | — |
| Read image data | ✅ `imageData()` + `imageDataAvailable()` | ⚠ `image of person` returns TIFF coercion (lossy) | ⚠ same as AS | — |
| Write image data | ✅ `setImageData:` | ⚠ TIFF only | ⚠ same | — |
| Modification / creation date | ⚠ runtime selector only (undocumented) | ✅ `modification date` / `creation date` | ✅ | — |
| Group CRUD | ✅ | ✅ | ✅ | — |
| Group membership | ✅ `addMember:toGroup:` | ✅ `add person to group` | ✅ | — |
| Containers | ✅ `CNContainer` | ❌ | ❌ | — |
| vCard 3.0 export | — | — | — | ✅ but **strips NOTE** |
| vCard 3.0 import (parse) | — | — | — | ✅ |
| vCard 4.0 export | — | — | — | ❌ outputs 3.0 even on input 4.0 |
| vCard 4.0 import (parse) | — | — | — | ✅ accepts 4.0 |
| Phone label localization | ⚠ raw `_$!<Home>!$_` token, translate via `CNLabeledValue.localizedStringForLabel:` | ⚠ same | ⚠ same | — |
| Smart groups | ❌ | ❌ | ❌ | — |
| Recents / interaction | ❌ | ❌ | ❌ | — |
| Performance baseline | 53 ms / 1696 contacts | 293 ms / 1696 contacts | ~same as AS | — |

## 3. TCC authorization

Contacts is a TCC-protected data class on macOS. The system prompts the first time a process touches the API.

### Authorization states (`CNAuthorizationStatus`)

| Raw | Name | Behavior |
|:-:|---|---|
| 0 | `notDetermined` | Has not asked yet. Next call will prompt. |
| 1 | `restricted` | Locked by parental controls / MDM. |
| 2 | `denied` | User said no. Calls return empty / error. |
| 3 | `authorized` | Full access. |
| 4 | `limited` | macOS 14+; user granted access to a subset. |

### Auth check pattern (call before any read/write)

```python
from Contacts import CNContactStore, CNEntityTypeContacts

CN_STATUS = {0: "notDetermined", 1: "restricted", 2: "denied", 3: "authorized", 4: "limited"}

def check_authorization() -> dict:
    status = CNContactStore.authorizationStatusForEntityType_(CNEntityTypeContacts)
    if status == 3 or status == 4:
        return {"success": True, "status": CN_STATUS[status]}
    return {
        "success": False,
        "error": f"Contacts access not granted (status={CN_STATUS[status]}).",
        "error_type": "authorization_denied",
        "remediation": "Open System Settings → Privacy & Security → Contacts and grant access.",
    }
```

### Requesting access (will prompt)

```python
import objc
from PyObjCTools import AppHelper

store = CNContactStore.alloc().init()

def on_complete(granted: bool, error: objc.objc_object | None) -> None:
    print(f"granted={granted}, err={error}")

store.requestAccessForEntityType_completionHandler_(CNEntityTypeContacts, on_complete)
```

The completion handler runs on a background queue. For a synchronous CLI flow, prefer surfacing the unauthorized state to the LLM with a clear `error_type` and let the user grant access manually, then retry.

### Bundling note

Running unbundled (`uv run python -m apple_contacts_mcp.server`) prompts via the launching process's TCC identity. When packaged into Claude Desktop, an `Info.plist` `NSContactsUsageDescription` key is required for the prompt copy. **This needs to be re-tested after packaging is set up; not relevant for the unbundled v0.1.0 release.**

## 4. Working code samples

### List contacts (Contacts.framework)

```python
from Contacts import (
    CNContactStore,
    CNContactFetchRequest,
    CNContactGivenNameKey,
    CNContactFamilyNameKey,
    CNContactIdentifierKey,
)

store = CNContactStore.alloc().init()
keys = [CNContactIdentifierKey, CNContactGivenNameKey, CNContactFamilyNameKey]
req = CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)

contacts: list = []
def collect(contact, stop_ptr) -> None:
    contacts.append({
        "id": contact.identifier(),
        "given_name": contact.givenName(),
        "family_name": contact.familyName(),
    })

store.enumerateContactsWithFetchRequest_error_usingBlock_(req, None, collect)
```

### Search by name (predicate)

```python
from Contacts import CNContact

pred = CNContact.predicateForContactsMatchingName_("John")
results, err = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
    pred, [CNContactIdentifierKey, CNContactGivenNameKey, CNContactFamilyNameKey], None
)
```

### Read note (AppleScript fallback)

```python
import subprocess

def read_note(contact_uuid: str) -> str:
    script = f'''
    tell application "Contacts"
      set p to first person whose id is "{contact_uuid}"
      if note of p is missing value then
        return ""
      else
        return note of p
      end if
    end tell
    '''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=10
    )
    return result.stdout.strip()
```

`{contact_uuid}` MUST be sanitized (`apple_contacts_mcp.utils.escape_applescript_string` once written) — UUIDs are alphanumeric+hyphen so trivially safe, but other AppleScript-bound strings need escaping.

### Group membership predicate

```python
from Contacts import CNContact

groups, _ = store.groupsMatchingPredicate_error_(None, None)
target = next((g for g in groups if g.name() == "Family"), None)
if target:
    pred = CNContact.predicateForContactsInGroupWithIdentifier_(target.identifier())
    members, err = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
        pred, keys, None
    )
```

### Create + save contact (write path — sample only, not exercised against real data)

```python
from Contacts import CNMutableContact, CNSaveRequest, CNLabeledValue, CNPhoneNumber

new_contact = CNMutableContact.alloc().init()
new_contact.setGivenName_("Jane")
new_contact.setFamilyName_("Smith")
phone = CNLabeledValue.labeledValueWithLabel_value_(
    "_$!<Mobile>!$_",
    CNPhoneNumber.phoneNumberWithStringValue_("+15550123"),
)
new_contact.setPhoneNumbers_([phone])

save_req = CNSaveRequest.alloc().init()
save_req.addContact_toContainerWithIdentifier_(new_contact, None)  # None = default container

ok, err = store.executeSaveRequest_error_(save_req, None)
```

### vCard export (3.0 only — strips NOTE)

```python
from Contacts import CNContactVCardSerialization

descriptor = CNContactVCardSerialization.descriptorForRequiredKeys()
req = CNContactFetchRequest.alloc().initWithKeysToFetch_([descriptor])
out: list = []
def cb(c, sp) -> None:
    out.append(c)
store.enumerateContactsWithFetchRequest_error_usingBlock_(req, None, cb)

data, err = CNContactVCardSerialization.dataWithContacts_error_(out, None)
vcard_text = bytes(data).decode("utf-8")  # PRODID: -//Apple Inc.//macOS X.Y.Z//EN, VERSION:3.0
```

### vCard parse (3.0 + 4.0 input both work)

```python
v = b"""BEGIN:VCARD
VERSION:4.0
FN:Jane Smith
N:Smith;Jane;;;
TEL;TYPE=cell:+1-555-0200
END:VCARD
"""
contacts, err = CNContactVCardSerialization.contactsWithData_error_(v, None)
```

## 5. Priority tiers for the v0.1.0+ roadmap

| Tier | Capability | Surface |
|---|---|---|
| **P1** | `list_contacts` (paged) | CN |
| **P1** | `get_contact` (by UUID) | CN |
| **P1** | `search_contacts` (by name predicate) | CN |
| **P1** | `create_contact` | CN + `CNSaveRequest` |
| **P1** | `update_contact` | CN + `CNSaveRequest` |
| **P1** | `delete_contact` | CN + `CNSaveRequest` |
| **P1** | TCC authorization status check + clear error surfacing | CN |
| **P2** | Notes read/write | **AppleScript fallback** |
| **P2** | `search_contacts` (by phone, email, organization) | CN predicates |
| **P2** | `list_groups` | CN |
| **P2** | `get_contacts_in_group` | CN predicate |
| **P2** | `add_contact_to_group` / `remove_contact_from_group` | CN + `CNSaveRequest` |
| **P2** | `export_vcard` (one or many contacts) | `CNContactVCardSerialization` |
| **P2** | `import_vcard` | `CNContactVCardSerialization` |
| **P3** | `create_group` / `rename_group` / `delete_group` | CN + `CNSaveRequest` |
| **P3** | Photo read/write | CN + `imageData` |
| **P3** | Containers (multi-source: iCloud / On My Mac / Exchange) | CN `CNContainer` |
| **P3** | Custom dates, social profiles, related names, IM | CN |
| **P3** | Phone label localization helper (translate `_$!<Home>!$_` → "home") | CN `CNLabeledValue.localizedStringForLabel:` |

This maps directly onto the BOOTSTRAP-defined milestones:

- **v0.1.0** — Tier P1 (core CRUD)
- **v0.2.0** — Tier P2 (filters/queries; group ops; vCard)
- **v0.3.0** — Tier P3 (niche fields, photo, containers)
- **v0.4.0** — Infrastructure hardening (coverage 90%, rate limiting, confirmation UX, test-mode safety, packaging Info.plist for Claude Desktop)

## 6. Open empirical questions

1. **Notes entitlement** — Apple grants `com.apple.developer.contacts.notes` on a case-by-case basis for App Store apps. We are unbundled and won't get it. Is the AppleScript fallback acceptable for v0.1.0, or do we surface a "notes feature requires AppleScript" capability flag the LLM can check? **Decision: AppleScript fallback only; document.**
2. **`modificationDate` / `creationDate` durability** — these are not documented `CNContactKey` constants but are accessible via runtime selectors. Are they stable across macOS 26 → 27? Re-check after each macOS major release. If they break, AppleScript is the fallback.
3. **vCard 4.0 export** — no native path. Open question whether to (a) post-process the 3.0 output to upgrade to 4.0, (b) ship a 3rd-party encoder (e.g., `vobject`), or (c) document 3.0-only as the limitation. **Decision: deferred to v0.2.0 — emit 3.0 in v0.1.0; revisit before v0.2.0.**
4. **Phone label translation** — should the MCP layer translate `_$!<Mobile>!$_` → `"mobile"` on read? Yes for output, but **on input** we need to accept "mobile" or "Home" and translate back. Adds a small bidirectional table; deferred to v0.2.0 if the table is non-trivial.
5. **Photo format** — Contacts.framework returns raw bytes; magic-byte detection (JPEG/PNG/HEIC) needed in the response. Test with iCloud-synced contacts for HEIC variants. Deferred to v0.3.0.
6. **Authorization revocation mid-process** — if the user revokes during operation, the next call returns empty results / error silently in some cases. Build a re-check at the entry of every tool, not just at start-up. Captured as a v0.4.0 hardening task.
7. **Test mode safety pattern** — `CONTACTS_TEST_MODE=true` + `CONTACTS_TEST_GROUP=<name>`. Destructive ops verify the target group via the API before proceeding. Same pattern as `apple-mail-mcp`'s `check_test_mode_safety`. Captured for v0.1.0 implementation.
8. **Containers and `On My Mac`** — single-container test database (iCloud only). Need a multi-container test rig (a non-iCloud Mac account) before shipping P3 container features.

## 7. Decision

**Primary surface for v0.1.0+: Contacts.framework via PyObjC.**

**Mandatory AppleScript fallback for:**
- `note` field (read + write)
- modification / creation date queries (after Apple-Events probe whether the runtime selectors break)

**Skill name in BOOTSTRAP §4.2:** `contacts-framework` (not `applescript-contacts`). A separate small skill or section will cover the AppleScript escape patterns since the fallback path uses them.

**Connector mock boundary:** wrap each PyObjC call site behind a `_run_cn_*` helper, parallel to `_run_applescript`. Both helpers get mocked at the unit test level; integration tests run against a real Contacts database.

**`scripts/check_applescript_safety.sh`** — copy from `apple-mail-mcp` adapted for the smaller AppleScript surface (just notes + dates).
A new `scripts/check_pyobjc_safety.sh` is needed: scans for unsafe patterns like passing user input into KVC keys, missing `descriptorForRequiredKeys` calls before vCard export, and missing `imageDataAvailable()` guards before `imageData()` reads. **Filed as v0.4.0 infrastructure issue.**
