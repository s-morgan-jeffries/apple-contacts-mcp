"""Security primitives: input sanitization, rate limiting, audit, confirmation.

Stubs for the bootstrap phase. Real implementations land alongside the first
features. See MCP_PLAYBOOK.md §4 for the canonical security checklist and
apple-mail-mcp/src/apple_mail_mcp/security.py for the reference implementation.
"""


def sanitize_input(value: str) -> str:
    """Strip control characters and normalize whitespace from user input.

    Bootstrap stub: returns the input unchanged. Real implementation will
    follow the playbook's two-step pattern (sanitize_input then escape).
    """
    return value
