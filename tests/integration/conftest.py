"""Integration-test conftest.

The session fixtures (`real_connector`, `test_group`, `tmp_contact`,
`integration_env`) live in the top-level `tests/conftest.py` so the
benchmark suite under `tests/benchmarks/` can request them too without
hitting pytest 9's "pytest_plugins outside top-level conftest" restriction
or the double-registration that happened when both auto-discovery and
`pytest_plugins` tried to load this module.

Lifecycle (copied for reference from the original integration conftest):
1. `real_connector` checks TCC status; skips the session on denied/restricted.
2. `test_group` finds-or-creates the `MCP-Test` group via raw CN calls.
3. `integration_env` sets `CONTACTS_TEST_MODE=true` + `CONTACTS_TEST_GROUP=MCP-Test`.
4. `tmp_contact` (function-scoped) creates a contact in the test group,
   yields its identifier, deletes on teardown.
5. Session teardown removes every contact in `MCP-Test` then the group itself.

Risks:
- **Pre-existing `MCP-Test` group**: if you happen to have a real group by
  that exact name with real members, the session teardown will delete them.
  Choose a different `CONTACTS_TEST_GROUP` name if so.
- **Crashed test process**: leaked contacts in `MCP-Test` are cleaned up on
  the next session (find-or-create + cleanup).
"""
