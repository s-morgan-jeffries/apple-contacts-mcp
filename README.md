# Apple Contacts MCP

A Model Context Protocol server for Apple Contacts on macOS.

**Version:** v0.4.0 — Infrastructure hardening: per-tool rate limiting, PII-curated audit log, post-call TCC re-verification, FastMCP-elicitation confirmation UX for destructive ops, coverage gate to 95%, PyObjC static safety analyzer, TOOLS.md ↔ `@mcp.tool()` parity check. Same 21 tools as v0.3.0 — no API surface changes. See [docs/reference/TOOLS.md](docs/reference/TOOLS.md) for the API surface and [CHANGELOG.md](CHANGELOG.md) for release notes.

## Tools

- `check_authorization` — TCC status pre-flight.
- `list_contacts` — paged read.
- `get_contact` — full P1 fetch by identifier.
- `search_contacts` — predicate by name, phone, email, or organization.
- `create_contact` — write via `CNSaveRequest`.
- `update_contact` — partial-field update.
- `delete_contact` — out-of-band confirmation via FastMCP elicitation outside test mode; refuses gracefully on clients without elicit support.
- `read_note` / `write_note` — note field via AppleScript fallback (entitlement-gated in CN).
- `read_photo` / `write_photo` — contact photo as base64-encoded bytes; read returns the detected format (JPEG/PNG/HEIC/GIF).
- `list_groups` / `get_contacts_in_group` — group read.
- `add_contact_to_group` / `remove_contact_from_group` — group membership.
- `create_group` / `rename_group` / `delete_group` — group CRUD. `delete_group` uses the same elicitation-confirmation flow as `delete_contact`.
- `export_vcard` / `import_vcard` — vCard 3.0 serialization round-trip.
- `list_containers` — list accounts (iCloud, Gmail, Exchange, On-My-Mac). Pair with `create_contact(..., container_identifier=...)` to write to a non-default container.

## Install

```bash
git clone https://github.com/s-morgan-jeffries/apple-contacts-mcp.git
cd apple-contacts-mcp
uv sync --dev
```

## Usage

After `uv sync --dev`, the server entry point is at `.venv/bin/apple-contacts-mcp`. Add it to your `claude_desktop_config.json` (typically at `~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "apple-contacts": {
      "command": "/absolute/path/to/apple-contacts-mcp/.venv/bin/apple-contacts-mcp",
      "args": []
    }
  }
}
```

The direct venv-binary path is more robust than wrapping in `uv run`, because Claude Desktop spawns MCP servers with a stripped `PATH` that may not see `uv`.

First contact with the Contacts framework will trigger a macOS TCC consent dialog. The dialog reads **"python-3.11 would like to search your contacts"** (or whichever Python version the venv uses) — macOS attaches the grant to the interpreter binary, not to this server's identity. Grant access there, or later in *System Settings → Privacy & Security → Contacts* (look for the "python-3.X" entry). See `check_authorization`'s response shape in [TOOLS.md](docs/reference/TOOLS.md#check_authorization) for the recovery flow if access was denied. Empirical context: [gap-analysis §3](docs/research/contacts-api-gap-analysis.md).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow, branch convention, and PR process. The authoritative reference for project-agnostic best practices is [MCP_PLAYBOOK.md](MCP_PLAYBOOK.md).

## License

MIT — see [LICENSE](LICENSE).
