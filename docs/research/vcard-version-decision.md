# vCard version-export decision

**Resolved:** 2026-05-09
**Closes:** [contacts-api-gap-analysis.md §6 Q3](./contacts-api-gap-analysis.md#6-open-empirical-questions)
**Tracking issue:** #23
**Unblocks:** #20 (`export_vcard` / `import_vcard`)

## Question

`Contacts.framework`'s `CNContactVCardSerialization.dataWithContacts:error:`
emits **vCard 3.0 only** — even on macOS 26.3.1, even when given a contact
that was originally imported from a 4.0 source. The parser side accepts both
3.0 and 4.0 input.

Before `export_vcard` ships in v0.2.0 we need to decide what version the
tool's output advertises:

- **(a)** Post-process Apple's 3.0 output to upgrade to 4.0.
- **(b)** Ship a third-party encoder (e.g., [`vobject`](https://github.com/skarim/vobject))
  and emit 4.0 directly.
- **(c)** Document 3.0-only as the limitation; emit Apple's bytes verbatim.

## Decision: option (c)

Emit Apple's vCard 3.0 verbatim. Document the limitations clearly in the
`export_vcard` docstring and in [TOOLS.md](../reference/TOOLS.md). When real
user demand for 4.0 surfaces (concrete cross-vendor flow, specific consumer
breakage, or Apple loosening the constraint), revisit.

## Why not option (a) — post-process 3.0 → 4.0

A correct 3.0→4.0 transformer is, in effect, a partial vCard parser plus
emitter:

- Line-folding rules differ subtly. RFC 2425 §5.8.1 vs. RFC 6350 §3.2.
- Parameter casing and value-list semantics changed (`TYPE=CELL,VOICE` →
  `TYPE=cell` with separate-property repetition). Apple already emits a
  hybrid (lowercase `type=` chained with semicolons) that doesn't match
  either spec literally.
- BDAY year-less encoding is the most consequential: Apple's
  `X-APPLE-OMIT-YEAR=1604:1604-05-15` must become `BDAY:--05-15`.
- PHOTO encoding shifts from `PHOTO;ENCODING=BASE64;TYPE=JPEG:...` to
  `PHOTO:data:image/jpeg;base64,...`.
- Apple's PRODID line would need to be either preserved (lying about who
  emitted the 4.0) or rewritten (and now we own the version-string update
  cadence forever).

Each of these is a small bug surface. Together they're a maintenance
liability disproportionate to the benefit, and the tool-side promise
("emits 4.0") would silently degrade as Apple's 3.0 output evolves.

## Why not option (b) — ship `vobject`

`vobject` is well-maintained (MIT, Python-only, active) and would handle
the boilerplate (line folding, escaping, parameter encoding). But two
constraints make it a poor fit:

- **It doesn't auto-upgrade 3.0 → 4.0.** `vobject` parses 3.0 to a 3.0
  in-memory representation and serializes 3.0 back. Emitting 4.0 means
  building the vCard from our contact dict directly, mapping every
  property by hand — which is the same work as option (a) without the
  benefit of starting from Apple's structured output.
- **A runtime dependency for narrow benefit.** Adding a transitive dep is
  cheap individually but compounds the install/audit/upgrade surface for
  the whole project. Worth it for substantial functionality, not for one
  property's encoding.

If we later decide we need 4.0, `vobject` is a reasonable choice — but
this PR isn't the moment to commit.

## Why option (c)

- **vCard 3.0 is universally consumed.** iOS / macOS Contacts, Outlook
  (all surfaces), Google Contacts, Android contacts, Evolution Data
  Server, libcontacts, KAddressBook — all parse 3.0 cleanly. 3.0 is the
  de facto interchange format in 2026.
- **Apple↔Apple round-trip is solid.** Export from one Mac, import into
  another, and every field — including the year-less BDAY hack — survives
  via Apple's `X-APPLE-OMIT-YEAR` recognition.
- **Cost is zero.** Apple's serializer hands us bytes; we hand them to the
  caller. Nothing to maintain, nothing to break.
- **Reversible.** If the limitations bite a real user, we can move to (a)
  or (b) without breaking the existing tool surface — just bump the
  version line and add the transformation.

## Known limitations (must surface in tool docs when #20 lands)

These belong in `export_vcard`'s docstring **and** in
[`TOOLS.md`](../reference/TOOLS.md) so callers know what they're getting:

1. **NOTE field is omitted.** `CNContactVCardSerialization` strips the
   note because the underlying key is entitlement-gated
   (`com.apple.developer.contacts.notes`). The tool emits no NOTE
   property. Use [`read_note(identifier)`](../reference/TOOLS.md) to read
   the note separately and merge into the vCard payload at the call site
   if needed.
2. **Year-less birthdays corrupt to "1604" for non-Apple consumers.**
   Apple stores year-less birthdays internally (and our `get_contact`
   round-trips the `{month, day}` shape correctly). The 3.0 export emits
   them as `BDAY;X-APPLE-OMIT-YEAR=1604:1604-05-15`. Apple recognizes its
   own `X-APPLE-OMIT-YEAR` parameter on import and strips the year. Other
   parsers (Google Contacts, Outlook, generic vCard libs) treat the date
   as literal **May 15, 1604**.
3. **No vCard 4.0-specific properties.** `KIND`, `ANNIVERSARY`, `GENDER`,
   `LANG`, `RELATED` are 4.0-only and are not emitted regardless of
   whether the contact has analogous data internally.

## When to revisit

Re-open this decision (file a follow-up issue and supersede this doc) if
**any** of these is true:

- A user files an issue describing a concrete cross-vendor export flow
  (e.g., "Apple → Google Contacts via our `export_vcard`") that hits the
  year-less BDAY corruption or another 3.0/4.0 mismatch.
- Apple ships native vCard 4.0 export in a future macOS — flag-day
  upgrade, no transformer needed.
- A specific consumer (Google Contacts is the most likely) updates to
  reject 3.0 input or to silently drop properties our users rely on.
- We decide to add a `format: Literal["vcard-3.0", "vcard-4.0"]` parameter
  to `export_vcard` and need a 4.0 implementation. (Even then, evaluate
  options a/b fresh — the cost/benefit may have shifted.)

## Appendix A: Apple's emitted output (probe results)

Probed 2026-05-09 against macOS 26.3.1. Two contacts were created in the
`MCP-Test` group, exported via
`CNContactVCardSerialization.dataWithContacts_error_([contact], None)`,
captured verbatim, then deleted.

### Year-full birthday

Input fields: `given_name`, `family_name`, `birthday={year:1980, month:5, day:15}`,
mobile phone, work email.

```
BEGIN:VCARD
VERSION:3.0
PRODID:-//Apple Inc.//macOS 26.3.1//EN
N:Probe;YFe378d2;;;
FN:YFe378d2 Probe
EMAIL;type=INTERNET;type=WORK;type=pref:yf@example.com
TEL;type=CELL;type=VOICE;type=pref:+15551234567
BDAY:1980-05-15
END:VCARD
```

### Year-less birthday

Input fields: `given_name`, `family_name`, `birthday={month:5, day:15}` (no year),
mobile phone.

```
BEGIN:VCARD
VERSION:3.0
PRODID:-//Apple Inc.//macOS 26.3.1//EN
N:Probe;YLd6700d;;;
FN:YLd6700d Probe
TEL;type=CELL;type=VOICE;type=pref:+15559999999
BDAY;X-APPLE-OMIT-YEAR=1604:1604-05-15
END:VCARD
```

### Notes from the probe

- **macOS 26.3.1 still emits `VERSION:3.0`.** No surprise upgrade in
  recent macOS.
- **`type=` is lowercase.** That's already 4.0-style syntax, oddly —
  Apple's 3.0 output is closer to 4.0 than the version line suggests.
- **NOTE absent in both contacts.** Neither contact had a NOTE set, but
  the entitlement-gated strip would have hidden it regardless.
- **No `X-APPLE-OMIT-YEAR` cleanup happens at re-import.** Apple
  recognizes its own marker; the year-less semantic is preserved on the
  Apple side.

## See also

- [`contacts-api-gap-analysis.md` §2](./contacts-api-gap-analysis.md#2-capability-matrix) — capability matrix shows vCard 3.0/4.0 emit/parse support per surface.
- [`contacts-api-gap-analysis.md` §4](./contacts-api-gap-analysis.md#4-the-applescript-fallback) — vCard export sample code.
- [`docs/reference/TOOLS.md`](../reference/TOOLS.md) — once #20 lands, the `export_vcard` entry will reproduce the limitations from this doc.
