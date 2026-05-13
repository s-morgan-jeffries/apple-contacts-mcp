# Authorization revocation mid-process — manual test

**Tests issue #37 / gap analysis §6 Q6.** CI cannot exercise TCC revocation
(the macOS TCC database isn't writable from a test runner and the system
preferences UI isn't scriptable), so this is a runbook the maintainer (or
any contributor touching auth code) executes manually after relevant
changes.

The point: every tool must surface `authorization_denied` cleanly when
Contacts access is revoked mid-session — never silently return empty data
or `not_found`.

## Prerequisites

- macOS with Apple Contacts populated (the user's real address book).
- The MCP server running under your usual launcher (Claude Desktop, an
  MCP-aware client, or `uv run apple-contacts-mcp` for raw stdio).
- The launching process already authorized against Contacts at least once
  (so we're starting from `status=authorized`).

## Procedure

### 1. Baseline (still authorized)

Run from the MCP client:

```
list_contacts(limit=10)
```

**Expected:** `success: true`, ~10 contacts returned.

### 2. Revoke access

Open **System Settings → Privacy & Security → Contacts**. Find the
launching process in the list (e.g., "Claude" / "Terminal" / the bundle id
of your runner). Turn the toggle **off**. Do NOT quit or restart the MCP
server.

macOS won't prompt the server. The status change is silent from the
server's perspective until a CN call is made.

### 3. Read-path: empty result post-revocation

```
list_contacts(limit=10)
```

**Expected:** `success: false`, `error_type: "authorization_denied"`,
`status: "denied"`, with the remediation field pointing at System
Settings. **NOT** an empty contacts list, **NOT** `success: true` with
`count: 0`.

### 4. Read-path: not-found masquerade

Use an identifier you know exists (from step 1):

```
get_contact(<some-id-from-step-1>)
```

**Expected:** `authorization_denied`, **NOT** `not_found`. The contact
exists; the server can't see it.

### 5. Destructive-path: confirmation flow

(Skip this step if you're in test mode — the destructive ops bypass
elicitation there. This step assumes `CONTACTS_TEST_MODE` is unset.)

```
delete_contact(<id>)
```

**Expected behavior depends on when revocation is detected:**

- If the entry-check catches it first: `authorization_denied` returned
  immediately, no elicitation prompt shown.
- If the entry-check was already done (rare race window): elicitation
  prompt may show; the post-save check catches the revocation and returns
  `authorization_denied`. The error message mentions "persistence of any
  change is undefined."

**NOT** acceptable: silent success with `success: true` despite the
revocation. **NOT** acceptable: silent no-op claiming success.

### 6. Recovery

Re-enable Contacts access in System Settings (toggle back on). Do NOT
restart the server.

Run `list_contacts(limit=10)` again.

**Expected:** `success: true`, contacts returned. The server recovered
without restart because each tool re-checks status on entry.

## Pass / fail summary

The test passes if **every** step matches the "Expected" outcome. Any
silent-empty / silent-not-found / silent-success result is a failure of
the #37 contract.

## When to run

- After any change to `_verify_authorization_still_granted` (server.py).
- After any change to `_require_contacts_authorization` (server.py).
- After any change to `_run_cn_authorization_status` (contacts_connector.py).
- After upgrading PyObjC, Contacts.framework, or the host macOS major
  version.
- During release-gate review for v0.x.0 versions that touch the auth
  surface.

## Why this is manual

macOS doesn't expose a programmatic way to flip TCC status from outside
System Settings (intentionally, for security). A simulated-revocation
env var (e.g., `CONTACTS_SIMULATE_REVOCATION=true`) was considered and
rejected: it would catch our code's response shape but not the actual CN
silent-failure behavior, which is the thing we're guarding against. The
real CN integration is the only valid signal.
