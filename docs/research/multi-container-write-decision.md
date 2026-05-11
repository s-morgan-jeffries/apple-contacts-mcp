# Multi-container write round-trip

**Resolved:** 2026-05-10
**Closes:** [contacts-api-gap-analysis.md §6 Q8a](./contacts-api-gap-analysis.md#6-open-empirical-questions)
**Tracking issue:** #29
**Unblocks:** #26 (`list_containers`, `create_contact_in_container`)
**Defers:** Q8b (CardDAV groups; needs a populated Google-side group), Q8c (Exchange / On-My-Mac; needs those account types in the rig)

## Question

The Phase 0 [Q8 probe](./contacts-api-gap-analysis.md#6-open-empirical-questions)
established read-side multi-container behavior empirically (default container,
unified enumeration, per-container predicates). Write-side behavior was
unverified: when `CNSaveRequest.addContact:toContainerWithIdentifier:` is given
a **non-default container identifier**, does the framework actually write the
contact to that container — or does it silently coerce to the default (iCloud)
and leave the explicit identifier as a hint that gets ignored?

Without confirming this, #26 (`create_contact_in_container`) would be designing
against unverified API behavior.

## Decision

**`addContact:toContainerWithIdentifier:<non-default-uuid>` writes to the named
container.** The framework respects the explicit identifier; the contact does
not leak into the default container.

**Implication for #26.** `create_contact_in_container` is a thin extension of
the existing `create_contact` — accept an optional `container_identifier`
parameter, pass it through to the same `addContact:toContainerWithIdentifier:`
call site at [contacts_connector.py:403](../../src/apple_contacts_mcp/contacts_connector.py#L403), default to `None` (current behavior). No
workaround, capability flag, or post-write fix-up needed.

## Empirical basis

### Probe procedure

A one-off Python script instantiated `CNContactStore`, enumerated containers,
built a `CNMutableContact` with a sentinel name (`given_name="Q8aProbe"`,
`family_name="Probe-<utc-timestamp>"`), and ran the round-trip:

1. `CNSaveRequest.addContact:toContainerWithIdentifier:<gmail-uuid>` —
   explicit non-default container identifier.
2. `unifiedContactsMatchingPredicate:CNContact.predicateForContactsInContainerWithIdentifier:<gmail-uuid>` — confirm the sentinel lands in Gmail.
3. Same predicate against `<icloud-uuid>` — confirm it did **not** land in the default.
4. `CNSaveRequest.deleteContact:` cleanup, re-running the predicates to confirm 0 survivors.

The probe script itself is one-off and not committed — matching the pattern
used for [vcard-version-decision.md](./vcard-version-decision.md) and
[label-translation-decision.md](./label-translation-decision.md).

### Run output (macOS 26.3.1, 2026-05-10)

```
[auth] CN authorization status: 3 (3 = authorized)

[containers] count=2
  - name='Gmail' id=797C8A05-…:ABAccount type=3 (CardDAV)
  - name='iCloud' id=F7F61738-…:ABAccount type=3 (CardDAV)
[containers] defaultContainerIdentifier=F7F61738-…:ABAccount

[probe] built sentinel contact: given_name='Q8aProbe' family_name='Probe-20260510T235219'
[write] ok=True identifier=685081FC-…:ABPerson

[read gmail]  total_in_container=1402 sentinel_matches=1
[read icloud] total_in_container=1696 sentinel_matches=0 (expected 0)

[cleanup] deleteContact ok=True identifier=685081FC-…:ABPerson
[cleanup] post-delete sentinel survivors in Gmail: 0 (expected 0)
```

### What this confirms

- **Explicit container ID is honored on write.** The sentinel showed up in
  Gmail (1 match) and not in iCloud (0 matches). The default-container fallback
  is *not* applied silently when a non-default identifier is supplied.
- **`predicateForContactsInContainerWithIdentifier:` scopes reads correctly.**
  Pre-write the predicate against Gmail returned 1402 contacts; after the
  write it returned 1403 (with our sentinel as the 1403rd). Against iCloud it
  stayed at 1696 throughout.
- **Cleanup via `deleteContact:` works on a contact created in a non-default
  container.** Same primitive that production uses for `delete_contact`; no
  container-specific delete path is needed.
- **Identifier format note for readers.** CN identifiers carry a type suffix:
  containers come back as `<UUID>:ABAccount`, contacts as `<UUID>:ABPerson`,
  groups as `<UUID>:ABGroup`. This is a stable convention worth documenting
  for #26's parameter validation.

### Surprising observation about the rig

The Gmail container reported 1402 contacts at probe time — not the "synced but
empty" state recorded at the original Q8 probe. This doesn't affect Q8a's
findings (the sentinel-match numbers are the load-bearing evidence), but it
means the rig has accumulated Google-side contacts since the original probe.
**Q8b (CardDAV groups) may now be partially reachable** — if any of those 1402
contacts belong to a Google-side group, the group should surface in
`list_groups`. Out of scope for this issue; flagged here so #26 / a future
Q8b probe knows the rig is no longer "Gmail is empty."

## Why we kept the probe one-off rather than as an integration test

Same reasoning as [vcard-version-decision.md](./vcard-version-decision.md): the
empirical question — "does the framework honor a non-default container UUID on
write?" — only needed to be answered once. Re-running it on each macOS release
isn't valuable until Apple actually changes the behavior. The decision file is
the durable artifact; the probe script is throwaway.

If Apple changes container semantics in a future macOS release, the right
response is a fresh probe (and a fresh decision doc revision), not a
permanently-running integration test gated behind an env var.

## Out of scope for this issue

- **Q8b (CardDAV groups).** Needs a Google-side group present in the rig. The
  rig has Google-side **contacts** now (1402 of them) but it's unverified
  whether any Google-side groups exist. A future probe can run
  `list_groups()` and partition results by `container_id` to find out.
- **Q8c (Exchange / On-My-Mac).** Needs those account types configured in the
  rig. Defer until a contributor has access.
- **Implementation of `list_containers` / `create_contact_in_container`.**
  That's [#26](https://github.com/s-morgan-jeffries/apple-contacts-mcp/issues/26). This decision unblocks #26 by confirming the underlying primitive works.
