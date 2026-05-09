# Apple Contacts MCP

A Model Context Protocol server for Apple Contacts on macOS.

**Version:** v0.2.0 — adds field-scoped search, group read/write, note read/write (AppleScript fallback), and vCard import/export on top of the v0.1.0 CRUD core. See [docs/reference/TOOLS.md](docs/reference/TOOLS.md) for the API surface and [CHANGELOG.md](CHANGELOG.md) for release notes.

## Tools

- `check_authorization` — TCC status pre-flight.
- `list_contacts` — paged read.
- `get_contact` — full P1 fetch by identifier.
- `search_contacts` — predicate by name, phone, email, or organization.
- `create_contact` — write via `CNSaveRequest`.
- `update_contact` — partial-field update.
- `delete_contact` — test-mode-only in v0.2.0; full destructive UX ships in v0.4.0.
- `read_note` / `write_note` — note field via AppleScript fallback (entitlement-gated in CN).
- `list_groups` / `get_contacts_in_group` — group read.
- `add_contact_to_group` / `remove_contact_from_group` — group membership.
- `export_vcard` / `import_vcard` — vCard 3.0 serialization round-trip.

## Install

```bash
git clone https://github.com/s-morgan-jeffries/apple-contacts-mcp.git
cd apple-contacts-mcp
uv sync --dev
```

## Usage

The server entry point is registered as `apple-contacts-mcp` (see `pyproject.toml [project.scripts]`). Configure it in Claude Desktop's MCP settings to expose the tool list above.

First run will trigger the system TCC permission prompt. Grant access in System Settings → Privacy & Security → Contacts. See `check_authorization`'s response shape in [TOOLS.md](docs/reference/TOOLS.md#check_authorization) for the recovery flow if access was denied.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow, branch convention, and PR process. The authoritative reference for project-agnostic best practices is [MCP_PLAYBOOK.md](MCP_PLAYBOOK.md).

## License

MIT — see [LICENSE](LICENSE).
