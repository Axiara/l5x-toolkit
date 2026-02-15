# L5X Agent Toolkit

AI-driven manipulation of Rockwell Automation Studio 5000 L5X project files.

---

## Overview

The L5X Agent Toolkit is a Python library and MCP (Model Context Protocol) server that gives AI assistants -- such as Claude Desktop and Claude Code -- the ability to read, modify, and validate Rockwell Automation PLC project files in the `.L5X` format. Instead of an AI attempting to edit raw XML (which is fragile and error-prone), this toolkit exposes **31 validated tool functions** that produce structurally correct XML every time.

The core design principle is simple: **the AI never touches raw XML**. Every operation -- creating tags, adding rungs, importing components, configuring alarms -- goes through a validated function that understands L5X schema rules, CDATA encoding, dual data format synchronization (L5K and Decorated), and Studio 5000's import requirements.

This means you can describe PLC modifications in plain English ("create a DINT tag called MotorSpeed with a default value of 1750") and the toolkit translates that into byte-perfect L5X XML that Studio 5000 Logix Designer will accept without errors.

## Features

- **31 consolidated MCP tools** covering project management, tag CRUD, program/routine operations, rung manipulation, component import/export, alarm management, validation, and cross-reference analysis
- **Batch operations** -- tag creation, rung manipulation, alarm configuration, and tag updates accept arrays of operations in a single call for efficient bulk changes
- **Dual data format synchronization** -- automatically generates both L5K (compact text) and Decorated (structured XML) representations, keeping them in sync so Studio 5000 does not crash on import
- **L5K data stripping** -- safely removes L5K data when it may be out of sync, and automatically updates the ExportOptions header so Studio 5000 does not expect data that is no longer present
- **Rung text parser and validator** -- tokenizes, parses, and validates Relay Ladder Logic instruction text with full support for branches, nested AOI calls, and tag member/array references
- **Tag substitution engine** -- duplicate rungs with tag name replacements for bulk logic generation (e.g., duplicating conveyor logic for multiple zones)
- **Unified component import/export** -- import and export programs, routines, rungs, tags, AOIs, UDTs, and modules with automatic conflict detection, dependency resolution, and configurable conflict handling (report, skip, overwrite, or fail)
- **Alarm management** -- create and configure ALARM_DIGITAL tags, inspect alarm conditions, and manage DatatypeAlarmDefinitions for UDTs and AOIs
- **Cross-reference analysis** -- find tag references across programs, analyze scope dependencies, compare structured tag instances for duplicates, and detect conflicts like tag shadowing and unused tags
- **Comprehensive validation** -- checks structure, references, naming conventions, rung syntax, AOI timestamps, task scheduling, and data format completeness before writing
- **No external dependencies beyond `lxml`** -- the toolkit uses only `lxml` and the Python standard library (plus `mcp[cli]` for the MCP server)
- **Works with Python 3.9+** on Windows, macOS, and Linux

## Installation

### Prerequisites

- **Python 3.9 or later** (check with `python --version`)
- **pip** (included with Python)

### Step-by-step install

1. Open a terminal (Command Prompt, PowerShell, or your shell of choice).

2. Install the required dependencies:

   ```bash
   pip install lxml "mcp[cli]"
   ```

3. Install the toolkit itself in development mode so you can update it in place:

   ```bash
   pip install -e "C:\Tools\l5x-toolkit"
   ```

   This registers the `l5x-mcp-server` console command and makes the `l5x_agent_toolkit` package importable from anywhere.

4. Verify the installation:

   ```bash
   python -c "from l5x_agent_toolkit import L5XProject; print('OK')"
   ```

## Quick Start

```python
from l5x_agent_toolkit import L5XProject
from l5x_agent_toolkit import tags, programs, validator

# Load an existing project
project = L5XProject(r'C:\Projects\MyMachine.L5X')

# Inspect what is in the project
summary = project.get_project_summary()
print(f"Controller: {project.controller_name}")
print(f"Programs: {summary['program_count']}, Tags: {summary['tag_count']}")

# Create a controller-scope tag
tags.create_tag(project, 'MotorSpeed', 'DINT', description='Speed setpoint RPM')
tags.set_tag_value(project, 'MotorSpeed', 1750)

# Create a program with a default MainRoutine
programs.create_program(project, 'ConveyorControl', description='Zone 1 conveyor')

# Add a rung to MainRoutine
programs.add_rung(
    project, 'ConveyorControl', 'MainRoutine',
    'XIC(StartPB)OTE(MotorRun);',
    comment='Start motor when pushbutton is pressed',
)

# Validate before saving
result = validator.validate_project(project)
if result.is_valid:
    print("Validation passed")
else:
    for err in result.errors:
        print(f"ERROR: {err}")

# Save to a new file
project.write(r'C:\Projects\MyMachine_modified.L5X')
```

## MCP Server Setup

### What is MCP?

The [Model Context Protocol (MCP)](https://modelcontextprotocol.io) is an open standard that lets AI assistants call external tools through a structured JSON-RPC interface over stdio. When you connect the L5X Agent Toolkit as an MCP server, Claude can directly invoke any of the 31 tools -- loading projects, creating tags, adding rungs, validating, and saving -- all through natural language conversation. You describe what you want in plain English, and Claude translates your intent into the correct sequence of tool calls.

### Claude Desktop setup

1. Locate your Claude Desktop configuration file:

   - **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
   - **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

2. Open the file in a text editor and add the L5X server to the `mcpServers` section:

   ```json
   {
     "mcpServers": {
       "l5x-toolkit": {
         "command": "python",
         "args": ["-m", "l5x_agent_toolkit.mcp_server"],
         "env": {}
       }
     }
   }
   ```

   If you have other MCP servers already configured, add the `"l5x-toolkit"` entry alongside them inside the existing `mcpServers` object.

3. Save the file and **restart Claude Desktop** (fully quit and reopen, not just close the window).

4. When Claude Desktop starts, you should see the L5X Agent Toolkit tools listed in the tools panel (the hammer icon).

### Claude Code setup

Run this command in your terminal to register the MCP server with Claude Code:

```bash
claude mcp add l5x-toolkit -- python -m l5x_agent_toolkit.mcp_server
```

Claude Code will automatically start the server when you begin a conversation.

### Verifying the connection

After restarting Claude Desktop (or starting a new Claude Code session), ask Claude:

> "What L5X tools do you have available?"

Claude should list the toolkit tools (load_project, manage_tags, manage_rungs, etc.). If it does not, check:

- That `python` is on your system PATH
- That `pip install -e "C:\Tools\l5x-toolkit"` completed without errors
- That the JSON config file is valid JSON (no trailing commas, etc.)
- The Claude Desktop logs (Help > Open Logs) for error messages

### Example conversation

Here is what a typical interaction looks like once the MCP server is connected:

> **You:** Load my project at C:\Projects\Line4.L5X and tell me what is in it.
>
> **Claude:** *(calls `load_project`)* Loaded: Line4_Controller (1756-L83E, FW 35.11). The project has 12 programs, 847 tags, 6 AOIs, 3 UDTs, and 24 modules.
>
> **You:** Create a new program called PalletizerControl with a description "Palletizer zone logic" and schedule it under MainTask.
>
> **Claude:** *(calls `create_program`, then `schedule_program`)* Done. Created program 'PalletizerControl' with a MainRoutine and scheduled it under MainTask.
>
> **You:** Add a DINT tag called PalletizerState to controller scope, then add a rung in PalletizerControl/MainRoutine that sets it to 1 when StartPB is pressed.
>
> **Claude:** *(calls `manage_tags`, then `manage_rungs`)* Created the tag and added the rung: `XIC(StartPB)MOV(1,PalletizerState);`
>
> **You:** Validate and save to C:\Projects\Line4_updated.L5X
>
> **Claude:** *(calls `validate_project`, then `save_project`)* Validation passed with 0 errors and 0 warnings. Saved to C:\Projects\Line4_updated.L5X.

## MCP Tools Reference

All 31 tools exposed by the MCP server, grouped by category. Every tool returns a string result (JSON for queries, a confirmation message for mutations). Batch tools accept JSON arrays of operations for efficient bulk changes.

### Project Management (5 tools)

| Tool | Description |
|------|-------------|
| `load_project` | Load an L5X file into memory. **Must be called first.** |
| `save_project` | Save the project to an L5X file. Omit path to overwrite the original. |
| `format_project` | Pretty-print the loaded project XML with consistent indentation. |
| `strip_l5k_data` | Remove L5K data from tags, keeping only Decorated format. Automatically updates the ExportOptions header. Use when L5K data may be out of sync with Decorated data. |
| `get_project_summary` | Get counts of programs, tags, AOIs, UDTs, modules, and tasks. |

### Query Tools (3 tools)

| Tool | Description |
|------|-------------|
| `query_project` | Query project contents -- programs, tags, modules, AOIs, UDTs, tasks. Supports entity filtering, glob patterns, pagination (limit/offset), and scope filtering. Replaces the former list_programs, list_routines, list_controller_tags, list_program_tags, list_modules, list_aois, list_udts, list_tasks tools. |
| `get_entity_info` | Get detailed information about a specific tag, AOI, UDT, or rung. Supports optional includes: value, references, alarm_conditions, parameters, members. Replaces the former get_tag_info, find_tag, get_aoi_info, get_udt_info tools. |
| `get_all_rungs` | Get rungs in an RLL routine with their text and comments. Supports pagination with start/count. |

### Tag Operations (2 batch tools)

| Tool | Description |
|------|-------------|
| `manage_tags` | Execute one or more tag CRUD operations in sequence. Actions: create, delete, rename, copy, move, create_alias. Replaces the former create_tag, delete_tag, rename_tag, copy_tag, move_tag, batch_create_tags, create_alias_tag tools. |
| `update_tags` | Set values, member values, and descriptions on one or more tags in a single call. Replaces the former set_tag_value, set_tag_member_value, set_tag_description tools. |

### Program and Routine Operations (5 tools)

| Tool | Description |
|------|-------------|
| `create_program` | Create a new program with a default MainRoutine. |
| `delete_program` | Delete a program and unschedule it from all tasks. |
| `create_routine` | Create a new routine in a program (RLL, ST, FBD, or SFC). |
| `manage_rungs` | Execute one or more rung operations in sequence. Actions: add, delete, modify, duplicate. Automatically adjusts indices for insertions/deletions within the same batch. Replaces the former add_rung, delete_rung, modify_rung_text, set_rung_comment, duplicate_rung_with_substitution tools. |
| `schedule_program` / `unschedule_program` | Schedule or remove a program from a task. |

### Import and Export (4 tools)

| Tool | Description |
|------|-------------|
| `import_component` | Import a component export file (rung, routine, program, AOI, UDT, or module) with conflict detection and configurable resolution (report, skip, overwrite, fail). Replaces the former import_aoi, import_udt, import_module tools. |
| `analyze_import` | Dry-run conflict analysis for a component export file without making changes. |
| `create_export_shell` | Create an empty export shell in memory (rung, routine, or program). Replaces the former create_rung_export, create_routine_export, create_program_export tools. |
| `export_component` | Extract a component (rung, routine, program, tag, UDT, or AOI) into a standalone L5X export file. Replaces the former export_rung, export_routine, export_program, export_tag, export_udt, export_aoi tools. |

### Alarm Management (3 tools)

| Tool | Description |
|------|-------------|
| `manage_alarms` | Create, configure, and inspect alarm tags in a single call. Actions: create_digital, configure_digital, get_info, get_conditions, configure_condition. Replaces the former create_alarm_digital_tag, batch_create_alarm_digital_tags, get_alarm_digital_info, configure_alarm_digital_tag tools. |
| `manage_alarm_definitions` | Manage DatatypeAlarmDefinitions for UDTs and AOIs. Actions: list, create, remove, get. |
| `list_alarms` | List all alarm tags and alarm conditions in the project. Filter by type (digital, analog, condition) and scope. |

### Analysis and Cross-Reference (5 tools)

| Tool | Description |
|------|-------------|
| `find_tag_references` | Find all rungs/routines where a specific tag is referenced. |
| `get_scope_references` | Return all tags and AOI instances referenced within a program or routine scope. Answers "what does this routine touch?" |
| `find_references` | Batch reverse-lookup: find where multiple tags, AOIs, or UDTs are referenced across the project. |
| `get_tag_values` | Get values and metadata for one or more tags in a single call. Supports glob patterns, member expansion, and AOI parameter context. |
| `compare_tag_instances` | Compare structured tag instances to find duplicates across specified members. Works with any data type (AOIs, UDTs, built-in types). |

### Validation and Utilities (4 tools)

| Tool | Description |
|------|-------------|
| `validate_project` | Run all validation checks: structure, references, naming, dependencies, modules, tasks, rung syntax, AOI timestamps, and data format completeness. |
| `analyze_rung_text` | Analyze, validate, or transform rung instruction text. Actions: validate, extract_tags, substitute. Replaces the former validate_rung_syntax, substitute_tags_in_rung, extract_tag_references_from_rung tools. |
| `detect_conflicts` | Detect potential conflicts: tag shadowing, unused tags, scope duplicates. |

## Configuration

### Claude Desktop

Configuration file location:

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

Full example with the L5X toolkit configured:

```json
{
  "mcpServers": {
    "l5x-toolkit": {
      "command": "python",
      "args": ["-m", "l5x_agent_toolkit.mcp_server"],
      "env": {}
    }
  }
}
```

If Python is not on your system PATH, use the full path to the Python executable:

```json
{
  "mcpServers": {
    "l5x-toolkit": {
      "command": "C:\\Python312\\python.exe",
      "args": ["-m", "l5x_agent_toolkit.mcp_server"],
      "env": {}
    }
  }
}
```

### Claude Code

Register the MCP server from any terminal:

```bash
claude mcp add l5x-toolkit -- python -m l5x_agent_toolkit.mcp_server
```

To verify it was added:

```bash
claude mcp list
```

To remove it later:

```bash
claude mcp remove l5x-toolkit
```

## Sharing with Others

To distribute the L5X Agent Toolkit to a coworker:

1. **Copy the folder.** Copy the entire `C:\Tools\l5x-toolkit` folder to their machine at the same path (or any path they prefer).

2. **Install dependencies.** Have them open a terminal and run:

   ```bash
   pip install lxml "mcp[cli]"
   pip install -e "C:\Tools\l5x-toolkit"
   ```

3. **Add the MCP config.** Have them add the `l5x-toolkit` entry to their Claude Desktop config file (see [Configuration](#configuration) above) or run the `claude mcp add` command for Claude Code.

4. **Restart Claude Desktop** (or start a new Claude Code session).

That is all that is needed. There is no license server, no cloud service, and no account required. The toolkit runs entirely locally.

## Project Structure

```
C:\Tools\l5x-toolkit
|-- setup.py                          # Package metadata and entry points
|-- README.md                         # This file
|-- l5x_agent_toolkit/
|   |-- __init__.py                   # Package init with lazy L5XProject import
|   |-- project.py                    # L5XProject class: load, write, query, navigate
|   |-- mcp_server.py                 # MCP server exposing 31 consolidated tools over stdio
|   |-- tags.py                       # Tag CRUD, batch updates, value sync, L5K stripping
|   |-- programs.py                   # Program/routine CRUD and rung operations
|   |-- rungs.py                      # Rung tokenizer, parser, validator, and tag substitution
|   |-- modules.py                    # I/O module list, inspect, import, configure, delete
|   |-- aoi.py                        # AOI import, query, dependency analysis, call generation
|   |-- udt.py                       # UDT import, query, member inspection, dependency analysis
|   |-- component_import.py           # Unified component import with conflict detection
|   |-- component_export.py           # Component export and export shell generation
|   |-- validator.py                  # Pre-flight validation engine (structure, refs, naming, etc.)
|   |-- data_format.py               # L5K and Decorated data format generation and sync
|   |-- accessors.py                  # Tag and data type accessor utilities
|   |-- models.py                     # Data models and type definitions
|   |-- schema.py                     # Constants: base types, built-in structures, instruction catalog
|   |-- utils.py                      # Shared XML helpers: CDATA, deep copy, element ordering
|-- tests/
    |-- __init__.py
    |-- test_alarm.py                 # Alarm tag and alarm definition tests
    |-- test_aoi.py                   # AOI import and query tests
    |-- test_create_tag.py            # Tag creation and data format tests
    |-- test_data_format.py           # L5K/Decorated format generation tests
    |-- test_mcp_server.py            # MCP server integration tests
```

## License / Credits

MIT License. See [LICENSE](LICENSE) for details.

The L5X Agent Toolkit was created by Megan Fox. It uses the [lxml](https://lxml.de/) library for XML processing and the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) for the Model Context Protocol server.

Rockwell Automation, Studio 5000, Logix Designer, and RSLogix 5000 are trademarks of Rockwell Automation, Inc. This toolkit is not affiliated with or endorsed by Rockwell Automation.
