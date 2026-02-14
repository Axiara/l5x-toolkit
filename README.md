# L5X Agent Toolkit

AI-driven manipulation of Rockwell Automation Studio 5000 L5X project files.

---

## Overview

The L5X Agent Toolkit is a Python library and MCP (Model Context Protocol) server that gives AI assistants -- such as Claude Desktop and Claude Code -- the ability to read, modify, and validate Rockwell Automation PLC project files in the `.L5X` format. Instead of an AI attempting to edit raw XML (which is fragile and error-prone), this toolkit exposes **42 validated tool functions** that produce structurally correct XML every time.

The core design principle is simple: **the AI never touches raw XML**. Every operation -- creating tags, adding rungs, importing Add-On Instructions, configuring modules -- goes through a validated function that understands L5X schema rules, CDATA encoding, dual data format synchronization (L5K and Decorated), and Studio 5000's import requirements.

This means you can describe PLC modifications in plain English ("create a DINT tag called MotorSpeed with a default value of 1750") and the toolkit translates that into byte-perfect L5X XML that Studio 5000 Logix Designer will accept without errors.

## Features

- **42 MCP tools** covering project management, tag CRUD, program/routine operations, rung manipulation, AOI/UDT import, module configuration, validation, and analysis
- **Dual data format synchronization** -- automatically generates both L5K (compact text) and Decorated (structured XML) representations, keeping them in sync so Studio 5000 does not crash on import
- **Rung text parser and validator** -- tokenizes, parses, and validates Relay Ladder Logic instruction text with full support for branches, nested AOI calls, and tag member/array references
- **Tag substitution engine** -- duplicate rungs with tag name replacements for bulk logic generation (e.g., duplicating conveyor logic for multiple zones)
- **AOI and UDT import with dependency resolution** -- automatically imports transitive dependencies (UDTs referenced by AOIs, UDTs referenced by other UDTs) and updates EditedDate timestamps
- **Module import from templates** -- import I/O module definitions from template L5X files with configurable name, address, slot, and parent module
- **Comprehensive validation** -- checks structure, references, naming conventions, rung syntax, AOI timestamps, task scheduling, and data format completeness before writing
- **Cross-reference search** -- find every rung and routine where a tag is used
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
from l5x_agent_toolkit import tags, programs, rungs, validator

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

The [Model Context Protocol (MCP)](https://modelcontextprotocol.io) is an open standard that lets AI assistants call external tools through a structured JSON-RPC interface over stdio. When you connect the L5X Agent Toolkit as an MCP server, Claude can directly invoke any of the 42 tools -- loading projects, creating tags, adding rungs, validating, and saving -- all through natural language conversation. You describe what you want in plain English, and Claude translates your intent into the correct sequence of tool calls.

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

Claude should list the toolkit tools (load_project, create_tag, add_rung, etc.). If it does not, check:

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
> **Claude:** *(calls `create_tag`, then `add_rung`)* Created the tag and added the rung: `XIC(StartPB)MOV(1,PalletizerState);`
>
> **You:** Validate and save to C:\Projects\Line4_updated.L5X
>
> **Claude:** *(calls `validate_project`, then `save_project`)* Validation passed with 0 errors and 0 warnings. Saved to C:\Projects\Line4_updated.L5X.

## API Reference

### project.py -- L5XProject class

| Method | Description |
|--------|-------------|
| `L5XProject(file_path)` | Load an L5X file into memory (or create an empty model if `file_path` is `None`) |
| `load(file_path)` | Load (or reload) an L5X file |
| `write(file_path)` | Write the project to an L5X file with CDATA sections preserved |
| `controller_name` | Property: the controller name string |
| `processor_type` | Property: the processor catalog number (e.g., `1756-L83E`) |
| `firmware_version` | Property: firmware revision string |
| `get_project_summary()` | Return a dict with counts of programs, tags, AOIs, UDTs, modules, and tasks |
| `list_programs()` | List all program names |
| `list_routines(program_name)` | List routines in a program with their types |
| `list_controller_tags()` | List all controller-scope tags with name, data type, and description |
| `list_program_tags(program_name)` | List all tags in a specific program |
| `list_modules()` | List all I/O modules |
| `list_aois()` | List all Add-On Instruction definitions |
| `list_udts()` | List all User-Defined Types |
| `list_tasks()` | List all tasks with type, priority, rate, and scheduled programs |
| `get_all_rungs(program_name, routine_name)` | Get all rungs with text and comments |
| `get_tag_value(tag_name, scope, program_name)` | Get a tag's current value |
| `get_tag_member_value(tag_name, member_path, scope, program_name)` | Get a structured tag member's value |
| `find_tag_references(tag_name)` | Find all rungs/routines referencing a tag |
| `find_unused_tags(scope, program_name)` | Find tags not referenced in any rung |
| `get_program_element(program_name)` | Get the raw XML element for a program |
| `get_routine_element(program_name, routine_name)` | Get the raw XML element for a routine |
| `get_data_type_definition(type_name)` | Get the XML element for a UDT or AOI definition |

### tags.py

| Function | Description |
|----------|-------------|
| `create_tag(project, name, data_type, ...)` | Create a new tag with full L5K and Decorated data |
| `delete_tag(project, name, scope, program_name)` | Delete a tag from the project |
| `rename_tag(project, old_name, new_name, ...)` | Rename a tag, optionally updating all rung references |
| `copy_tag(project, name, new_name, ...)` | Deep-copy a tag with a new name |
| `move_tag(project, name, from_scope, to_scope, ...)` | Move a tag between controller and program scope |
| `set_tag_value(project, name, value, ...)` | Set a scalar tag's value (syncs both data formats) |
| `set_tag_member_value(project, name, member_path, value, ...)` | Set a member value in a structured or array tag |
| `set_tag_description(project, name, description, ...)` | Set or update a tag's description text |
| `get_tag_info(project, name, ...)` | Get detailed tag information (type, value, dimensions, etc.) |
| `tag_exists(project, name, scope, program_name)` | Check whether a tag exists |
| `batch_create_tags(project, specs, scope, program_name)` | Create multiple tags from a list of specification dicts |

### programs.py

| Function | Description |
|----------|-------------|
| `create_program(project, name, description)` | Create a new program with a default MainRoutine |
| `delete_program(project, name)` | Delete a program and unschedule it from all tasks |
| `create_routine(project, program_name, routine_name, routine_type)` | Create a new routine (RLL, ST, FBD, or SFC) |
| `delete_routine(project, program_name, routine_name)` | Delete a routine from a program |
| `add_rung(project, program_name, routine_name, text, comment, position)` | Add a rung to an RLL routine |
| `delete_rung(project, program_name, routine_name, rung_number)` | Delete a rung by index |
| `modify_rung_text(project, program_name, routine_name, rung_number, new_text)` | Replace the instruction text of an existing rung |
| `set_rung_comment(project, program_name, routine_name, rung_number, comment)` | Set or update a rung comment |
| `copy_rung(project, program_name, routine_name, rung_number, position)` | Copy a rung to another position |
| `duplicate_rung_with_substitution(project, ..., substitutions, new_comment)` | Duplicate a rung with tag name replacements |
| `add_st_line(project, program_name, routine_name, text)` | Add a line of Structured Text |
| `set_st_content(project, program_name, routine_name, lines)` | Replace all Structured Text content |
| `add_jsr_rung(project, program_name, routine_name, target_routine, ...)` | Add a JSR (Jump to Subroutine) rung |
| `schedule_program(project, task_name, program_name)` | Schedule a program under a task |
| `unschedule_program(project, task_name, program_name)` | Remove a program from a task's schedule |

### rungs.py

| Function / Class | Description |
|------------------|-------------|
| `tokenize(rung_text)` | Tokenize rung instruction text into a flat list of `Token` objects |
| `parse_rung(rung_text, comment)` | Parse rung text into a structured `Rung` AST with `InstructionCall` and `Branch` nodes |
| `validate_rung_syntax(rung_text)` | Check bracket matching, semicolon termination, and parenthesis balance |
| `validate_rung_references(rung_text, available_tags)` | Check that all referenced tags exist in a given set |
| `extract_tag_references(rung_text)` | Extract all unique base tag names referenced in rung text |
| `substitute_tags(rung_text, substitutions)` | Replace tag names with word-boundary-safe substitution |
| `build_rung_text(instructions, comment)` | Construct valid rung text from one or more instruction strings |
| `TokenType` | Enum of token types: INSTRUCTION, TAG_REFERENCE, LITERAL, OPEN_BRACKET, etc. |
| `Token` | Dataclass: a single lexical token with `type` and `value` |
| `InstructionCall` | Dataclass: an instruction with `name` and `arguments` |
| `Branch` | Dataclass: parallel OR logic with a list of `paths` |
| `Rung` | Dataclass: a fully parsed rung with `elements` and `comment` |

### modules.py

| Function | Description |
|----------|-------------|
| `list_modules(project)` | List all modules with catalog numbers, parent info, and inhibit state |
| `get_module_info(project, name)` | Get detailed module info including ports, description, and EKey state |
| `set_module_address(project, module_name, port_id, address)` | Set the address (IP or slot) on a specific port |
| `set_module_inhibited(project, module_name, inhibited)` | Enable or disable module inhibit |
| `import_module(project, template_path, name, ...)` | Import a module from a template L5X file with configurable identity |
| `delete_module(project, name)` | Delete a module (cannot delete 'Local') |

### aoi.py

| Function | Description |
|----------|-------------|
| `import_aoi(project, file_path, overwrite)` | Import an AOI from an L5X export file with automatic dependency resolution |
| `get_aoi_info(project, name)` | Get full AOI metadata: revision, parameters, local tags, routines |
| `get_aoi_parameters(project, name)` | Get the parameter list with types, usage, required flag, and defaults |
| `list_aoi_dependencies(project, name)` | List UDTs and other AOIs this AOI depends on |
| `generate_aoi_call_text(project, aoi_name, instance_tag, param_map)` | Generate rung instruction text for calling an AOI |

### udt.py

| Function | Description |
|----------|-------------|
| `import_udt(project, file_path, overwrite)` | Import a UDT from an L5X export file with transitive dependency resolution |
| `get_udt_info(project, name)` | Get full UDT metadata: family, class, description, and all members |
| `get_udt_members(project, name)` | Get visible (non-hidden) members only |
| `get_udt_all_members(project, name)` | Get all members including hidden BOOL backing fields |
| `list_udt_dependencies(project, name)` | List other UDTs referenced in member definitions |

### validator.py

| Function / Class | Description |
|------------------|-------------|
| `validate_project(project)` | Run all validation checks and return an aggregated `ValidationResult` |
| `validate_structure(project)` | Check required XML elements and hierarchy |
| `validate_references(project)` | Check tag references in rungs against defined tags |
| `validate_tag(project, tag_element)` | Validate a single tag element (name, type, data format) |
| `validate_rung(rung_text)` | Validate a single rung's syntax |
| `validate_naming(project)` | Check naming conventions for tags, programs, and routines |
| `ValidationResult` | Container with `errors`, `warnings`, `is_valid`, `add_error()`, `add_warning()`, `merge()` |

### data_format.py

| Function | Description |
|----------|-------------|
| `get_default_radix(data_type)` | Return the default display radix for a data type |
| `scalar_to_l5k(data_type, value)` | Convert a Python value to L5K format string |
| `scalar_to_decorated_value(data_type, value, radix)` | Convert a Python value to Decorated XML Value attribute string |
| `generate_default_l5k(data_type, dimensions, project)` | Generate the default L5K data text for a tag |
| `generate_default_decorated(data_type, dimensions, radix, project)` | Generate the default Decorated XML element tree for a tag |
| `generate_tag_data_elements(data_type, dimensions, radix, project)` | Generate both `<Data Format="L5K">` and `<Data Format="Decorated">` elements |

## MCP Tools Reference

All 42 tools exposed by the MCP server, grouped by category. Every tool returns a string result (JSON for queries, a confirmation message for mutations).

### Project Management (3 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `load_project` | `file_path` | Load an L5X file into memory. **Must be called first.** |
| `save_project` | `file_path` (optional) | Save the project. Omit path to overwrite the original. |
| `get_project_summary` | *(none)* | Get counts of programs, tags, AOIs, UDTs, modules, and tasks. |

### Query Tools (10 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `list_programs` | *(none)* | List all program names. |
| `list_routines` | `program_name` | List routines in a program with their types. |
| `list_controller_tags` | *(none)* | List all controller-scope tags. |
| `list_program_tags` | `program_name` | List all tags in a specific program. |
| `list_modules` | *(none)* | List all I/O modules with catalog numbers. |
| `list_aois` | *(none)* | List all Add-On Instruction definitions. |
| `list_udts` | *(none)* | List all User-Defined Types. |
| `list_tasks` | *(none)* | List all tasks with type, priority, and scheduled programs. |
| `get_all_rungs` | `program_name`, `routine_name` | Get all rungs with text and comments. |
| `get_tag_info` | `name`, `scope`, `program_name` | Get detailed info about a specific tag. |

### Tag Operations (7 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `create_tag` | `name`, `data_type`, `scope`, `program_name`, `dimensions`, `description`, `radix` | Create a new tag (BOOL, DINT, REAL, TIMER, UDT, array, etc.). |
| `delete_tag` | `name`, `scope`, `program_name` | Delete a tag. |
| `rename_tag` | `old_name`, `new_name`, `scope`, `program_name`, `update_references` | Rename a tag, optionally updating all rung references. |
| `set_tag_value` | `name`, `value`, `scope`, `program_name` | Set a scalar tag's value. |
| `set_tag_member_value` | `name`, `member_path`, `value`, `scope`, `program_name` | Set a member value in a structured/array tag (e.g., `Timer1.PRE`). |
| `set_tag_description` | `name`, `description`, `scope`, `program_name` | Set or update a tag's description. |
| `batch_create_tags` | `tag_specs_json`, `scope`, `program_name` | Create multiple tags from a JSON array of specs. |

### Program and Routine Operations (10 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `create_program` | `name`, `description` | Create a program with a default MainRoutine. |
| `delete_program` | `name` | Delete a program and unschedule from all tasks. |
| `create_routine` | `program_name`, `routine_name`, `routine_type` | Create a routine (RLL, ST, FBD, or SFC). |
| `add_rung` | `program_name`, `routine_name`, `instruction_text`, `comment`, `position` | Add a rung to an RLL routine. |
| `delete_rung` | `program_name`, `routine_name`, `rung_number` | Delete a rung by index. |
| `modify_rung_text` | `program_name`, `routine_name`, `rung_number`, `new_text` | Replace a rung's instruction text. |
| `set_rung_comment` | `program_name`, `routine_name`, `rung_number`, `comment` | Set or update a rung comment. |
| `duplicate_rung_with_substitution` | `program_name`, `routine_name`, `rung_number`, `substitutions_json`, `comment` | Duplicate a rung with tag name replacements. |
| `schedule_program` | `task_name`, `program_name` | Schedule a program under a task. |
| `unschedule_program` | `task_name`, `program_name` | Remove a program from a task's schedule. |

### Import Operations (3 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `import_aoi` | `file_path`, `overwrite` | Import an AOI from an L5X file with automatic dependency resolution. |
| `import_udt` | `file_path`, `overwrite` | Import a UDT from an L5X file with transitive dependency resolution. |
| `import_module` | `template_path`, `name`, `parent_module`, `address`, `slot`, `description` | Import a module from a template L5X file. |

### Analysis Tools (5 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_aoi_info` | `name` | Get full AOI details: revision, parameters, local tags, routines. |
| `get_aoi_parameters` | `name` | Get the AOI parameter list with types, usage, and defaults. |
| `get_udt_info` | `name` | Get full UDT details: family, class, description, and members. |
| `get_udt_members` | `name` | Get visible (non-hidden) members of a UDT. |
| `find_tag_references` | `tag_name` | Find all rungs/routines where a tag is referenced. |

### Validation and Utilities (4 tools)

| Tool | Parameters | Description |
|------|-----------|-------------|
| `validate_project` | *(none)* | Run all validation checks (structure, references, naming, rung syntax, etc.). |
| `validate_rung_syntax` | `rung_text` | Check if a rung instruction string is syntactically valid. |
| `substitute_tags_in_rung` | `rung_text`, `substitutions_json` | Replace tag names in rung text with word-boundary-safe substitution. |
| `extract_tag_references_from_rung` | `rung_text` | Extract all base tag names referenced in rung text. |

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
C:\Tools\l5x-toolkit\n|-- setup.py                          # Package metadata and entry points
|-- README.md                         # This file
|-- l5x_agent_toolkit/
|   |-- __init__.py                   # Package init with lazy L5XProject import
|   |-- project.py                    # L5XProject class: load, write, query, navigate
|   |-- tags.py                       # Tag CRUD: create, delete, rename, copy, move, set value
|   |-- programs.py                   # Program/routine CRUD and rung operations
|   |-- rungs.py                      # Rung tokenizer, parser, validator, and tag substitution
|   |-- modules.py                    # I/O module list, inspect, import, configure, delete
|   |-- aoi.py                        # AOI import, query, dependency analysis, call generation
|   |-- udt.py                        # UDT import, query, member inspection, dependency analysis
|   |-- validator.py                  # Pre-flight validation engine (structure, refs, naming, etc.)
|   |-- data_format.py               # L5K and Decorated data format generation and sync
|   |-- schema.py                     # Constants: base types, built-in structures, instruction catalog
|   |-- utils.py                      # Shared XML helpers: CDATA, deep copy, element ordering
|   |-- mcp_server.py                 # MCP server exposing all 42 tools over stdio
|-- tests/
    |-- __init__.py                   # Test package init
```

## License / Credits

The L5X Agent Toolkit was built for internal use at CRG Automation. It uses the [lxml](https://lxml.de/) library for XML processing and the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) for the Model Context Protocol server.

Rockwell Automation, Studio 5000, Logix Designer, and RSLogix 5000 are trademarks of Rockwell Automation, Inc. This toolkit is not affiliated with or endorsed by Rockwell Automation.
