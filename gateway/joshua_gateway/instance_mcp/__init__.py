"""The ``instance`` builtin: what this deployment runs, and what GitHub has.

Three read-only tools, hosted in-process by the gateway like ``files``:
``instance_version`` (the build of the three containers),
``instance_check_updates`` (the latest release and the changelog between it
and the running version), and ``instance_addons`` (the ``mcp:`` entries this
instance runs, and the catalog of addons it could add).

It reports and never acts. It reaches two fixed hosts, ``api.github.com`` and
``raw.githubusercontent.com``, only when a tool is called, with no token and
no cache. A guest gets "not available".
"""
