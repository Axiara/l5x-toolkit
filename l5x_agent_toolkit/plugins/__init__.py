"""
Built-in plugin directory.

Drop ``.py`` files in this directory to have them auto-discovered by the
MCP server at startup.  Each file should contain at least one class that
inherits from :class:`l5x_agent_toolkit.plugin.L5XPlugin`.

Files starting with ``_`` are ignored.
"""
