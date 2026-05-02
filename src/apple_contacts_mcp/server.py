"""FastMCP server for Apple Contacts integration."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from .contacts_connector import ContactsConnector
from .exceptions import ContactsTimeoutError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

mcp: FastMCP = FastMCP("apple-contacts")
connector = ContactsConnector()


_LIST_CONTACTS_MAX = 200


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


def _require_contacts_authorization() -> dict[str, Any] | None:
    """Returns None if access is granted; otherwise an error dict to return.

    Reused by every data tool. Triggers the system permission prompt the
    first time it sees ``notDetermined``.
    """
    try:
        status = connector._run_cn_authorization_status()
        if status == "notDetermined":
            try:
                connector._run_cn_request_access()
            except ContactsTimeoutError:
                return {
                    "success": False,
                    "error": (
                        "Contacts permission prompt is awaiting your "
                        "response. Grant access in the system dialog "
                        "and retry."
                    ),
                    "error_type": "authorization_denied",
                    "status": "notDetermined",
                }
            status = connector._run_cn_authorization_status()
        if status in ("authorized", "limited"):
            return None
        return {
            "success": False,
            "status": status,
            "error": f"Contacts access not granted (status={status}).",
            "error_type": "authorization_denied",
            "remediation": _AUTH_REMEDIATION.get(
                status,
                "Open System Settings → Privacy & Security → Contacts.",
            ),
        }
    except Exception as exc:
        logger.error("authorization check failed: %s", exc)
        return {
            "success": False,
            "error": f"Failed to check TCC status: {exc}",
            "error_type": "unknown",
        }


@mcp.tool()
def list_contacts(offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """List contacts (paged), each with id, given_name, family_name, organization.

    Use ``get_contact(id)`` to fetch full details for a specific contact.
    Use ``search_contacts(query)`` to filter by name. Order is not
    guaranteed.

    Args:
        offset: Number of contacts to skip. Default 0. Must be >= 0.
        limit:  Max contacts to return. Default 50. Capped at 200.

    Returns:
        On success: ``{"success": True, "contacts": [...], "count": N,
        "offset": offset, "limit": effective_limit}``.
        On TCC denial: ``{"success": False, "error_type":
        "authorization_denied", "status": ..., "error": ..., "remediation":
        ...}``.
        On bad input: ``{"success": False, "error_type":
        "validation_error", "error": ...}``.
        On unexpected failure: ``{"success": False, "error_type":
        "unknown", "error": ...}``.
    """
    if offset < 0:
        return {
            "success": False,
            "error": "offset must be >= 0",
            "error_type": "validation_error",
        }
    if limit < 1:
        return {
            "success": False,
            "error": "limit must be >= 1",
            "error_type": "validation_error",
        }

    effective_limit = min(limit, _LIST_CONTACTS_MAX)

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    try:
        contacts = connector._run_cn_enumerate_contacts(
            offset=offset, limit=effective_limit
        )
    except Exception as exc:
        logger.error("list_contacts fetch failed: %s", exc)
        return {
            "success": False,
            "error": f"Fetch failed: {exc}",
            "error_type": "unknown",
        }

    return {
        "success": True,
        "contacts": contacts,
        "count": len(contacts),
        "offset": offset,
        "limit": effective_limit,
    }


@mcp.tool()
def get_contact(identifier: str) -> dict[str, Any]:
    """Fetch a single contact by its CN identifier with all P1 fields.

    Get an identifier from ``list_contacts`` or ``search_contacts``, then
    call this for the full record (name parts, organization, phones,
    emails, postal addresses, urls, birthday).

    Args:
        identifier: The contact's CN identifier (UUID-shaped string).

    Returns:
        On success: ``{"success": True, "contact": {...full P1 fields...}}``.
        On missing identifier (no such contact): ``{"success": False,
        "error_type": "not_found", "error": ...}``.
        On bad input (empty string): ``{"success": False, "error_type":
        "validation_error", "error": ...}``.
        On TCC denial: same shape as ``list_contacts`` (status,
        remediation).
        On unexpected failure: ``{"success": False, "error_type":
        "unknown", "error": ...}``.

    Each entry in the labeled-value families (phones, emails, urls,
    postal_addresses) carries both ``label_raw`` (the
    ``_$!<...>!$_`` token) and ``label`` (the human string).
    """
    if not identifier or not identifier.strip():
        return {
            "success": False,
            "error": "identifier must be a non-empty string",
            "error_type": "validation_error",
        }

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    try:
        contact = connector._run_cn_unified_contact(identifier)
    except Exception as exc:
        logger.error("get_contact fetch failed: %s", exc)
        return {
            "success": False,
            "error": f"Fetch failed: {exc}",
            "error_type": "unknown",
        }

    if contact is None:
        return {
            "success": False,
            "error": f"No contact found with identifier {identifier!r}",
            "error_type": "not_found",
        }

    return {"success": True, "contact": contact}


def main() -> None:
    """Start the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
