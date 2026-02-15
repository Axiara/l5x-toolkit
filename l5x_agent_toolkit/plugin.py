"""
Plugin architecture for the L5X Agent Toolkit MCP server.

Allows third-party packages to register additional MCP tools that operate
on the loaded L5X project.  Plugins are discovered automatically via
Python entry points (``l5x_toolkit.plugins`` group) and can also be
loaded from a local ``plugins/`` directory.

Quick start for plugin authors
------------------------------

1. Create a class that inherits from :class:`L5XPlugin`.
2. Implement :meth:`register_tools` to add ``@mcp.tool()`` functions.
3. Declare an entry point in your package's ``pyproject.toml``::

       [project.entry-points."l5x_toolkit.plugins"]
       my_plugin = "my_package.plugin:MyPlugin"

4. ``pip install`` your package alongside the toolkit — done.

See ``examples/plugins/tag_report/`` for a complete working example.
"""

from __future__ import annotations

import importlib
import logging
import pathlib
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from .project import L5XProject

logger = logging.getLogger(__name__)


# ===================================================================
# Plugin context — the stable API surface exposed to plugins
# ===================================================================

@dataclass
class PluginContext:
    """Stable interface that every plugin receives.

    Plugins should use this object to interact with the toolkit rather
    than reaching into internal modules directly.  This lets the core
    evolve without breaking plugins.

    Attributes:
        get_project:  Callable that returns the currently loaded
                      :class:`L5XProject`, or raises ``RuntimeError``
                      if no project is loaded.
        get_project_path:  Callable that returns the filesystem path of
                           the loaded project (or ``None``).
        mcp:          The ``FastMCP`` server instance — call
                      ``@ctx.mcp.tool()`` to register new tools.
        toolkit_version:  Semantic version string of the installed
                          toolkit (e.g. ``"0.1.0"``).
    """

    get_project: Callable[[], "L5XProject"]
    get_project_path: Callable[[], Optional[str]]
    mcp: "FastMCP"
    toolkit_version: str = ""


# ===================================================================
# Base class every plugin must inherit from
# ===================================================================

class L5XPlugin(ABC):
    """Base class for L5X Agent Toolkit plugins.

    Subclass this and implement :meth:`register_tools` to add your own
    MCP tools.  The server calls your plugin at startup, passing a
    :class:`PluginContext` with everything you need.

    Example::

        class MyPlugin(L5XPlugin):
            name = "My Cool Plugin"
            version = "1.0.0"
            description = "Adds tag-report tools."

            def register_tools(self, ctx: PluginContext) -> None:
                @ctx.mcp.tool()
                def my_custom_tool(tag_name: str) -> str:
                    prj = ctx.get_project()
                    # ... do something with the project ...
                    return "result"
    """

    # ------ metadata (override in subclasses) ------

    name: str = "Unnamed Plugin"
    """Human-readable plugin name."""

    version: str = "0.0.0"
    """Semver string for the plugin itself."""

    description: str = ""
    """One-line summary shown in ``list_plugins`` output."""

    # ------ lifecycle ------

    @abstractmethod
    def register_tools(self, ctx: PluginContext) -> None:
        """Register MCP tools with the server.

        Called once at server startup.  Use ``ctx.mcp.tool()`` as a
        decorator to add tools, and ``ctx.get_project()`` inside those
        tools to access the loaded L5X project.

        Args:
            ctx: The :class:`PluginContext` providing access to the
                 MCP server, the loaded project, and toolkit metadata.
        """
        ...

    def on_project_loaded(self, ctx: PluginContext) -> None:
        """Optional hook called each time a project is loaded.

        Override this if your plugin needs to build caches or perform
        setup when a new project file is opened.  The default
        implementation does nothing.

        Args:
            ctx: The :class:`PluginContext` (project is guaranteed to be
                 loaded when this is called).
        """

    def on_project_saved(self, ctx: PluginContext) -> None:
        """Optional hook called each time a project is saved.

        Override this if your plugin needs to perform post-save actions.
        The default implementation does nothing.

        Args:
            ctx: The :class:`PluginContext`.
        """


# ===================================================================
# Plugin registry — keeps track of loaded plugins
# ===================================================================

@dataclass
class _PluginRecord:
    """Internal record for a loaded plugin."""
    plugin: L5XPlugin
    source: str  # "entry_point", "directory", or "manual"
    tools_registered: list[str] = field(default_factory=list)


class PluginRegistry:
    """Discovers, loads, and manages plugins.

    This is a singleton-style registry created by the MCP server at
    startup.  It is not intended to be instantiated by plugin authors.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, _PluginRecord] = {}

    # ------ public queries ------

    @property
    def loaded_plugins(self) -> dict[str, _PluginRecord]:
        """Return a read-only view of loaded plugins keyed by name."""
        return dict(self._plugins)

    def get_plugin(self, name: str) -> Optional[L5XPlugin]:
        """Retrieve a loaded plugin by name, or ``None``."""
        rec = self._plugins.get(name)
        return rec.plugin if rec else None

    # ------ discovery & loading ------

    def discover_and_load(self, ctx: PluginContext) -> list[str]:
        """Discover plugins from entry points and local directory.

        Returns a list of successfully loaded plugin names.
        """
        loaded: list[str] = []
        loaded.extend(self._load_entry_point_plugins(ctx))
        loaded.extend(self._load_directory_plugins(ctx))
        return loaded

    def register_plugin(
        self,
        plugin: L5XPlugin,
        ctx: PluginContext,
        source: str = "manual",
    ) -> bool:
        """Register and initialise a single plugin instance.

        Returns ``True`` if the plugin was loaded successfully.
        """
        name = plugin.name
        if name in self._plugins:
            logger.warning(
                "Plugin %r is already loaded — skipping duplicate from %s",
                name, source,
            )
            return False

        # Snapshot the tool list *before* registration so we can diff.
        tools_before = set(ctx.mcp._tool_manager._tools.keys())

        try:
            plugin.register_tools(ctx)
        except Exception:
            logger.exception("Plugin %r failed during register_tools", name)
            return False

        tools_after = set(ctx.mcp._tool_manager._tools.keys())
        new_tools = sorted(tools_after - tools_before)

        # Check for tool name collisions with previously registered plugins
        for tool_name in new_tools:
            for existing_name, existing_rec in self._plugins.items():
                if tool_name in existing_rec.tools_registered:
                    logger.warning(
                        "Tool %r from plugin %r collides with plugin %r",
                        tool_name, name, existing_name,
                    )

        self._plugins[name] = _PluginRecord(
            plugin=plugin,
            source=source,
            tools_registered=new_tools,
        )

        logger.info(
            "Loaded plugin %r v%s (%s) — %d tool(s): %s",
            name, plugin.version, source, len(new_tools),
            ", ".join(new_tools) or "(none)",
        )
        return True

    # ------ lifecycle event dispatchers ------

    def notify_project_loaded(self, ctx: PluginContext) -> None:
        """Call ``on_project_loaded`` on all registered plugins."""
        for name, rec in self._plugins.items():
            try:
                rec.plugin.on_project_loaded(ctx)
            except Exception:
                logger.exception(
                    "Plugin %r raised in on_project_loaded", name,
                )

    def notify_project_saved(self, ctx: PluginContext) -> None:
        """Call ``on_project_saved`` on all registered plugins."""
        for name, rec in self._plugins.items():
            try:
                rec.plugin.on_project_saved(ctx)
            except Exception:
                logger.exception(
                    "Plugin %r raised in on_project_saved", name,
                )

    # ------ internals ------

    def _load_entry_point_plugins(self, ctx: PluginContext) -> list[str]:
        """Load plugins declared as ``l5x_toolkit.plugins`` entry points."""
        loaded: list[str] = []
        try:
            if sys.version_info >= (3, 12):
                from importlib.metadata import entry_points
                eps = entry_points(group="l5x_toolkit.plugins")
            else:
                # Python 3.9-3.11 compatibility
                from importlib.metadata import entry_points as _ep
                all_eps = _ep()
                if isinstance(all_eps, dict):
                    eps = all_eps.get("l5x_toolkit.plugins", [])
                else:
                    eps = all_eps.select(group="l5x_toolkit.plugins")
        except Exception:
            logger.debug("No entry-point plugins found (or metadata error).")
            return loaded

        for ep in eps:
            try:
                plugin_cls = ep.load()
                if not (isinstance(plugin_cls, type) and
                        issubclass(plugin_cls, L5XPlugin)):
                    logger.warning(
                        "Entry point %r does not point to an L5XPlugin "
                        "subclass — skipping.", ep.name,
                    )
                    continue
                plugin = plugin_cls()
                if self.register_plugin(plugin, ctx, source="entry_point"):
                    loaded.append(plugin.name)
            except Exception:
                logger.exception(
                    "Failed to load entry-point plugin %r", ep.name,
                )
        return loaded

    def _load_directory_plugins(self, ctx: PluginContext) -> list[str]:
        """Load plugins from ``<package_dir>/plugins/*.py``."""
        loaded: list[str] = []
        plugin_dir = pathlib.Path(__file__).parent / "plugins"
        if not plugin_dir.is_dir():
            return loaded

        for path in sorted(plugin_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            module_name = f"l5x_agent_toolkit.plugins.{path.stem}"
            try:
                mod = importlib.import_module(module_name)
            except Exception:
                logger.exception("Failed to import plugin module %s", path)
                continue

            # Look for an ``L5XPlugin`` subclass in the module.
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if (isinstance(obj, type)
                        and issubclass(obj, L5XPlugin)
                        and obj is not L5XPlugin):
                    try:
                        plugin = obj()
                        if self.register_plugin(
                            plugin, ctx, source="directory",
                        ):
                            loaded.append(plugin.name)
                    except Exception:
                        logger.exception(
                            "Failed to instantiate plugin class %s from %s",
                            attr_name, path,
                        )
        return loaded
