# Label translation table — decision

**Resolved:** 2026-05-09
**Closes:** [contacts-api-gap-analysis.md §6 Q4](./contacts-api-gap-analysis.md#6-open-empirical-questions)
**Tracking issue:** #22
**Last issue in v0.2.0** — once this lands, the milestone ships.

## Question

`Contacts.framework` emits raw label tokens like `_$!<Mobile>!$_` and
`_$!<HomeFAX>!$_` for the labeled multi-valued fields (phones, emails,
URLs, postal addresses). The read side translates them to human strings
via `CNLabeledValue.localizedStringForLabel:`. The **write side** has no
inverse — `create_contact` and `update_contact` previously required the
caller to pass the raw `_$!<...>!$_` token in `label_raw`. That's
ergonomically hostile to LLM callers, who naturally want to say
`"mobile"` or `"home fax"`.

Should the MCP layer accept human forms on input and translate to Apple's
tokens internally?

## Decision

**Yes.** Implement a 12-entry English bidirectional table (one direction
new; the other already exists via Apple's `localizedStringForLabel:`).
The write-side input contract changes:

- The input field is renamed from `label_raw` to **`label`** (single
  field, breaking change vs v0.1.0; pre-1.0 so acceptable).
- Three input forms are accepted, all routed through
  `apple_contacts_mcp.utils.label_to_apple_token`:
  1. **Human form** (`"mobile"`, `"home fax"`, `"iPhone"`,
     case-insensitive) — translated to Apple's token.
  2. **Apple token** (`"_$!<Mobile>!$_"`) — passed through unchanged.
     Apple stores it as the built-in label.
  3. **Custom string** (`"Spotify"`, `"WhatsApp"`) — passed through
     unchanged. Apple stores it as a custom label.

The read-side response shape is **unchanged** — `get_contact` still emits
both `label_raw` (token, identity) and `label` (Apple's localized form,
display).

## Empirical evidence

Probed against macOS 26.3.1 by calling
`CNLabeledValue.localizedStringForLabel:` against the suspected token
catalog. The complete set of Apple-recognized built-in tokens (those
that translate to a different string on the read side):

| Apple token | Human form | Used by |
|---|---|---|
| `_$!<Mobile>!$_` | `mobile` | phones, emails, urls, postal |
| `_$!<Work>!$_` | `work` | phones, emails, urls, postal |
| `_$!<Home>!$_` | `home` | phones, emails, urls, postal |
| `_$!<Other>!$_` | `other` | phones, emails, urls, postal |
| `_$!<iPhone>!$_` | `iPhone` (capital P) | phones |
| `_$!<Main>!$_` | `main` | phones |
| `_$!<HomeFAX>!$_` | `home fax` | phones |
| `_$!<WorkFAX>!$_` | `work fax` | phones |
| `_$!<OtherFAX>!$_` | `other fax` | phones |
| `_$!<Pager>!$_` | `pager` | phones |
| `_$!<School>!$_` | `school` | emails, urls |
| `_$!<HomePage>!$_` | `homepage` | urls |

**12 entries.** Strings that look like tokens but aren't recognized
(probed `_$!<Personal>!$_`, `_$!<AppleWatch>!$_`) come back from
`localizedStringForLabel:` unchanged — Apple treats them as custom
labels, not as built-ins.

Tokens for `relatedNames` and `dates` (anniversary, parent, friend,
etc.) also exist but those entity types are deferred to v0.3.0 (#27);
not in scope for this PR.

## Why a single-field input contract over keeping `label_raw`

Three options were considered:

1. **Keep `label_raw`, broaden semantics** — non-breaking; field name
   stays misleading once the field accepts human forms.
2. **Add `label`, keep `label_raw` as alias** — non-breaking; two ways
   to do the same thing.
3. **Replace `label_raw` with `label`** — breaking; one canonical input
   field; consistent with the read-side `label` echo.

(3) won. Pre-1.0, breaking the v0.1.0 input shape is fine; the alternative
is API friction that lasts forever. The output side still echoes
`label_raw` for round-trip identity, so callers who need
locale-independent identity on read have it.

## Why English-only on input

`CNLabeledValue.localizedStringForLabel:` returns locale-dependent
strings — `"mobile"` in en, `"mobil"` in de, `"téléphone portable"` in
fr. Accepting non-English forms on input would couple the write contract
to the system's current locale, which is brittle and unpredictable for
LLM callers (the LLM doesn't know the system locale).

The decision: **accept English forms only**. Non-English human forms
(`"mobil"`, `"téléphone portable"`) fall through to the custom-label
pass-through, preserving them on save but not translating them to
Apple's built-in label. Documented as a limitation in the tool
docstrings.

If a future user reports a real need for non-English input forms, the
table can be expanded with locale prefixes (`"de:mobil"`,
`"fr:portable"`) or made per-locale via `NSLocale`. Not worth doing
preemptively.

## Why no reverse helper

Apple's `localizedStringForLabel:` already translates token → human
display on the read side. We use it as-is — no reverse helper needed in
`utils.py`. The connector's `_serialize_labeled_values` calls it for
every entry, producing the `label` field in `get_contact` output.

## Custom labels

Custom labels (`"Spotify"`, `"WhatsApp"`, anything not in the table and
not a `_$!<...>!$_` token) **pass through unchanged** to Contacts.app,
which stores them as custom labels. This matches Apple's CN behavior:
`CNLabeledValue.labeledValueWithLabel:value:` accepts any string as a
label.

We do **not** validate or reject custom labels. Rejecting them would
prevent users from storing legitimate custom labels (e.g., per-app
contact tags), which is paternalistic and contradicts Apple's data
model.

## Implementation summary

- `apple_contacts_mcp.utils.label_to_apple_token(label: str) -> str`
  is the single function. 12-entry lowercase keyed dict for built-ins;
  pass-through for everything else; case- and whitespace-insensitive
  lookup on the human form.
- Connector calls the helper in `_build_mutable_contact` and
  `_apply_update_fields` for each labeled-field block (phones, emails,
  urls, postal_addresses).
- The read serializer (`_serialize_labeled_values`) is unchanged.

## When to revisit

Re-open this decision (file a follow-up issue and supersede this doc) if
**any** of these is true:

- A user files an issue describing a concrete cross-locale flow that
  requires non-English input forms.
- Apple ships new built-in label tokens (e.g., as part of macOS 27+)
  that aren't in the table. Re-probe and extend.
- v0.3.0's `relatedNames` / `dates` work (#27) needs a parallel table.
  At that point, consider whether to merge into one universal helper or
  keep separate per-field tables.

## See also

- [`contacts-api-gap-analysis.md` §1](./contacts-api-gap-analysis.md#1-overview) — capability matrix mentions raw `_$!<Home>!$_` tokens.
- [`docs/reference/TOOLS.md`](../reference/TOOLS.md) — `create_contact` and `update_contact` parameter sections document the new `label` input field.
- [`apple_contacts_mcp.utils.label_to_apple_token`](../../src/apple_contacts_mcp/utils.py) — the helper itself.
