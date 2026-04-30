---
name: contacts-performance
description: Use when designing list/search/batch operations or chasing a slow tool. Establishes the empirical baselines from Phase 0 plus the patterns that hold for Contacts.framework specifically — most importantly the absence of subprocess overhead that makes mail-style perf advice misleading. Detailed per-tool baselines are tracked under issue #28 (v0.3.0).
---

# Contacts performance patterns

The performance characteristics of `Contacts.framework` differ fundamentally from `apple-mail-mcp` — there is no `osascript` subprocess in the hot path for the primary surface. Mail's "batch into a single AppleScript invocation" advice does not apply. This skill captures what does.

## Headline baselines (Phase 0, macOS 26.3.1, M-series Mac, 1696-contact iCloud DB)

| Operation | Baseline | Source |
|---|:-:|---|
| `enumerateContactsWithFetchRequest` (1696 contacts, 3 keys) | **53 ms** | gap analysis §2 |
| AppleScript `name of every person` (same 1696) | 293 ms | gap analysis §2 |
| `predicateForContactsMatchingName_("John")` | <50 ms (28 matches) | gap analysis §4 |
| `groupsMatchingPredicate_error_(None)` (8 groups) | <10 ms | empirical |
| AppleScript `whose note is not ""` (over 1696) | **timed out at 60 s** | gap analysis §2 |
| `CNContactVCardSerialization.dataWithContacts:error:` (1 contact) | <5 ms | empirical |

**Observation:** the framework is consistently 4–6× faster than AppleScript for the same conceptual operation, and predicate-based filters are *orders of magnitude* faster than AppleScript loops over arbitrary properties. Per-tool baselines (single-contact read, predicate fetch with various result sizes, save round-trip, multi-contact vCard export) are still untaken; tracked under [issue #28](https://github.com/s-morgan-jeffries/apple-contacts-mcp/issues/28).

## Core pattern: predicates beat loops

```python
# WRONG — quadratic in contact count, hits the framework once per contact
for c in all_contacts:
    if "John" in c.givenName():
        results.append(c)

# RIGHT — single predicate, optimized at the framework level
pred = CNContact.predicateForContactsMatchingName_("John")
results, _ = store.unifiedContactsMatchingPredicate_keysToFetch_error_(pred, keys, None)
```

This is more than a 10× win on 1700-contact databases and grows worse as contact count grows. **For any "find me contacts where X" tool, use a predicate** — even if you have to construct a custom `NSPredicate` for fields without a canned helper.

## Pagination

`enumerateContactsWithFetchRequest` cannot itself paginate — it streams the full result set through the callback. For paged tool responses:

```python
def list_contacts(offset: int = 0, limit: int = 50) -> dict:
    LIMIT_HARD_CAP = 200
    limit = min(limit, LIMIT_HARD_CAP)

    contacts = []
    skipped = 0
    def collect(contact, stop_ptr):
        nonlocal skipped
        if skipped < offset:
            skipped += 1
            return
        if len(contacts) >= limit:
            stop_ptr[0] = True  # short-circuit the enumeration
            return
        contacts.append(serialize(contact))

    store.enumerateContactsWithFetchRequest_error_usingBlock_(req, None, collect)
    return {"success": True, "contacts": contacts, "offset": offset, "limit": limit}
```

The `stop_ptr[0] = True` short-circuits the framework's enumeration — without it you pay the full O(N) cost even when you only want 50 contacts.

## Batch writes — `CNSaveRequest` is the unit, not the call

```python
# WRONG — N round-trips through the framework
for update in updates:
    req = CNSaveRequest.alloc().init()
    req.updateContact_(update)
    store.executeSaveRequest_error_(req, None)

# RIGHT — one round-trip, atomic
req = CNSaveRequest.alloc().init()
for update in updates:
    req.updateContact_(update)
store.executeSaveRequest_error_(req, None)
```

`CNSaveRequest` is internally batched and atomic. There is no batching benefit beyond one request — but the cost of submitting N requests is N× the overhead of submitting one. Build the full save request, submit once.

## Key-fetch parsimony

Every key in `keysToFetch` adds load. Fetch only what the tool actually returns. Don't reflexively pass the full key set "in case we need it" — the framework actually reads from the underlying store, so excess keys hit disk.

For tools that return a small projection (e.g., `list_contacts` returning `[{id, given_name, family_name, organization}]`), declare exactly those four keys. For tools that return the full contact (e.g., `get_contact`), declare the full P1 key set.

## When to AppleScript

The fallback path runs `osascript` as a subprocess — **expect 200–400 ms of overhead per invocation** even for trivial scripts. Same as mail. Specifically, for `note` reads:

| Operation | Estimated cost |
|---|:-:|
| Single `note of person` read | ~250 ms (osascript spin-up dominates) |
| `note of every person` (1696) — IF feasible | several seconds best case; **predicates over notes time out** |

**Strategy:** for note-heavy tools, batch by enumerating contacts via `Contacts.framework` first (fast), then make N AppleScript calls only for the contacts the tool actually returns. Never iterate AppleScript over every contact.

If you find yourself wanting to filter by note content (`whose note contains "..."`), don't — the predicate times out as documented above. Either:
1. Feature-flag the search and bail with a clear error
2. Mass-fetch notes via AppleScript into a side-cache and search the cache (only feasible for small contact counts, which contradicts why you'd want to filter in the first place)

The cleanest path is option 1.

## Profiling

For empirical work (issue #28), `tests/benchmarks/` exists. Run:

```bash
make benchmark               # current vs. baseline
make benchmark-baseline      # re-capture baselines after intentional perf changes
```

Per `tests/conftest.py`, benchmark tests are opt-in via `--run-benchmark` so they don't slow regular CI. Capture baselines into `tests/benchmarks/baseline.json`.

## Gotchas

1. **`enumerateContactsWithFetchRequest` runs the callback synchronously** on the calling thread. There's no async to await; just call it.
2. **`unifiedContactsMatchingPredicate_keysToFetch_error_` returns a list, not a generator.** For unbounded queries this can balloon memory — prefer `enumerate*` with stop-pointer if you only need the first N results.
3. **`store.containerOfContactWithIdentifier:error:` is one extra round-trip per contact.** If a tool needs the container per contact for a list, batch by fetching contacts per container instead.
4. **`imageData()` decodes only when accessed.** Reading `imageDataAvailable()` first is cheap — use it as a guard, don't always read `imageData()`.

## When to revisit

Re-profile when:
- Apple ships a major macOS release (re-run baselines)
- Contact counts in the test rig differ by 5×+ from the 1696-contact baseline
- A tool ships that the original baselines didn't cover (per-op baseline added under issue #28)
