"""FastMCP server for Apple Contacts integration."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from .contacts_connector import ContactsConnector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

mcp: FastMCP = FastMCP("apple-contacts")
connector = ContactsConnector()


_AUTH_REMEDIATION: dict[str, str] = {
    "notDetermined": (
        "Contacts access has not been requested yet. Run a data tool "
        "(e.g. list_contacts) to trigger the system permission prompt, "
        "or grant access manually in System Settings → Privacy & Security "
        "→ Contacts."
    ),
    "denied": (
        "Contacts access was denied. Open System Settings → Privacy & "
        "Security → Contacts and enable access for this server "
        "(macOS will not re-prompt automatically)."
    ),
    "restricted": (
        "Contacts access is locked by parental controls or device "
        "management. Contact your administrator."
    ),
}


@mcp.tool()
def check_authorization() -> dict[str, Any]:
    """Report current TCC authorization status for Contacts access.

    Use this proactively before other tools, and again after any tool
    returns error_type='authorization_denied'. Does not trigger the
    system permission prompt; call list_contacts (or any data tool)
    to do that.

    Returns:
        Always success: True. The status field is one of:
          - "authorized": full access (proceed with any tool)
          - "limited":    macOS 14+ partial access (proceed; some
                          contacts may be hidden)
          - "notDetermined": permission not yet requested
          - "denied":     user explicitly denied
          - "restricted": locked by MDM / parental controls
        When status is not authorized/limited, the response also
        includes a remediation field with copy you can show the user.
    """
    try:
        status = connector._run_cn_authorization_status()
    except Exception as exc:
        logger.error("check_authorization failed: %s", exc)
        return {
            "success": False,
            "error": f"Failed to read TCC status: {exc}",
            "error_type": "unknown",
        }

    response: dict[str, Any] = {"success": True, "status": status}
    if status not in ("authorized", "limited"):
        response["remediation"] = _AUTH_REMEDIATION[status]
    return response


def main() -> None:
    """Start the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
