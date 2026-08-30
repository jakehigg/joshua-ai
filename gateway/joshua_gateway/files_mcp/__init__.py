"""The files MCP: the agent's only file interface, path-confined per person.

``server.build_builtin_server`` builds the in-process ``Server`` that the gateway
hosts as the ``files`` builtin. ``paths`` resolves every path against the
requesting person's root set and rejects any escape.
"""
