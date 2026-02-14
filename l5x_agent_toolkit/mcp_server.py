"""
MCP Server for L5X Agent Toolkit.

Exposes validated L5X file manipulation tools via the Model Context Protocol,
allowing any MCP-compatible AI client (Claude Desktop, Claude Code, etc.) to
perform hyper-accurate PLC project modifications through natural language.

This server uses consolidated, batch-capable tools to minimise round-trips.
Most mutation endpoints accept a JSON array of operations so that multiple
changes can be applied in a single call.

Usage:
    python -m l5x_agent_toolkit.mcp_server
    # or
    python l5x_agent_toolkit/mcp_server.py
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import sys
from typing import Optional
from urllib.parse import unquote, urlparse

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Toolkit imports
# ---------------------------------------------------------------------------
from .project import L5XProject
from . import tags as _tags
from . import programs as _programs
from . import modules as _modules
from . import rungs as _rungs
from . import aoi as _aoi
from . import udt as _udt
from . import validator as _validator
from . import component_export as _comp_export
from . import component_import as _comp_import

# ---------------------------------------------------------------------------
# Logging (stderr only -- stdout is reserved for MCP protocol)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("l5x-mcp")

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "L5X Agent Toolkit",
    instructions=(
        "Validated tools for reading and modifying Rockwell Automation "
        "Studio 5000 L5X project files.  The AI never touches raw XML -- "
        "every operation produces structurally correct output.\n\n"
        "Always call load_project first before using any other tool.\n\n"
        "Batch-capable tools (manage_tags, update_tags, manage_rungs, "
        "manage_alarms) accept a JSON array of operations so you can "
        "create, modify, or delete multiple items in a single call."
    ),
)

# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------
_project: Optional[L5XProject] = None
_project_path: Optional[str] = None


def _require_project() -> L5XProject:
    """Return the loaded project or raise an error."""
    if _project is None:
        raise RuntimeError(
            "No project loaded. Call load_project first."
        )
    return _project


def _normalize_path(raw_path: str) -> str:
    """Normalize a file path from Claude Desktop into a real filesystem path.

    Handles:
    - file:///C:/... URIs (drag-and-drop gives these)
    - URL-encoded characters (%20 for spaces, etc.)
    - Forward slashes on Windows
    - Relative paths (resolved against cwd)
    - Surrounding quotes or whitespace
    """
    path = raw_path.strip().strip('"').strip("'")

    # Handle file:// URIs
    if path.startswith("file:///"):
        parsed = urlparse(path)
        # On Windows, urlparse gives /C:/path -- strip leading slash
        decoded = unquote(parsed.path)
        if len(decoded) >= 3 and decoded[0] == '/' and decoded[2] == ':':
            decoded = decoded[1:]
        path = decoded
    elif path.startswith("file://"):
        path = unquote(path[7:])

    # Normalize slashes and resolve to absolute
    path = os.path.normpath(path)
    path = os.path.abspath(path)

    return path


def _auto_convert_value(value_str: str, data_type: str):
    """Convert a string value to the appropriate Python type for a tag."""
    if data_type in ('REAL', 'LREAL'):
        return float(value_str)
    if data_type == 'BOOL':
        return 1 if value_str.lower() in ('1', 'true', 'yes') else 0
    try:
        return int(value_str)
    except ValueError:
        return float(value_str)


# ===================================================================
# 1. Project Management
# ===================================================================

@mcp.tool()
def load_project(file_path: str) -> str:
    """Load an L5X project file into memory.

    This must be called before any other tool. The project stays in memory
    until a different project is loaded or the server is restarted.

    Args:
        file_path: Absolute path to the .L5X file.
    """
    global _project, _project_path
    try:
        resolved = _normalize_path(file_path)
        log.info("Resolved path: %s -> %s", file_path, resolved)
        _project = L5XProject(resolved)
        _project_path = resolved
        summary = _project.get_project_summary()
        target_type = _project.target_type
        log.info("Loaded project: %s (TargetType=%s)", file_path, target_type)

        # Build response based on export type
        lines = [
            f"Loaded: {_project.controller_name} "
            f"({_project.processor_type}, FW {_project.firmware_version})",
            f"Export Type: {target_type}",
        ]

        if target_type == 'Controller':
            lines.append(
                f"Programs: {summary['program_count']}, "
                f"Tags: {summary['tag_count']}, "
                f"AOIs: {summary['aoi_count']}, "
                f"UDTs: {summary['udt_count']}, "
                f"Modules: {summary['module_count']}"
            )
        elif target_type == 'AddOnInstructionDefinition':
            lines.append(
                f"This is an AOI export file. "
                f"AOIs: {summary['aoi_count']}, UDTs: {summary['udt_count']}"
            )
            lines.append(
                "Use get_entity_info(entity='aoi') to inspect. "
                "To use this AOI, load a full project and use import_component."
            )
        elif target_type == 'DataType':
            lines.append(
                f"This is a UDT export file. UDTs: {summary['udt_count']}"
            )
            lines.append(
                "Use get_entity_info(entity='udt') to inspect. "
                "To use this UDT, load a full project and use import_component."
            )
        elif target_type == 'Module':
            lines.append(
                f"This is a Module export file. Modules: {summary['module_count']}"
            )
            lines.append(
                "To use this module, load a full project and use import_component."
            )
        elif target_type == 'Rung':
            target_count = _project.root.get('TargetCount', '?')
            lines.append(
                f"This is a Rung export file ({target_count} target rungs). "
                f"Programs: {summary['program_count']}, "
                f"Tags: {summary['tag_count']}, "
                f"AOIs: {summary['aoi_count']}, "
                f"UDTs: {summary['udt_count']}"
            )
            lines.append(
                "You can: read rungs/tags, create tags (including AOI/UDT types "
                "defined in the export), add/modify/delete rungs, and save changes."
            )
        else:
            lines.append(
                f"Programs: {summary['program_count']}, "
                f"Tags: {summary['tag_count']}, "
                f"AOIs: {summary['aoi_count']}, "
                f"UDTs: {summary['udt_count']}, "
                f"Modules: {summary['module_count']}"
            )

        return '\n'.join(lines)
    except Exception as e:
        _project = None
        _project_path = None
        return f"Error loading project: {e}"


@mcp.tool()
def save_project(file_path: str = "") -> str:
    """Save the current project to an L5X file.

    Args:
        file_path: Destination path. If empty, overwrites the original file.
    """
    prj = _require_project()
    dest = _normalize_path(file_path) if file_path else _project_path
    if not dest:
        return "Error: No file path specified and no original path available."
    try:
        prj.write(dest)
        log.info("Saved project to: %s", dest)
        return f"Project saved to: {dest}"
    except Exception as e:
        return f"Error saving project: {e}"


@mcp.tool()
def format_project() -> str:
    """Pretty-print the loaded project XML with consistent indentation.

    Re-indents every element in the XML tree so that child elements are
    indented with two spaces relative to their parent.  Call this before
    ``save_project`` to produce a cleanly formatted output file.

    This is optional -- Studio 5000 does not require pretty formatting,
    but it makes the L5X file much easier to read and diff.
    """
    prj = _require_project()
    try:
        from .utils import indent_xml
        indent_xml(prj.root)
        return "Project XML re-indented successfully."
    except Exception as e:
        return f"Error formatting project: {e}"


@mcp.tool()
def strip_l5k_data(scope: str = "", program_name: str = "") -> str:
    """Remove L5K data from tags, keeping only Decorated format.

    Studio 5000 can reconstruct L5K data from Decorated format during
    import. Use this when L5K data may be out of sync, causing
    'Data type mismatch' import errors.

    Args:
        scope: 'controller', 'program', or empty for all scopes.
        program_name: Filter to specific program when scope is 'program'.
    """
    prj = _require_project()
    try:
        count = _tags.strip_l5k_data(
            prj, scope=scope, program_name=program_name or None)
        return f"Removed {count} L5K Data elements from tags."
    except Exception as e:
        return f"Error stripping L5K data: {e}"


@mcp.tool()
def get_project_summary() -> str:
    """Get a summary of the loaded project (counts, names, metadata)."""
    prj = _require_project()
    summary = prj.get_project_summary()
    return json.dumps(summary, indent=2)


# ===================================================================
# 2. Query Tools (consolidated)
# ===================================================================

@mcp.tool()
def query_project(
    entity: str = "all",
    scope: str = "",
    program_name: str = "",
    name_filter: str = "",
) -> str:
    """Query project contents -- programs, tags, modules, AOIs, UDTs, tasks.

    Replaces the former list_programs, list_routines, list_controller_tags,
    list_program_tags, list_modules, list_aois, list_udts, list_tasks tools.
    Use entity='all' to get a full inventory in one call.

    Args:
        entity: What to list. One of: 'all', 'programs', 'routines',
                'tags', 'modules', 'aois', 'udts', 'tasks'.
                'all' returns a combined inventory.
        scope: For tags: 'controller', 'program', or '' (both scopes).
               Ignored for non-tag entities.
        program_name: Required for 'routines'. For 'tags', filters to a
                      specific program when scope includes 'program'.
        name_filter: Optional glob pattern to filter names (e.g. 'Motor*').
    """
    prj = _require_project()
    try:
        result: dict = {}

        entities = (
            ["programs", "tags", "modules", "aois", "udts", "tasks"]
            if entity == "all"
            else [entity]
        )

        for ent in entities:
            if ent == "programs":
                result["programs"] = prj.list_programs()
            elif ent == "routines":
                if not program_name:
                    return "Error: program_name is required when entity='routines'."
                result["routines"] = prj.list_routines(program_name)
            elif ent == "tags":
                tags: list = []
                if scope in ("controller", ""):
                    for t in prj.list_controller_tags():
                        t["scope"] = "controller"
                        tags.append(t)
                if scope in ("program", ""):
                    if program_name:
                        for t in prj.list_program_tags(program_name):
                            t["scope"] = "program"
                            t["program"] = program_name
                            tags.append(t)
                    elif scope == "" or scope == "program":
                        for p in prj.list_programs():
                            for t in prj.list_program_tags(p):
                                t["scope"] = "program"
                                t["program"] = p
                                tags.append(t)
                result["tags"] = tags
            elif ent == "modules":
                result["modules"] = prj.list_modules()
            elif ent == "aois":
                result["aois"] = prj.list_aois()
            elif ent == "udts":
                result["udts"] = prj.list_udts()
            elif ent == "tasks":
                result["tasks"] = prj.list_tasks()
            else:
                return (
                    f"Error: Unknown entity '{ent}'. "
                    f"Choose from: all, programs, routines, tags, modules, "
                    f"aois, udts, tasks."
                )

        # Apply name_filter if provided
        if name_filter:
            for key in result:
                items = result[key]
                if isinstance(items, list):
                    result[key] = [
                        item for item in items
                        if fnmatch.fnmatch(
                            (item.get("name", item)
                             if isinstance(item, dict) else item),
                            name_filter,
                        )
                    ]

        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error querying project: {e}"


@mcp.tool()
def get_entity_info(
    entity: str,
    name: str,
    scope: str = "controller",
    program_name: str = "",
    routine_name: str = "",
    include: str = "",
) -> str:
    """Get detailed information about a specific entity.

    Replaces the former get_tag_info, find_tag, get_tag_member_value,
    get_aoi_info, get_aoi_parameters, get_udt_info, get_udt_members tools.

    Args:
        entity: Entity type -- 'tag', 'aoi', 'udt', 'rung'.
        name: Entity name. For rungs, use the zero-based rung number.
              For tag member access, use dot/bracket notation on the name
              (e.g. 'Timer1.PRE', 'Array[0]').
        scope: For tags: 'controller', 'program', or '' (search all scopes).
        program_name: Required when scope is 'program', or for rung queries.
        routine_name: Required for rung queries.
        include: Comma-separated extras to include in the response:
                 For tags:  'value', 'references', 'alarm_conditions'
                 For AOIs:  'parameters'
                 For UDTs:  'members'
                 Multiple values can be combined: 'parameters,members'
    """
    prj = _require_project()
    try:
        include_set = {s.strip() for s in include.split(",") if s.strip()}
        prog = program_name if program_name else None

        if entity == "tag":
            # Check if name contains a member path (dot or bracket)
            member_path = ""
            base_name = name
            if '.' in name:
                parts = name.split('.', 1)
                base_name = parts[0]
                member_path = parts[1]
            elif '[' in name:
                idx = name.index('[')
                base_name = name[:idx]
                member_path = name[idx:]

            # If scope is empty, search all scopes
            if scope == "":
                info = _tags.find_tag(prj, base_name)
            else:
                info = _tags.get_tag_info(
                    prj, base_name, scope=scope, program_name=prog,
                )

            # Add member value if a member path was specified
            if member_path:
                effective_scope = info.get("scope", scope) or scope
                effective_prog = info.get("program") or prog
                member_val = prj.get_tag_member_value(
                    base_name, member_path,
                    scope=effective_scope,
                    program_name=effective_prog,
                )
                info["member_path"] = member_path
                info["member_value"] = member_val

            # Include extras
            if "references" in include_set:
                info["references"] = prj.find_tag_references(base_name)
            if "alarm_conditions" in include_set:
                effective_scope = info.get("scope", scope) or scope
                effective_prog = info.get("program") or prog
                info["alarm_conditions"] = _tags.get_tag_alarm_conditions(
                    prj, base_name,
                    scope=effective_scope,
                    program_name=effective_prog,
                )

            return json.dumps(info, indent=2)

        elif entity == "aoi":
            info = _aoi.get_aoi_info(prj, name)
            if "parameters" in include_set:
                info["parameters_detail"] = _aoi.get_aoi_parameters(prj, name)
            return json.dumps(info, indent=2)

        elif entity == "udt":
            info = _udt.get_udt_info(prj, name)
            if "members" in include_set:
                info["members_detail"] = _udt.get_udt_members(prj, name)
            return json.dumps(info, indent=2)

        elif entity == "rung":
            if not program_name or not routine_name:
                return "Error: program_name and routine_name are required for entity='rung'."
            all_rungs = prj.get_all_rungs(program_name, routine_name)
            try:
                rung_num = int(name)
            except ValueError:
                return f"Error: For entity='rung', name must be a rung number (got '{name}')."
            if rung_num < 0 or rung_num >= len(all_rungs):
                return f"Error: Rung {rung_num} out of range (0-{len(all_rungs) - 1})."
            return json.dumps(all_rungs[rung_num], indent=2)

        else:
            return (
                f"Error: Unknown entity '{entity}'. "
                f"Choose from: tag, aoi, udt, rung."
            )
    except Exception as e:
        return f"Error getting entity info: {e}"


@mcp.tool()
def get_all_rungs(program_name: str, routine_name: str) -> str:
    """Get all rungs in an RLL routine with their text and comments.

    Args:
        program_name: Name of the program.
        routine_name: Name of the routine.
    """
    prj = _require_project()
    return json.dumps(prj.get_all_rungs(program_name, routine_name))


@mcp.tool()
def find_tag_references(tag_name: str) -> str:
    """Find all locations where a tag is referenced in rung text or ST code.

    Returns a list of {program, routine, rung, text} for each reference.

    Args:
        tag_name: Tag name to search for.
    """
    prj = _require_project()
    try:
        refs = prj.find_tag_references(tag_name)
        return json.dumps(refs)
    except Exception as e:
        return f"Error finding references: {e}"


# ===================================================================
# 3. Tag Operations (consolidated)
# ===================================================================

@mcp.tool()
def manage_tags(
    operations_json: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Execute one or more tag CRUD operations in sequence.

    Replaces the former create_tag, delete_tag, rename_tag, copy_tag,
    move_tag, batch_create_tags, and create_alias_tag tools.

    Args:
        operations_json: JSON array of operation objects. Each has an
            'action' field plus action-specific fields:

            create:       {name, data_type, dimensions?, description?,
                           radix?, tag_class?}
            delete:       {name}
            rename:       {name, new_name, update_references? (default true)}
            copy:         {name, new_name, to_scope?, to_program_name?}
            move:         {name, to_scope, to_program?}
            create_alias: {name, alias_for, description?}

            Each operation can optionally include 'scope' and 'program_name'
            to override the tool-level defaults.

        scope: Default scope for all operations ('controller' or 'program').
        program_name: Default program for all operations.

    Example:
        [{"action": "create", "name": "Motor1_Run", "data_type": "BOOL"},
         {"action": "create", "name": "Motor1_Flt", "data_type": "BOOL",
          "description": "Motor 1 fault"},
         {"action": "rename", "name": "OldTag", "new_name": "NewTag"}]
    """
    prj = _require_project()
    try:
        ops = json.loads(operations_json)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON -- {e}"

    results = []
    for i, op in enumerate(ops):
        action = op.get("action", "")
        op_scope = op.get("scope", scope)
        op_prog = op.get("program_name", program_name) or None

        try:
            if action == "create":
                _tags.create_tag(
                    prj, op["name"], op["data_type"],
                    scope=op_scope,
                    program_name=op_prog,
                    dimensions=op.get("dimensions") or None,
                    description=op.get("description") or None,
                    radix=op.get("radix") or None,
                    tag_class=op.get("tag_class") or None,
                )
                results.append({"index": i, "status": "ok", "action": "create",
                                "name": op["name"]})

            elif action == "delete":
                _tags.delete_tag(
                    prj, op["name"],
                    scope=op_scope, program_name=op_prog,
                )
                results.append({"index": i, "status": "ok", "action": "delete",
                                "name": op["name"]})

            elif action == "rename":
                _tags.rename_tag(
                    prj, op["name"], op["new_name"],
                    scope=op_scope,
                    program_name=op_prog,
                    update_references=op.get("update_references", True),
                )
                results.append({"index": i, "status": "ok", "action": "rename",
                                "old": op["name"], "new": op["new_name"]})

            elif action == "copy":
                _tags.copy_tag(
                    prj, op["name"], op["new_name"],
                    source_scope=op_scope,
                    source_program=op_prog,
                    dest_scope=op.get("to_scope", op_scope),
                    dest_program=op.get("to_program_name", op_prog),
                )
                results.append({"index": i, "status": "ok", "action": "copy",
                                "name": op["name"], "new_name": op["new_name"]})

            elif action == "move":
                _tags.move_tag(
                    prj, op["name"],
                    from_scope=op_scope,
                    from_program=op_prog,
                    to_scope=op["to_scope"],
                    to_program=op.get("to_program") or None,
                )
                results.append({"index": i, "status": "ok", "action": "move",
                                "name": op["name"]})

            elif action == "create_alias":
                _tags.create_alias_tag(
                    prj, op["name"], op["alias_for"],
                    scope=op_scope,
                    program_name=op_prog,
                    description=op.get("description") or None,
                )
                results.append({"index": i, "status": "ok", "action": "create_alias",
                                "name": op["name"]})

            else:
                results.append({"index": i, "status": "error",
                                "message": f"Unknown action: {action}"})
        except Exception as e:
            results.append({"index": i, "status": "error", "action": action,
                            "message": str(e)})

    succeeded = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "error")
    return json.dumps(
        {"succeeded": succeeded, "failed": failed, "details": results},
        indent=2,
    )


@mcp.tool()
def update_tags(
    updates_json: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Set values, member values, and descriptions on one or more tags.

    Replaces the former set_tag_value, set_tag_member_value, and
    set_tag_description tools.

    Args:
        updates_json: JSON array of update objects. Each has:
            - 'name': tag name (required)
            - 'value': new scalar value as string (optional)
            - 'description': new description text (optional)
            - 'members': dict of {member_path: value} for structured
              or array tags (optional)

            Each update can optionally include 'scope' and 'program_name'.

        scope: Default scope for all updates.
        program_name: Default program for all updates.

    Example:
        [{"name": "Timer1", "members": {"PRE": "5000"},
          "description": "Main cycle timer"},
         {"name": "MotorSpeed", "value": "1750"}]
    """
    prj = _require_project()
    try:
        updates = json.loads(updates_json)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON -- {e}"

    results = []
    for i, upd in enumerate(updates):
        tag_name = upd.get("name", "")
        upd_scope = upd.get("scope", scope)
        upd_prog = upd.get("program_name", program_name) or None
        changes_made = []

        try:
            # Set scalar value
            if "value" in upd:
                info = _tags.get_tag_info(
                    prj, tag_name, scope=upd_scope, program_name=upd_prog,
                )
                dt = info.get("data_type", "")
                py_val = _auto_convert_value(str(upd["value"]), dt)
                _tags.set_tag_value(
                    prj, tag_name, py_val,
                    scope=upd_scope, program_name=upd_prog,
                )
                changes_made.append(f"value={py_val}")

            # Set member values
            if "members" in upd:
                for member_path, member_val in upd["members"].items():
                    try:
                        py_val = int(str(member_val))
                    except ValueError:
                        py_val = float(str(member_val))
                    _tags.set_tag_member_value(
                        prj, tag_name, member_path, py_val,
                        scope=upd_scope, program_name=upd_prog,
                    )
                    changes_made.append(f"{member_path}={py_val}")

            # Set description
            if "description" in upd:
                _tags.set_tag_description(
                    prj, tag_name, upd["description"],
                    scope=upd_scope, program_name=upd_prog,
                )
                changes_made.append("description")

            results.append({"index": i, "status": "ok", "name": tag_name,
                            "changes": changes_made})
        except Exception as e:
            results.append({"index": i, "status": "error", "name": tag_name,
                            "message": str(e)})

    succeeded = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "error")
    return json.dumps(
        {"succeeded": succeeded, "failed": failed, "details": results},
        indent=2,
    )


# ===================================================================
# 4. Program & Routine Operations
# ===================================================================

@mcp.tool()
def create_program(name: str, description: str = "") -> str:
    """Create a new program with a default MainRoutine.

    Args:
        name: Program name.
        description: Optional description text.
    """
    prj = _require_project()
    try:
        _programs.create_program(
            prj, name,
            description=description or None,
        )
        return f"Created program '{name}'"
    except Exception as e:
        return f"Error creating program: {e}"


@mcp.tool()
def delete_program(name: str) -> str:
    """Delete a program and unschedule it from all tasks.

    Args:
        name: Program name to delete.
    """
    prj = _require_project()
    try:
        _programs.delete_program(prj, name)
        return f"Deleted program '{name}'"
    except Exception as e:
        return f"Error deleting program: {e}"


@mcp.tool()
def create_routine(
    program_name: str,
    routine_name: str,
    routine_type: str = "RLL",
) -> str:
    """Create a new routine in a program.

    Args:
        program_name: Name of the parent program.
        routine_name: Name for the new routine.
        routine_type: Routine type - 'RLL', 'ST', 'FBD', or 'SFC'.
    """
    prj = _require_project()
    try:
        _programs.create_routine(
            prj, program_name, routine_name,
            routine_type=routine_type,
        )
        return f"Created routine '{routine_name}' (type={routine_type}) in '{program_name}'"
    except Exception as e:
        return f"Error creating routine: {e}"


@mcp.tool()
def manage_rungs(
    program_name: str,
    routine_name: str,
    operations_json: str,
) -> str:
    """Execute one or more rung operations on a routine in sequence.

    Replaces the former add_rung, delete_rung, modify_rung_text,
    set_rung_comment, and duplicate_rung_with_substitution tools.

    Operations are processed in order. Rung numbers in later operations
    should account for insertions/deletions made by earlier operations
    in the same batch.

    Args:
        program_name: Program containing the routine.
        routine_name: Name of the RLL routine.
        operations_json: JSON array of operation objects. Each has an
            'action' field plus action-specific fields:

            add:       {text, comment?, position? (-1 or omit to append)}
            delete:    {rung_number}
            modify:    {rung_number, text?, comment?} (set either or both)
            duplicate: {rung_number, substitutions, comment?}

    Example:
        [{"action": "add", "text": "XIC(Start)OTE(Run);",
          "comment": "Start logic"},
         {"action": "add", "text": "TON(Delay,1000,0);"},
         {"action": "modify", "rung_number": 0, "comment": "Updated"}]
    """
    prj = _require_project()
    try:
        ops = json.loads(operations_json)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON -- {e}"

    results = []
    for i, op in enumerate(ops):
        action = op.get("action", "")
        try:
            if action == "add":
                pos = op.get("position")
                pos = pos if pos is not None and pos >= 0 else None
                _programs.add_rung(
                    prj, program_name, routine_name,
                    op["text"],
                    comment=op.get("comment") or None,
                    position=pos,
                )
                results.append({"index": i, "status": "ok", "action": "add",
                                "text": op["text"][:60]})

            elif action == "delete":
                _programs.delete_rung(
                    prj, program_name, routine_name, op["rung_number"],
                )
                results.append({"index": i, "status": "ok", "action": "delete",
                                "rung_number": op["rung_number"]})

            elif action == "modify":
                rn = op["rung_number"]
                if "text" in op:
                    _programs.modify_rung_text(
                        prj, program_name, routine_name, rn, op["text"],
                    )
                if "comment" in op:
                    _programs.set_rung_comment(
                        prj, program_name, routine_name, rn, op["comment"],
                    )
                results.append({"index": i, "status": "ok", "action": "modify",
                                "rung_number": rn})

            elif action == "duplicate":
                subs = op.get("substitutions", {})
                _programs.duplicate_rung_with_substitution(
                    prj, program_name, routine_name, op["rung_number"],
                    subs,
                    new_comment=op.get("comment") or None,
                )
                results.append({"index": i, "status": "ok", "action": "duplicate",
                                "rung_number": op["rung_number"]})

            else:
                results.append({"index": i, "status": "error",
                                "message": f"Unknown action: {action}"})
        except Exception as e:
            results.append({"index": i, "status": "error", "action": action,
                            "message": str(e)})

    succeeded = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "error")
    return json.dumps(
        {"succeeded": succeeded, "failed": failed, "details": results},
        indent=2,
    )


@mcp.tool()
def schedule_program(task_name: str, program_name: str) -> str:
    """Schedule a program to run under a task.

    Args:
        task_name: Name of the task (e.g. 'MainTask').
        program_name: Name of the program to schedule.
    """
    prj = _require_project()
    try:
        _programs.schedule_program(prj, task_name, program_name)
        return f"Scheduled '{program_name}' in task '{task_name}'"
    except Exception as e:
        return f"Error scheduling program: {e}"


@mcp.tool()
def unschedule_program(task_name: str, program_name: str) -> str:
    """Remove a program from a task's schedule.

    Args:
        task_name: Name of the task.
        program_name: Name of the program to unschedule.
    """
    prj = _require_project()
    try:
        _programs.unschedule_program(prj, task_name, program_name)
        return f"Unscheduled '{program_name}' from task '{task_name}'"
    except Exception as e:
        return f"Error unscheduling program: {e}"


# ===================================================================
# 5. Import Operations (consolidated)
# ===================================================================

@mcp.tool()
def import_component(
    file_path: str,
    conflict_resolution: str = "report",
    target_program: str = "",
    target_routine: str = "",
    rung_position: int = -1,
    module_name: str = "",
    parent_module: str = "Local",
    module_address: str = "",
    module_slot: str = "",
    overwrite: bool = False,
) -> str:
    """Import a component export file into the loaded project.

    Automatically detects the component type (Rung, Routine, Program,
    DataType, AddOnInstructionDefinition, Module) and imports with
    conflict detection and resolution.

    This replaces the former import_aoi, import_udt, and import_module
    tools. For AOI/UDT/Module files that need simple imports (no conflict
    analysis), set conflict_resolution='overwrite' or 'skip'.

    For Module template imports, also provide module_name and optionally
    parent_module, module_address, and module_slot.

    Args:
        file_path: Path to the component export .L5X file.
        conflict_resolution: How to handle conflicts:
            'report' = dry run (return conflicts only, no changes),
            'skip' = import non-conflicting items and skip conflicts,
            'overwrite' = replace existing items with imported versions,
            'fail' = abort on any conflict.
        target_program: Override target program (for Rung/Routine imports).
        target_routine: Override target routine (for Rung imports).
        rung_position: Insert position for rungs (0-based). -1 to append.
        module_name: Name for an imported module (Module files only).
        parent_module: Parent module name (default: 'Local').
        module_address: IP address for Ethernet modules.
        module_slot: Slot number for backplane modules.
        overwrite: Shorthand -- when true, sets conflict_resolution='overwrite'.
    """
    prj = _require_project()
    try:
        fp = _normalize_path(file_path)

        if overwrite and conflict_resolution == "report":
            conflict_resolution = "overwrite"

        # Detect if this is a Module file needing the legacy import path
        if module_name:
            _modules.import_module(
                prj, fp, module_name,
                parent_module=parent_module,
                address=module_address or None,
                slot=module_slot or None,
            )
            return f"Imported module '{module_name}' under '{parent_module}'"

        # Detect if this is a standalone AOI/UDT file where the user
        # wants a simple overwrite import (legacy import_aoi/import_udt)
        from .utils import parse_l5x
        source_root = parse_l5x(fp)
        target_type = source_root.get("TargetType", "")

        if target_type == "AddOnInstructionDefinition" and conflict_resolution == "overwrite":
            elem = _aoi.import_aoi(prj, fp, overwrite=True)
            name = elem.get("Name", "?")
            return f"Imported AOI '{name}'"

        if target_type == "DataType" and conflict_resolution == "overwrite":
            elem = _udt.import_udt(prj, fp, overwrite=True)
            name = elem.get("Name", "?")
            return f"Imported UDT '{name}'"

        # General import path with conflict resolution
        result = _comp_import.import_component(
            prj, fp,
            conflict_resolution=conflict_resolution,
            target_program=target_program,
            target_routine=target_routine,
            rung_position=rung_position,
        )
        return json.dumps(result.to_dict(), indent=2)
    except Exception as e:
        return f"Error importing component: {e}"


@mcp.tool()
def analyze_import(file_path: str) -> str:
    """Dry-run conflict analysis for importing a component export file.

    Checks for UDT/AOI definition mismatches, tag type conflicts,
    and name collisions without making any changes to the project.

    Args:
        file_path: Path to the component export .L5X file.
    """
    prj = _require_project()
    try:
        fp = _normalize_path(file_path)
        result = _comp_import.analyze_import(prj, fp)
        return json.dumps(result.to_dict(), indent=2)
    except Exception as e:
        return f"Error analyzing import: {e}"


# ===================================================================
# 6. Validation & Utilities (consolidated)
# ===================================================================

@mcp.tool()
def validate_project() -> str:
    """Run all validation checks on the loaded project.

    Checks structure, references, naming, dependencies, modules, tasks,
    rung syntax, AOI timestamps, and data format completeness.

    Returns a summary with error and warning counts plus details.
    """
    prj = _require_project()
    try:
        result = _validator.validate_project(prj)
        output = {
            "is_valid": result.is_valid,
            "error_count": len(result.errors),
            "warning_count": len(result.warnings),
            "errors": result.errors[:50],
            "warnings": result.warnings[:50],
        }
        return json.dumps(output, indent=2)
    except Exception as e:
        return f"Error running validation: {e}"


@mcp.tool()
def analyze_rung_text(
    rung_text: str,
    action: str = "validate",
    substitutions_json: str = "",
) -> str:
    """Analyze, validate, or transform rung instruction text.

    Replaces the former validate_rung_syntax, substitute_tags_in_rung,
    and extract_tag_references_from_rung tools.

    Args:
        rung_text: The instruction text (e.g. 'XIC(tag1)OTE(tag2);').
        action: What to do with the text:
            'validate'      -- check syntax, return 'Valid' or error list.
            'extract_tags'  -- return sorted list of referenced tag names.
            'substitute'    -- replace tag names using substitutions_json.
        substitutions_json: JSON object mapping old names to new names.
            Required when action='substitute'.
    """
    try:
        if action == "validate":
            errors = _rungs.validate_rung_syntax(rung_text)
            if not errors:
                return "Valid"
            return json.dumps(errors)

        elif action == "extract_tags":
            refs = _rungs.extract_tag_references(rung_text)
            return json.dumps(sorted(refs))

        elif action == "substitute":
            if not substitutions_json:
                return "Error: substitutions_json is required for action='substitute'."
            subs = json.loads(substitutions_json)
            result = _rungs.substitute_tags(rung_text, subs)
            return result

        else:
            return (
                f"Error: Unknown action '{action}'. "
                f"Choose from: validate, extract_tags, substitute."
            )
    except Exception as e:
        return f"Error analyzing rung text: {e}"


# ===================================================================
# 7. Alarm Management (consolidated)
# ===================================================================

@mcp.tool()
def manage_alarms(
    operations_json: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Create, configure, and inspect alarm tags in a single call.

    Replaces the former create_alarm_digital_tag, batch_create_alarm_digital_tags,
    get_alarm_digital_info, configure_alarm_digital_tag,
    get_tag_alarm_conditions, and configure_tag_alarm_condition tools.

    Args:
        operations_json: JSON array of operation objects. Each has an
            'action' field plus action-specific fields:

            create_digital:      {name, message, severity? (1-1000, default 500),
                                  description?, ack_required? (default true),
                                  latched? (default false), tag_class?}
            configure_digital:   {name, severity?, message?, ack_required?,
                                  latched?}  (only specified fields are changed)
            get_info:            {name}
            get_conditions:      {name}
            configure_condition: {tag_name, condition_name, severity?,
                                  on_delay?, off_delay?, used?,
                                  ack_required?, message?}

            Each operation can optionally include 'scope' and 'program_name'.

        scope: Default scope for all operations.
        program_name: Default program for all operations.

    Example:
        [{"action": "create_digital", "name": "Alarm_Conv1",
          "message": "Conveyor 1 Fault", "severity": 750},
         {"action": "create_digital", "name": "Alarm_Conv2",
          "message": "Conveyor 2 Fault", "severity": 750}]
    """
    prj = _require_project()
    try:
        ops = json.loads(operations_json)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON -- {e}"

    results = []
    for i, op in enumerate(ops):
        action = op.get("action", "")
        op_scope = op.get("scope", scope)
        op_prog = op.get("program_name", program_name) or None

        try:
            if action == "create_digital":
                _tags.create_alarm_digital_tag(
                    prj,
                    name=op["name"],
                    message=op["message"],
                    severity=op.get("severity", 500),
                    scope=op_scope,
                    program_name=op_prog,
                    description=op.get("description") or None,
                    ack_required=op.get("ack_required", True),
                    latched=op.get("latched", False),
                    tag_class=op.get("tag_class") or None,
                )
                results.append({"index": i, "status": "ok",
                                "action": "create_digital", "name": op["name"]})

            elif action == "configure_digital":
                kwargs: dict = {}
                if "severity" in op:
                    kwargs["severity"] = op["severity"]
                if "message" in op:
                    kwargs["message"] = op["message"]
                if "ack_required" in op:
                    val = op["ack_required"]
                    kwargs["ack_required"] = (
                        val if isinstance(val, bool)
                        else str(val).lower() == "true"
                    )
                if "latched" in op:
                    val = op["latched"]
                    kwargs["latched"] = (
                        val if isinstance(val, bool)
                        else str(val).lower() == "true"
                    )
                _tags.configure_alarm_digital_tag(
                    prj, op["name"],
                    scope=op_scope, program_name=op_prog,
                    **kwargs,
                )
                changes = ", ".join(f"{k}={v}" for k, v in kwargs.items())
                results.append({"index": i, "status": "ok",
                                "action": "configure_digital",
                                "name": op["name"], "changes": changes})

            elif action == "get_info":
                info = _tags.get_alarm_digital_info(
                    prj, op["name"],
                    scope=op_scope, program_name=op_prog,
                )
                results.append({"index": i, "status": "ok",
                                "action": "get_info", "data": info})

            elif action == "get_conditions":
                conditions = _tags.get_tag_alarm_conditions(
                    prj, op["name"],
                    scope=op_scope, program_name=op_prog,
                )
                results.append({"index": i, "status": "ok",
                                "action": "get_conditions", "data": conditions})

            elif action == "configure_condition":
                kwargs = {}
                if "severity" in op and op["severity"] is not None:
                    kwargs["severity"] = op["severity"]
                if "on_delay" in op and op["on_delay"] is not None:
                    kwargs["on_delay"] = op["on_delay"]
                if "off_delay" in op and op["off_delay"] is not None:
                    kwargs["off_delay"] = op["off_delay"]
                if "used" in op:
                    val = op["used"]
                    kwargs["used"] = (
                        val if isinstance(val, bool)
                        else str(val).lower() == "true"
                    )
                if "ack_required" in op:
                    val = op["ack_required"]
                    kwargs["ack_required"] = (
                        val if isinstance(val, bool)
                        else str(val).lower() == "true"
                    )
                if "message" in op and op["message"]:
                    kwargs["message"] = op["message"]

                _tags.configure_tag_alarm_condition(
                    prj, op["tag_name"], op["condition_name"],
                    scope=op_scope, program_name=op_prog,
                    **kwargs,
                )
                changes = ", ".join(f"{k}={v}" for k, v in kwargs.items())
                results.append({"index": i, "status": "ok",
                                "action": "configure_condition",
                                "tag": op["tag_name"],
                                "condition": op["condition_name"],
                                "changes": changes})

            else:
                results.append({"index": i, "status": "error",
                                "message": f"Unknown action: {action}"})
        except Exception as e:
            results.append({"index": i, "status": "error", "action": action,
                            "message": str(e)})

    succeeded = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "error")
    return json.dumps(
        {"succeeded": succeeded, "failed": failed, "details": results},
        indent=2,
    )


@mcp.tool()
def manage_alarm_definitions(
    action: str,
    data_type_name: str = "",
    members_json: str = "",
) -> str:
    """Manage DatatypeAlarmDefinitions for data types (UDTs/AOIs).

    Replaces the former list_alarm_definitions, create_alarm_definition,
    and remove_alarm_definition tools.

    Args:
        action: 'list', 'create', 'remove', or 'get'.
        data_type_name: Required for 'create', 'remove', and 'get'.
        members_json: Required for 'create'. JSON array of member alarm
            definition objects. Each should have: name, input (starts with '.'),
            condition_type, and optionally: severity (default 500),
            on_delay (default 0), off_delay (default 0), message,
            ack_required (default false), expression (default '= 1').
    """
    prj = _require_project()
    try:
        if action == "list":
            results = prj.list_alarm_definitions()
            if not results:
                return "No alarm definitions found in the project."
            return json.dumps(results, indent=2)

        elif action == "get":
            if not data_type_name:
                return "Error: data_type_name is required for action='get'."
            result = prj.get_alarm_definition(data_type_name)
            return json.dumps(result, indent=2)

        elif action == "create":
            if not data_type_name:
                return "Error: data_type_name is required for action='create'."
            if not members_json:
                return "Error: members_json is required for action='create'."
            members = json.loads(members_json)
            prj.create_alarm_definition(data_type_name, members)
            return (
                f"Created alarm definition for '{data_type_name}' "
                f"with {len(members)} member alarm(s)"
            )

        elif action == "remove":
            if not data_type_name:
                return "Error: data_type_name is required for action='remove'."
            removed = prj.remove_alarm_definition(data_type_name)
            count = len(removed.findall("MemberAlarmDefinition"))
            return (
                f"Removed alarm definition for '{data_type_name}' "
                f"({count} member alarm(s) removed)"
            )

        else:
            return (
                f"Error: Unknown action '{action}'. "
                f"Choose from: list, create, remove, get."
            )
    except Exception as e:
        return f"Error managing alarm definitions: {e}"


@mcp.tool()
def list_alarms(
    alarm_type: str = "",
    scope: str = "",
    program_name: str = "",
) -> str:
    """List all alarm tags and alarm conditions in the project.

    Returns ALARM_DIGITAL tags, ALARM_ANALOG tags, and tags with
    AlarmConditions. Optionally filter by type and scope.

    Args:
        alarm_type: Filter: 'digital', 'analog', 'condition', or '' for all.
        scope: 'controller', 'program', or '' for all scopes.
        program_name: Filter to a specific program (when scope='program').
    """
    prj = _require_project()
    try:
        results = _tags.list_alarms(
            prj,
            alarm_type=alarm_type or None,
            scope=scope or None,
            program_name=program_name or None,
        )
        return json.dumps(results, indent=2)
    except Exception as e:
        return f"Error listing alarms: {e}"


# ===================================================================
# 8. Component Export (consolidated)
# ===================================================================

@mcp.tool()
def create_export_shell(
    export_type: str,
    program_name: str = "ExportedProgram",
    routine_name: str = "MainRoutine",
    routine_type: str = "RLL",
) -> str:
    """Create an empty export shell in memory and load it as the active project.

    Replaces the former create_rung_export, create_routine_export, and
    create_program_export tools.

    The resulting in-memory project can be populated with manage_tags,
    manage_rungs, etc., then written out with save_project.

    Args:
        export_type: Type of export shell -- 'rung', 'routine', or 'program'.
        program_name: Name for the context program.
        routine_name: Name for the context routine (rung/routine types).
        routine_type: Routine type for routine exports ('RLL', 'ST', etc.).
    """
    global _project, _project_path
    try:
        source = _project if _project is not None else None

        if export_type == "rung":
            prj = _comp_export.create_rung_export(
                project=source,
                program_name=program_name,
                routine_name=routine_name,
            )
        elif export_type == "routine":
            prj = _comp_export.create_routine_export(
                project=source,
                program_name=program_name,
                routine_name=routine_name,
                routine_type=routine_type,
            )
        elif export_type == "program":
            prj = _comp_export.create_program_export(
                project=source,
                program_name=program_name,
            )
        else:
            return (
                f"Error: Unknown export_type '{export_type}'. "
                f"Choose from: rung, routine, program."
            )

        _project = prj
        _project_path = None
        return (
            f"Created empty {export_type} export in memory "
            f"(program='{program_name}'). "
            f"Use manage_tags/manage_rungs to populate, then save_project."
        )
    except Exception as e:
        return f"Error creating export shell: {e}"


@mcp.tool()
def export_component(
    component_type: str,
    name: str = "",
    program_name: str = "",
    routine_name: str = "",
    scope: str = "controller",
    file_path: str = "",
    include_tags: bool = True,
) -> str:
    """Extract a component from the project into a standalone L5X export file.

    Replaces the former export_rung, export_routine, export_program,
    export_tag, export_udt, and export_aoi tools.

    Args:
        component_type: What to export -- 'rung', 'routine', 'program',
                        'tag', 'udt', 'aoi'.
        name: Entity name or identifiers:
              - For rungs: comma-separated rung indices (e.g. '0,1,2').
              - For routine/program/tag/udt/aoi: the entity name.
        program_name: Program containing the routine/rungs, or the
                      program for program-scope tags.
        routine_name: Routine name (for rung/routine exports).
        scope: Tag scope ('controller' or 'program'). Only for tag exports.
        file_path: Output file path. If empty, auto-generates a name.
        include_tags: For rung/routine exports, include referenced tags
                      and type dependencies.
    """
    prj = _require_project()
    try:
        fp = _normalize_path(file_path) if file_path else ""

        if component_type == "rung":
            nums = [int(n.strip()) for n in name.split(",") if n.strip()]
            result = _comp_export.export_rung(
                prj, program_name, routine_name, nums,
                file_path=fp, include_tags=include_tags,
            )
            return f"Exported {len(nums)} rung(s) to: {result}"

        elif component_type == "routine":
            result = _comp_export.export_routine(
                prj, program_name, routine_name or name,
                file_path=fp, include_tags=include_tags,
            )
            return f"Exported routine '{routine_name or name}' to: {result}"

        elif component_type == "program":
            result = _comp_export.export_program(
                prj, program_name or name, file_path=fp,
            )
            return f"Exported program '{program_name or name}' to: {result}"

        elif component_type == "tag":
            result = _comp_export.export_tag(
                prj, name,
                scope=scope,
                program_name=program_name,
                file_path=fp,
            )
            return f"Exported tag '{name}' to: {result}"

        elif component_type == "udt":
            result = _comp_export.export_udt(
                prj, name, file_path=fp,
            )
            return f"Exported UDT '{name}' to: {result}"

        elif component_type == "aoi":
            result = _comp_export.export_aoi(
                prj, name, file_path=fp,
            )
            return f"Exported AOI '{name}' to: {result}"

        else:
            return (
                f"Error: Unknown component_type '{component_type}'. "
                f"Choose from: rung, routine, program, tag, udt, aoi."
            )
    except Exception as e:
        return f"Error exporting component: {e}"


# ===================================================================
# 9. Analysis & Cross-Reference Tools
# ===================================================================

def _parse_aoi_calls_from_rung(rung_text: str, known_aois: set) -> list:
    """Parse a rung text and extract AOI instruction calls with argument mapping.

    Returns a list of dicts:
      {'aoi_name': str, 'arguments': [str, ...]}
    where arguments[0] is the instance tag and subsequent entries are the
    wired parameter values in declaration order.
    """
    tokens = _rungs.tokenize(rung_text)
    calls = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if (tok.type == _rungs.TokenType.INSTRUCTION
                and tok.value in known_aois):
            # Collect arguments inside the parentheses
            aoi_name = tok.value
            args = []
            # Next token should be OPEN_PAREN
            j = i + 1
            if j < len(tokens) and tokens[j].type == _rungs.TokenType.OPEN_PAREN:
                depth = 1
                j += 1
                current_arg_parts = []
                while j < len(tokens) and depth > 0:
                    t = tokens[j]
                    if t.type == _rungs.TokenType.OPEN_PAREN:
                        depth += 1
                        current_arg_parts.append(t.value)
                    elif t.type == _rungs.TokenType.CLOSE_PAREN:
                        depth -= 1
                        if depth == 0:
                            if current_arg_parts:
                                args.append(''.join(current_arg_parts))
                        else:
                            current_arg_parts.append(t.value)
                    elif t.type == _rungs.TokenType.COMMA and depth == 1:
                        args.append(''.join(current_arg_parts))
                        current_arg_parts = []
                    else:
                        current_arg_parts.append(t.value)
                    j += 1
            calls.append({'aoi_name': aoi_name, 'arguments': args})
            i = j
        else:
            i += 1
    return calls


@mcp.tool()
def get_scope_references(
    program_name: str,
    routine_name: str = "",
    rung_range: str = "",
    include_tag_info: bool = True,
) -> str:
    """Return all tags and AOI instances referenced within a code scope.

    Answers the question: "What does this routine/rung range touch?"
    Returns every unique tag with its metadata, which rungs use it,
    and for AOI-bound tags, the parameter binding context.

    Args:
        program_name: The program to analyze.
        routine_name: Specific routine (or empty to scan all routines
                      in the program).
        rung_range: Rung filter -- '' for all, '3-7' for range,
                    '0,2,5' for specific rungs. Ignored for non-RLL.
        include_tag_info: If true, resolve each tag's data_type, scope,
                          description. If false, return names only.

    Returns:
        JSON with:
        - tags: list of {name, data_type, scope, description, rungs: [int...]}
        - aoi_calls: list of {aoi_name, rung, instance_tag, bindings: [{
            parameter, usage, required, wired_tag}]}
        - summary: {unique_tags, controller_tags, program_tags, aoi_calls}
    """
    prj = _require_project()
    try:
        # Determine which rungs to scan
        rung_texts: list[tuple[int, str]] = []  # (rung_number, text)

        if routine_name:
            all_rungs = prj.get_all_rungs(program_name, routine_name)
            for r in all_rungs:
                rung_texts.append((r['number'], r['text']))
        else:
            # Scan all routines in the program
            routines_info = prj.list_routines(program_name)
            for rinfo in routines_info:
                if rinfo.get('type', 'RLL') == 'RLL':
                    for r in prj.get_all_rungs(program_name, rinfo['name']):
                        rung_texts.append((r['number'], r['text']))

        # Apply rung_range filter
        if rung_range:
            allowed: set[int] = set()
            for part in rung_range.split(','):
                part = part.strip()
                if '-' in part:
                    lo, hi = part.split('-', 1)
                    allowed.update(range(int(lo.strip()), int(hi.strip()) + 1))
                else:
                    allowed.add(int(part))
            rung_texts = [(n, t) for n, t in rung_texts if n in allowed]

        # Extract tag references per rung
        tag_rungs: dict[str, list[int]] = {}  # tag_name -> [rung_numbers]
        for rung_num, text in rung_texts:
            refs = _rungs.extract_tag_references(text)
            for tag_name in refs:
                tag_rungs.setdefault(tag_name, []).append(rung_num)

        # Build known AOI names for call detection
        known_aois: set[str] = set()
        try:
            aoi_list = prj.list_aois()
            known_aois = {a['name'] for a in aoi_list}
        except Exception:
            pass

        # Extract AOI calls with parameter bindings
        aoi_calls_result = []
        for rung_num, text in rung_texts:
            calls = _parse_aoi_calls_from_rung(text, known_aois)
            for call in calls:
                aoi_name = call['aoi_name']
                args = call['arguments']
                bindings = []
                try:
                    params = _aoi.get_aoi_parameters(prj, aoi_name)
                    # Filter to visible, non-system params
                    visible_params = [
                        p for p in params
                        if p.get('visible', True)
                        and p['name'] not in ('EnableIn', 'EnableOut')
                    ]
                    # args[0] is the instance tag; params map to args[1:]
                    param_args = args[1:]
                    for idx, param in enumerate(visible_params):
                        wired = param_args[idx] if idx < len(param_args) else '?'
                        bindings.append({
                            'parameter': param['name'],
                            'data_type': param['data_type'],
                            'usage': param['usage'],
                            'required': param['required'],
                            'wired_tag': wired,
                        })
                except Exception:
                    # AOI not found or params can't be read
                    for idx, arg in enumerate(args):
                        bindings.append({
                            'parameter': f'arg{idx}',
                            'wired_tag': arg,
                        })

                instance_tag = args[0] if args else '?'
                aoi_calls_result.append({
                    'aoi_name': aoi_name,
                    'rung': rung_num,
                    'instance_tag': instance_tag,
                    'bindings': bindings,
                })

        # Resolve tag info if requested
        tags_result = []
        ctrl_count = 0
        prog_count = 0
        for tag_name, rungs_list in sorted(tag_rungs.items()):
            entry: dict = {'name': tag_name, 'rungs': sorted(set(rungs_list))}
            if include_tag_info:
                try:
                    info = _tags.find_tag(prj, tag_name)
                    entry['data_type'] = info.get('data_type', '')
                    entry['scope'] = info.get('scope', '')
                    entry['program'] = info.get('program', '')
                    entry['description'] = info.get('description', '')
                    if info.get('scope') == 'controller':
                        ctrl_count += 1
                    else:
                        prog_count += 1
                except Exception:
                    entry['data_type'] = '?'
                    entry['scope'] = '?'
            tags_result.append(entry)

        result = {
            'tags': tags_result,
            'aoi_calls': aoi_calls_result,
            'summary': {
                'unique_tags': len(tags_result),
                'controller_tags': ctrl_count,
                'program_tags': prog_count,
                'aoi_calls': len(aoi_calls_result),
            },
        }
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error getting scope references: {e}"


@mcp.tool()
def find_references(
    names_json: str,
    entity_type: str = "tag",
) -> str:
    """Find where one or more entities are referenced across the project.

    Batch reverse-lookup: for a list of tag names, returns every rung/ST line
    that references each one. Also supports searching for AOI invocations
    and UDT usage.

    Args:
        names_json: JSON array of entity names, e.g. '["Tag1", "Tag2"]'.
                    Also accepts a single string: '"Tag1"'.
        entity_type: What to search for:
            'tag' -- find rung/ST references to these tag names.
            'aoi' -- find rungs that invoke these AOI instructions.
            'udt' -- find tags whose DataType matches these UDT names.

    Returns:
        JSON object mapping each name to its references:
        For tags/aois: {name: [{program, routine, rung/line, text}]}
        For udts: {name: [{tag_name, scope, program?}]}
    """
    prj = _require_project()
    try:
        raw = json.loads(names_json)
        names = [raw] if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON -- {e}"

    try:
        result: dict = {}

        if entity_type == "tag":
            for name in names:
                refs = prj.find_tag_references(name)
                result[name] = refs

        elif entity_type == "aoi":
            # For AOIs, we search for the instruction name in rung text
            for name in names:
                refs = prj.find_tag_references(name)
                # Also do a more targeted search: AOI calls look like
                # AOIName(instance,...) so we search for the pattern
                import re
                pattern = re.compile(
                    rf'(?<![A-Za-z0-9_]){re.escape(name)}\(',
                )
                aoi_refs = []
                for prog_elem in prj._all_program_elements():
                    prog_name = prog_elem.get('Name', '')
                    routines_el = prog_elem.find('Routines')
                    if routines_el is None:
                        continue
                    for routine in routines_el.findall('Routine'):
                        routine_name = routine.get('Name', '')
                        rll = routine.find('RLLContent')
                        if rll is not None:
                            for rung in rll.findall('Rung'):
                                text_el = rung.find('Text')
                                if text_el is not None and text_el.text:
                                    text = text_el.text.strip()
                                    if pattern.search(text):
                                        aoi_refs.append({
                                            'program': prog_name,
                                            'routine': routine_name,
                                            'rung': int(rung.get('Number', '0')),
                                            'text': text,
                                        })
                        st = routine.find('STContent')
                        if st is not None:
                            for line_el in st.findall('Line'):
                                if line_el.text and pattern.search(line_el.text.strip()):
                                    aoi_refs.append({
                                        'program': prog_name,
                                        'routine': routine_name,
                                        'line': int(line_el.get('Number', '0')),
                                        'text': line_el.text.strip(),
                                    })
                result[name] = aoi_refs

        elif entity_type == "udt":
            # For UDTs, find all tags whose DataType matches
            for name in names:
                matches = []
                # Controller tags
                for t in prj.list_controller_tags():
                    if t.get('data_type', '').lower() == name.lower():
                        matches.append({
                            'tag_name': t['name'],
                            'scope': 'controller',
                        })
                # Program tags
                for p in prj.list_programs():
                    for t in prj.list_program_tags(p):
                        if t.get('data_type', '').lower() == name.lower():
                            matches.append({
                                'tag_name': t['name'],
                                'scope': 'program',
                                'program': p,
                            })
                result[name] = matches

        else:
            return (
                f"Error: Unknown entity_type '{entity_type}'. "
                f"Choose from: tag, aoi, udt."
            )

        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error finding references: {e}"


@mcp.tool()
def get_tag_values(
    names_json: str,
    scope: str = "controller",
    program_name: str = "",
    include_members: bool = False,
    include_aoi_context: bool = False,
    name_filter: str = "",
) -> str:
    """Get values and metadata for one or more tags in a single call.

    Answers the question: "Show me everything about these tags" without
    multiple round-trips. Supports glob patterns to match tag groups.

    Args:
        names_json: JSON array of tag names, e.g. '["Tag1", "Tag2"]'.
                    Use '[]' (empty array) with name_filter to query
                    by pattern instead.
        scope: Default scope for lookups ('controller', 'program', or
               '' to search all).
        program_name: Required when scope is 'program'.
        include_members: If true, expand structured types into full
                         member trees with values (TIMER, UDT, etc.).
        include_aoi_context: If true and a tag is wired as an AOI
                             parameter in any rung, include which AOI,
                             which parameter, usage (Input/Output/InOut),
                             and whether it's required.
        name_filter: Glob pattern to select tags (e.g. 'Conv1_*').
                     Applied to the specified scope. Overrides names_json
                     if names_json is empty.

    Returns:
        JSON array of tag objects, each with:
        - name, data_type, scope, description, value, radix
        - members: (if include_members) dict of member values
        - aoi_context: (if include_aoi_context) list of {aoi_name,
          parameter, usage, required, program, routine, rung}
    """
    prj = _require_project()
    try:
        raw = json.loads(names_json)
        names = [raw] if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON -- {e}"

    try:
        # If names is empty but name_filter is provided, discover names
        if not names and name_filter:
            all_tags = []
            if scope in ('controller', ''):
                all_tags.extend(prj.list_controller_tags())
            if scope in ('program', ''):
                if program_name:
                    all_tags.extend(prj.list_program_tags(program_name))
                elif scope == '':
                    for p in prj.list_programs():
                        for t in prj.list_program_tags(p):
                            t['_program'] = p
                            all_tags.append(t)
            names = [
                t['name'] for t in all_tags
                if fnmatch.fnmatch(t['name'], name_filter)
            ]

        # Build AOI call index if needed
        aoi_tag_bindings: dict[str, list] = {}  # tag_name -> [{aoi info}]
        if include_aoi_context:
            known_aois: set[str] = set()
            try:
                known_aois = {a['name'] for a in prj.list_aois()}
            except Exception:
                pass

            if known_aois:
                # Pre-cache AOI parameter lists
                aoi_params_cache: dict[str, list] = {}
                for aoi_name in known_aois:
                    try:
                        params = _aoi.get_aoi_parameters(prj, aoi_name)
                        aoi_params_cache[aoi_name] = [
                            p for p in params
                            if p.get('visible', True)
                            and p['name'] not in ('EnableIn', 'EnableOut')
                        ]
                    except Exception:
                        pass

                # Scan all rungs for AOI calls
                for prog_elem in prj._all_program_elements():
                    pname = prog_elem.get('Name', '')
                    routines_el = prog_elem.find('Routines')
                    if routines_el is None:
                        continue
                    for routine in routines_el.findall('Routine'):
                        rname = routine.get('Name', '')
                        rll = routine.find('RLLContent')
                        if rll is None:
                            continue
                        for rung in rll.findall('Rung'):
                            text_el = rung.find('Text')
                            if text_el is None or not text_el.text:
                                continue
                            rung_num = int(rung.get('Number', '0'))
                            calls = _parse_aoi_calls_from_rung(
                                text_el.text.strip(), known_aois,
                            )
                            for call in calls:
                                aoi_name = call['aoi_name']
                                args = call['arguments']
                                params = aoi_params_cache.get(aoi_name, [])
                                # args[0] is instance tag; params map to args[1:]
                                param_args = args[1:]
                                for idx, arg in enumerate(param_args):
                                    base = _rungs._base_tag_name(arg)
                                    if base == '?':
                                        continue
                                    param_info = params[idx] if idx < len(params) else None
                                    binding = {
                                        'aoi_name': aoi_name,
                                        'parameter': param_info['name'] if param_info else f'arg{idx}',
                                        'usage': param_info['usage'] if param_info else '?',
                                        'required': param_info['required'] if param_info else False,
                                        'program': pname,
                                        'routine': rname,
                                        'rung': rung_num,
                                    }
                                    aoi_tag_bindings.setdefault(base, []).append(binding)

        # Build result for each tag
        results = []
        for tag_name in names:
            entry: dict = {'name': tag_name}
            try:
                if scope == '':
                    info = _tags.find_tag(prj, tag_name)
                else:
                    info = _tags.get_tag_info(
                        prj, tag_name,
                        scope=scope,
                        program_name=program_name or None,
                    )
                entry['data_type'] = info.get('data_type', '')
                entry['scope'] = info.get('scope', scope)
                entry['program'] = info.get('program', '')
                entry['description'] = info.get('description', '')
                entry['radix'] = info.get('radix', '')
                entry['tag_type'] = info.get('tag_type', 'Base')
                entry['alias_for'] = info.get('alias_for')

                # Value
                val = info.get('value')
                if include_members and isinstance(val, dict):
                    entry['value'] = None
                    entry['members'] = val
                else:
                    entry['value'] = val

            except Exception as e:
                entry['error'] = str(e)

            # AOI context
            if include_aoi_context and tag_name in aoi_tag_bindings:
                entry['aoi_context'] = aoi_tag_bindings[tag_name]

            results.append(entry)

        return json.dumps(results, indent=2)
    except Exception as e:
        return f"Error getting tag values: {e}"


@mcp.tool()
def detect_conflicts(
    check: str = "all",
    aoi_name: str = "",
    address_member: str = "",
    array_member: str = "",
) -> str:
    """Detect potential conflicts and issues in the project.

    Performs domain-specific checks that go beyond basic validation,
    looking for logical conflicts that could cause runtime issues.

    Args:
        check: Which checks to run:
            'all' -- run all checks below.
            'aoi_address' -- find AOI instances that share the same I/O
                array tag AND address offset (ASI bus collision risk).
            'tag_shadowing' -- find program-scope tags whose names match
                controller-scope tags (legal but confusing).
            'unused_tags' -- find tags not referenced in any code.
            'scope_duplicates' -- find identical tag names across
                different programs.
        aoi_name: For 'aoi_address': filter to specific AOI type.
                  If empty, checks all AOI types.
        address_member: For 'aoi_address': the member name that holds the
                        address offset (e.g. 'AddressOffset', 'ASIAddress',
                        'Address'). If empty, auto-detects common patterns.
        array_member: For 'aoi_address': the member name that holds the
                      I/O array reference. If empty, auto-detects.

    Returns:
        JSON with check results and conflict details.
    """
    prj = _require_project()
    try:
        checks = (
            ['aoi_address', 'tag_shadowing', 'unused_tags', 'scope_duplicates']
            if check == 'all'
            else [check]
        )

        result: dict = {}

        # ---------------------------------------------------------------
        # AOI Address Conflict Detection
        # ---------------------------------------------------------------
        if 'aoi_address' in checks:
            aoi_conflicts = []

            # Determine which AOI types to check
            aoi_types_to_check: list[str] = []
            if aoi_name:
                aoi_types_to_check = [aoi_name]
            else:
                try:
                    aoi_types_to_check = [a['name'] for a in prj.list_aois()]
                except Exception:
                    pass

            for aoi_type in aoi_types_to_check:
                # Get AOI parameters to find address/array members
                try:
                    params = _aoi.get_aoi_parameters(prj, aoi_type)
                except Exception:
                    continue

                param_names = {p['name'].lower(): p['name'] for p in params}

                # Auto-detect address member
                addr_member = address_member
                if not addr_member:
                    for candidate in ['addressoffset', 'asiaddress', 'address',
                                      'addr', 'offset', 'nodeaddress',
                                      'startaddress', 'baseaddress']:
                        if candidate in param_names:
                            addr_member = param_names[candidate]
                            break

                # Auto-detect array/buffer member
                arr_member = array_member
                if not arr_member:
                    for candidate in ['inputarray', 'outputarray', 'asiinput',
                                      'asioutput', 'inputbuffer', 'outputbuffer',
                                      'databuffer', 'ioarray', 'data',
                                      'inarray', 'outarray']:
                        if candidate in param_names:
                            arr_member = param_names[candidate]
                            break

                if not addr_member and not arr_member:
                    continue  # This AOI doesn't have address-like params

                # Find all instances of this AOI type
                instances: list[dict] = []

                # Search controller tags
                for t in prj.list_controller_tags():
                    if t.get('data_type', '').lower() == aoi_type.lower():
                        instances.append({
                            'tag_name': t['name'],
                            'scope': 'controller',
                        })

                # Search program tags
                for p in prj.list_programs():
                    for t in prj.list_program_tags(p):
                        if t.get('data_type', '').lower() == aoi_type.lower():
                            instances.append({
                                'tag_name': t['name'],
                                'scope': 'program',
                                'program': p,
                            })

                if len(instances) < 2:
                    continue  # Can't have conflicts with < 2 instances

                # Read address/array values for each instance
                keyed: dict[str, list] = {}  # (array_tag, offset) -> [instances]
                for inst in instances:
                    try:
                        addr_val = None
                        arr_val = None
                        tag_name = inst['tag_name']
                        sc = inst['scope']
                        pg = inst.get('program')

                        if addr_member:
                            try:
                                addr_val = prj.get_tag_member_value(
                                    tag_name, addr_member,
                                    scope=sc, program_name=pg,
                                )
                            except Exception:
                                pass

                        if arr_member:
                            try:
                                arr_val = prj.get_tag_member_value(
                                    tag_name, arr_member,
                                    scope=sc, program_name=pg,
                                )
                            except Exception:
                                pass

                        # Also check rung text for wired array tags
                        # (InOut params appear in rung text, not decorated data)
                        if arr_val is None or addr_val is None:
                            refs = prj.find_tag_references(tag_name)
                            known_aois_set = {aoi_type}
                            for ref in refs:
                                text = ref.get('text', '')
                                calls = _parse_aoi_calls_from_rung(
                                    text, known_aois_set,
                                )
                                for call in calls:
                                    args = call['arguments']
                                    try:
                                        vis_params = _aoi.get_aoi_parameters(
                                            prj, aoi_type,
                                        )
                                        vis_params = [
                                            p for p in vis_params
                                            if p.get('visible', True)
                                            and p['name'] not in (
                                                'EnableIn', 'EnableOut',
                                            )
                                        ]
                                        for idx, p in enumerate(vis_params):
                                            if (p['name'] == arr_member
                                                    and idx < len(args)
                                                    and arr_val is None):
                                                arr_val = args[idx]
                                            if (p['name'] == addr_member
                                                    and idx < len(args)
                                                    and addr_val is None):
                                                try:
                                                    addr_val = int(args[idx])
                                                except (ValueError, TypeError):
                                                    addr_val = args[idx]
                                    except Exception:
                                        pass

                        key = f"{arr_val}@{addr_val}"
                        keyed.setdefault(key, []).append({
                            **inst,
                            'address_value': addr_val,
                            'array_value': arr_val,
                        })
                    except Exception:
                        pass

                # Report groups with 2+ instances
                for key, group in keyed.items():
                    if len(group) >= 2:
                        aoi_conflicts.append({
                            'aoi_type': aoi_type,
                            'address_member': addr_member,
                            'array_member': arr_member,
                            'shared_key': key,
                            'instance_count': len(group),
                            'instances': group,
                        })

            result['aoi_address'] = {
                'conflicts_found': len(aoi_conflicts),
                'conflicts': aoi_conflicts,
            }

        # ---------------------------------------------------------------
        # Tag Shadowing (program tag hides controller tag)
        # ---------------------------------------------------------------
        if 'tag_shadowing' in checks:
            shadows = []
            ctrl_names = {
                t['name'].lower(): t
                for t in prj.list_controller_tags()
            }

            for p in prj.list_programs():
                for t in prj.list_program_tags(p):
                    if t['name'].lower() in ctrl_names:
                        ctrl_tag = ctrl_names[t['name'].lower()]
                        shadows.append({
                            'tag_name': t['name'],
                            'program': p,
                            'program_data_type': t.get('data_type', ''),
                            'controller_data_type': ctrl_tag.get('data_type', ''),
                            'types_match': (
                                t.get('data_type', '').lower()
                                == ctrl_tag.get('data_type', '').lower()
                            ),
                        })

            result['tag_shadowing'] = {
                'shadows_found': len(shadows),
                'shadows': shadows,
            }

        # ---------------------------------------------------------------
        # Unused Tags
        # ---------------------------------------------------------------
        if 'unused_tags' in checks:
            unused_ctrl = prj.find_unused_tags(scope='controller')
            unused_prog: dict[str, list] = {}
            for p in prj.list_programs():
                unused = prj.find_unused_tags(scope='program', program_name=p)
                if unused:
                    unused_prog[p] = unused

            result['unused_tags'] = {
                'controller_unused': len(unused_ctrl),
                'controller_tags': unused_ctrl,
                'programs': {
                    p: {'count': len(tags), 'tags': tags}
                    for p, tags in unused_prog.items()
                },
            }

        # ---------------------------------------------------------------
        # Scope Duplicates (same tag name in multiple programs)
        # ---------------------------------------------------------------
        if 'scope_duplicates' in checks:
            # Build map: lower(tag_name) -> [(program, data_type)]
            name_map: dict[str, list] = {}
            for p in prj.list_programs():
                for t in prj.list_program_tags(p):
                    key = t['name'].lower()
                    name_map.setdefault(key, []).append({
                        'program': p,
                        'name': t['name'],
                        'data_type': t.get('data_type', ''),
                    })

            dupes = []
            for key, entries in name_map.items():
                if len(entries) >= 2:
                    dupes.append({
                        'tag_name': entries[0]['name'],
                        'occurrences': entries,
                        'types_consistent': len(
                            {e['data_type'].lower() for e in entries}
                        ) == 1,
                    })

            result['scope_duplicates'] = {
                'duplicates_found': len(dupes),
                'duplicates': dupes,
            }

        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error detecting conflicts: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server on stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
