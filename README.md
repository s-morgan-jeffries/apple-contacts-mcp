# Apple Contacts MCP

A Model Context Protocol server for Apple Contacts on macOS.

> **Status:** bootstrap phase. No tools shipped yet — see [BOOTSTRAP.md](BOOTSTRAP.md) and [INITIAL_ISSUES.md](INITIAL_ISSUES.md) (TBD) for the v0.1.0 roadmap. The first feature lands once Phase 0 API research is complete.

## Install

```bash
git clone https://github.com/s-morgan-jeffries/apple-contacts-mcp.git
cd apple-contacts-mcp
uv sync --dev
```

## Usage

TBD. The server entry point is registered as `apple-contacts-mcp` (see `pyproject.toml [project.scripts]`). Once tools land, configure in Claude Desktop's MCP settings.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow, branch convention, and PR process. The authoritative reference for project-agnostic best practices is [MCP_PLAYBOOK.md](MCP_PLAYBOOK.md).

## License

MIT — see [LICENSE](LICENSE).
