# L5X Agent Toolkit — Plugin Guide

The L5X Agent Toolkit supports **plugins** that add new MCP tools to the server. Plugins let you — or anyone in the community — extend the toolkit with domain-specific functionality without modifying the core codebase.

---

## Table of Contents

- [For Users: Installing and Using Plugins](#for-users-installing-and-using-plugins)
- [For Developers: Building a Plugin](#for-developers-building-a-plugin)
  - [Quick Start](#quick-start)
  - [Plugin Anatomy](#plugin-anatomy)
  - [The PluginContext API](#the-plugincontext-api)
  - [Registering Tools](#registering-tools)
  - [Lifecycle Hooks](#lifecycle-hooks)
  - [Packaging and Distribution](#packaging-and-distribution)
  - [Directory-Based Plugins](#directory-based-plugins)
- [Plugin Ideas](#plugin-ideas)
- [API Reference](#api-reference)
- [FAQ](#faq)

---

## For Users: Installing and Using Plugins

### Installing a Plugin

Plugins are standard Python packages. Install them the same way you install any Python library:

```bash
pip install l5x-plugin-tag-report
```

That's it. The next time the MCP server starts, it will automatically discover and load the plugin.

### Verifying Loaded Plugins

Use the built-in `list_plugins` tool to see what's loaded:

```
> list_plugins
{
  "plugins": [
    {
      "name": "Tag Report",
      "version": "1.0.0",
      "description": "Export tags to CSV, audit naming conventions, ...",
      "source": "entry_point",
      "tools": ["export_tags_csv", "audit_tag_naming", "project_statistics"]
    }
  ],
  "total": 1
}
```

### Uninstalling a Plugin

```bash
pip uninstall l5x-plugin-tag-report
```

The plugin's tools will no longer appear after the server restarts.

---

## For Developers: Building a Plugin

### Quick Start

Here's the minimum needed to create a working plugin:

**1. Create the plugin class** (`my_plugin/plugin.py`):

```python
from l5x_agent_toolkit.plugin import L5XPlugin, PluginContext

class MyPlugin(L5XPlugin):
    name = "My Plugin"
    version = "1.0.0"
    description = "Does something useful."

    def register_tools(self, ctx: PluginContext) -> None:
        @ctx.mcp.tool()
        def my_custom_tool(tag_name: str) -> str:
            """A tool that does something with a tag."""
            prj = ctx.get_project()
            tags = prj.tags.list_controller()
            matching = [t for t in tags if t["name"] == tag_name]
            if not matching:
                return f"Tag '{tag_name}' not found."
            return f"Found tag: {matching[0]}"
```

**2. Declare the entry point** (`pyproject.toml`):

```toml
[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"

[project]
name = "my-l5x-plugin"
version = "1.0.0"
dependencies = ["l5x_agent_toolkit>=0.2.0"]

[project.entry-points."l5x_toolkit.plugins"]
my_plugin = "my_plugin.plugin:MyPlugin"
```

**3. Install and test:**

```bash
pip install -e .
l5x-mcp-server  # your tool appears automatically
```

### Plugin Anatomy

Every plugin is a Python class that inherits from `L5XPlugin`:

```python
from l5x_agent_toolkit.plugin import L5XPlugin, PluginContext

class MyPlugin(L5XPlugin):
    # --- Metadata (shown in list_plugins output) ---
    name = "My Plugin"              # Required: human-readable name
    version = "1.0.0"              # Required: semver string
    description = "What it does."   # Optional: one-line summary

    # --- Required: register your MCP tools ---
    def register_tools(self, ctx: PluginContext) -> None:
        ...

    # --- Optional: lifecycle hooks ---
    def on_project_loaded(self, ctx: PluginContext) -> None:
        ...  # Called each time load_project succeeds

    def on_project_saved(self, ctx: PluginContext) -> None:
        ...  # Called each time save_project succeeds
```

### The PluginContext API

The `PluginContext` object is your gateway to the toolkit. It provides:

| Attribute | Type | Description |
|-----------|------|-------------|
| `ctx.get_project()` | `Callable[[], L5XProject]` | Returns the loaded project. Raises `RuntimeError` if nothing is loaded. |
| `ctx.get_project_path()` | `Callable[[], Optional[str]]` | Returns the file path of the loaded project, or `None`. |
| `ctx.mcp` | `FastMCP` | The MCP server instance. Use `@ctx.mcp.tool()` to register tools. |
| `ctx.toolkit_version` | `str` | Installed toolkit version (e.g. `"0.2.0"`). |

### Registering Tools

Register tools inside `register_tools()` using the `@ctx.mcp.tool()` decorator — the same mechanism the core toolkit uses:

```python
def register_tools(self, ctx: PluginContext) -> None:

    @ctx.mcp.tool()
    def my_tool(param1: str, param2: int = 10) -> str:
        """Tool description shown to the AI client.

        Args:
            param1: What this parameter does.
            param2: Another parameter with a default.
        """
        prj = ctx.get_project()
        # ... your logic here ...
        return json.dumps({"result": "success"})
```

**Key rules for tool functions:**

- **Return strings.** All MCP tools return strings. Use `json.dumps()` for structured data.
- **Call `ctx.get_project()`** inside the tool function (not at registration time) so it always gets the currently loaded project.
- **Write clear docstrings.** The docstring becomes the tool description the AI client sees. Good descriptions = better AI usage.
- **Use type hints on parameters.** They become the tool's input schema.
- **Handle errors gracefully.** Return error messages as strings rather than raising exceptions — a raised exception will surface as an MCP error.

### Accessing Project Data

The `L5XProject` object returned by `ctx.get_project()` provides sub-accessors:

```python
prj = ctx.get_project()

# Tags
ctrl_tags = prj.tags.list_controller()          # list of dicts
prog_tags = prj.tags.list_program("MainProgram") # list of dicts
tag_el = prj.tags.get_controller_tag_element("MyTag")  # lxml Element
value = prj.tags.get_value("MyTag", "controller")

# Programs / Routines / Rungs
programs = prj.programs.list_all()               # list of strings
routines = prj.programs.list_routines("MainProgram")
rungs = prj.programs.list_rungs("MainProgram", "MainRoutine")

# Data Types (UDTs, AOIs, Modules, Tasks)
udts = prj.types.list_udts()
aois = prj.types.list_aois()
modules = prj.types.list_modules()
tasks = prj.types.list_tasks()

# Cross-Reference Analysis
refs = prj.analysis.find_tag_references("MyTag")
unused = prj.analysis.find_unused_tags()
shadows = prj.analysis.detect_tag_shadowing()

# Project metadata
name = prj.controller_name
processor = prj.processor_type
summary = prj.get_project_summary()
```

You can also import and use the toolkit's backing modules directly:

```python
from l5x_agent_toolkit import tags, programs, rungs, validator
from l5x_agent_toolkit.models import Scope, RoutineType, TagInfo
from l5x_agent_toolkit.schema import BASE_DATA_TYPES, INSTRUCTION_CATALOG
from l5x_agent_toolkit.utils import deep_copy, validate_tag_name
```

### Lifecycle Hooks

Override these optional methods to react to project events:

```python
def on_project_loaded(self, ctx: PluginContext) -> None:
    """Called after load_project succeeds."""
    prj = ctx.get_project()
    # Build caches, validate prerequisites, etc.

def on_project_saved(self, ctx: PluginContext) -> None:
    """Called after save_project succeeds."""
    # Update external systems, log changes, etc.
```

Lifecycle hooks are called inside a try/except — a failing hook will be logged but won't crash the server or affect other plugins.

### Packaging and Distribution

**Recommended structure:**

```
my-l5x-plugin/
    pyproject.toml
    README.md
    my_plugin/
        __init__.py
        plugin.py          # contains your L5XPlugin subclass
```

**Entry point declaration** (the critical part):

```toml
[project.entry-points."l5x_toolkit.plugins"]
my_plugin = "my_plugin.plugin:MyPlugin"
```

The entry point group **must** be `l5x_toolkit.plugins`. The key (`my_plugin`) is an arbitrary identifier. The value points to your `L5XPlugin` subclass using `module.path:ClassName` syntax.

**Publishing to PyPI:**

```bash
pip install build twine
python -m build
twine upload dist/*
```

Users then install with `pip install my-l5x-plugin`.

### Directory-Based Plugins

For quick prototyping or personal-use plugins, you can drop `.py` files directly into the toolkit's `plugins/` directory:

```
l5x_agent_toolkit/
    plugins/
        __init__.py
        my_quick_tool.py   # <-- auto-discovered
```

The server scans this directory at startup and loads any `L5XPlugin` subclass it finds. Files starting with `_` are ignored.

This approach is simpler but has trade-offs:
- No version management
- Tightly coupled to the toolkit installation
- Not distributable via pip

**Use entry points for anything you plan to share.**

---

## Plugin Ideas

Here are some ideas for plugins that would be useful to the broader automation community:

| Plugin Idea | Description |
|-------------|-------------|
| **Tag Report** | Export tags to CSV, audit naming conventions, project statistics *(included as example)* |
| **IO List Generator** | Extract module/point mappings to a spreadsheet-ready format |
| **Rung Complexity Analyzer** | Score routines by cyclomatic complexity, flag overly complex rungs |
| **Standards Checker** | Validate against ISA-18.2 (alarms), ISA-88 (batch), or company standards |
| **Migration Helper** | Compare two L5X files, generate a diff report, detect deprecated instructions |
| **Template Scaffolder** | Generate boilerplate programs/tags from device templates (e.g. "add a VFD") |
| **Documentation Generator** | Auto-generate Markdown/HTML docs from project structure and descriptions |
| **Tag Value Snapshot** | Save/restore tag value snapshots for testing or commissioning |
| **Cross-Reference Report** | Generate detailed reports of where every tag is used |
| **Duplicate Detection** | Find copy-paste patterns, identical routines, and redundant logic |

---

## API Reference

### `L5XPlugin` (base class)

| Member | Type | Description |
|--------|------|-------------|
| `name` | `str` | Plugin display name (must be unique across loaded plugins) |
| `version` | `str` | Semver version string |
| `description` | `str` | One-line summary |
| `register_tools(ctx)` | method | **Required.** Register MCP tools here. |
| `on_project_loaded(ctx)` | method | Optional. Called after each `load_project`. |
| `on_project_saved(ctx)` | method | Optional. Called after each `save_project`. |

### `PluginContext`

| Attribute | Type | Description |
|-----------|------|-------------|
| `get_project` | `Callable[[], L5XProject]` | Get the loaded project or raise `RuntimeError` |
| `get_project_path` | `Callable[[], Optional[str]]` | Get the loaded project's file path |
| `mcp` | `FastMCP` | The server instance — register tools with `@ctx.mcp.tool()` |
| `toolkit_version` | `str` | Installed toolkit version |

### `list_plugins` (built-in MCP tool)

Returns JSON with all loaded plugins, their versions, and registered tool names.

---

## FAQ

**Q: Can a plugin modify the project XML directly?**
A: Yes — `ctx.get_project()` gives you full access to the `L5XProject` object, including the underlying lxml tree via `prj.root`. However, we recommend using the toolkit's higher-level APIs (tag/program/rung operations) whenever possible, as they handle data format synchronization and element ordering automatically.

**Q: What happens if two plugins register a tool with the same name?**
A: The second tool will overwrite the first, and a warning will be logged. Use distinctive tool name prefixes (e.g. `mycompany_report_tags`) to avoid collisions.

**Q: Can I depend on a specific toolkit version?**
A: Yes. Declare it in your `pyproject.toml`:
```toml
dependencies = ["l5x_agent_toolkit>=0.2.0,<1.0.0"]
```

**Q: How do I test my plugin?**
A: Load a test project, construct a `PluginContext`, and call your tools directly:
```python
from l5x_agent_toolkit.plugin import PluginContext, PluginRegistry
from l5x_agent_toolkit import mcp_server

# Load a test project
mcp_server.load_project("test.L5X")

# Build context and register your plugin
ctx = mcp_server._build_plugin_context()
registry = PluginRegistry()
registry.register_plugin(MyPlugin(), ctx)

# Call your tool functions through the MCP tool manager
tools = mcp_server.mcp._tool_manager._tools
result = tools["my_tool"].fn(param1="value")
```

See `tests/test_plugins.py` and the example plugin in `examples/plugins/l5x_plugin_tag_report/` for complete working examples.

**Q: What Python versions are supported?**
A: Python 3.9+ (same as the core toolkit). Entry point discovery adapts automatically to the Python version's `importlib.metadata` API.

**Q: Can plugins add resources or prompts (not just tools)?**
A: The current plugin API focuses on tools, which is the primary MCP integration point. If you need to register MCP resources or prompts, you have direct access to the `ctx.mcp` FastMCP server instance and can use its full API.
