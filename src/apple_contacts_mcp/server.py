"""FastMCP server for Apple Contacts integration."""

import logging

from fastmcp import FastMCP

from .contacts_connector import ContactsConnector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

mcp: FastMCP = FastMCP("apple-contacts")
connector = ContactsConnector()


def main() -> None:
    """Start the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
