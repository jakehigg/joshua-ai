"""The engine: the Claude Agent SDK adapter and the warm session pool.

``ConversationManager`` keeps one warm session per active conversation and runs
every turn through it. The agent has no SDK built-in tools; every capability is
an MCP server reached through the gateway, plus the in-process builtins the
manager wires in.
"""
