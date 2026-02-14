"""
MCP Server for L5X Agent Toolkit.

Exposes validated L5X file manipulation tools via the Model Context Protocol,
allowing any MCP-compatible AI client (Claude Desktop, Claude Code, etc.) to
perform hyper-accurate PLC project modifications through natural language.

Usage:
    python -m l5x_agent_toolkit.mcp_server
    # or
    python l5x_agent_toolkit/mcp_server.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
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
        "Studio 5000 L5X project files. The AI never touches raw XML -- "
        "every operation produces structurally correct output. "
        "Always call load_project first before using any other tool."
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
                "Use get_aoi_info/get_aoi_parameters to inspect. "
                "To use this AOI, load a full project and use import_aoi."
            )
        elif target_type == 'DataType':
            lines.append(
                f"This is a UDT export file. UDTs: {summary['udt_count']}"
            )
            lines.append(
                "Use get_udt_info/get_udt_members to inspect. "
                "To use this UDT, load a full project and use import_udt."
            )
        elif target_type == 'Module':
            lines.append(
                f"This is a Module export file. Modules: {summary['module_count']}"
            )
            lines.append(
                "To use this module, load a full project and use import_module."
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

    This is optional — Studio 5000 does not require pretty formatting,
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
# 2. Query Tools
# ===================================================================

@mcp.tool()
def list_programs() -> str:
    """List all program names in the project."""
    prj = _require_project()
    return json.dumps(prj.list_programs())


@mcp.tool()
def list_routines(program_name: str) -> str:
    """List all routines in a program with their types (RLL, ST, etc.).

    Args:
        program_name: Name of the program.
    """
    prj = _require_project()
    return json.dumps(prj.list_routines(program_name))


@mcp.tool()
def list_controller_tags() -> str:
    """List all controller-scope tags with name, data type, and description."""
    prj = _require_project()
    return json.dumps(prj.list_controller_tags())


@mcp.tool()
def list_program_tags(program_name: str) -> str:
    """List all tags in a specific program.

    Args:
        program_name: Name of the program.
    """
    prj = _require_project()
    return json.dumps(prj.list_program_tags(program_name))


@mcp.tool()
def list_modules() -> str:
    """List all I/O modules with catalog numbers and parent info."""
    prj = _require_project()
    return json.dumps(prj.list_modules())


@mcp.tool()
def list_aois() -> str:
    """List all Add-On Instruction definitions with names and revisions."""
    prj = _require_project()
    return json.dumps(prj.list_aois())


@mcp.tool()
def list_udts() -> str:
    """List all User-Defined Types with names and member counts."""
    prj = _require_project()
    return json.dumps(prj.list_udts())


@mcp.tool()
def list_tasks() -> str:
    """List all tasks with type, priority, rate, and scheduled programs."""
    prj = _require_project()
    return json.dumps(prj.list_tasks())


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
def get_tag_info(
    name: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Get detailed information about a specific tag.

    Returns name, data type, dimensions, description, value, radix, etc.

    Args:
        name: Tag name.
        scope: 'controller' or 'program'.
        program_name: Required when scope is 'program'.
    """
    prj = _require_project()
    prog = program_name if program_name else None
    info = _tags.get_tag_info(prj, name, scope=scope, program_name=prog)
    return json.dumps(info)


@mcp.tool()
def find_tag(name: str) -> str:
    """Find a tag by name across all scopes (controller and every program).

    Searches controller scope first, then all program scopes. Returns
    full tag details including which scope the tag was found in and all
    member values for structured types (UDTs, TIMER, COUNTER, etc.).

    Use this when you don't know which scope a tag belongs to.

    Args:
        name: Tag name to search for.
    """
    prj = _require_project()
    info = _tags.find_tag(prj, name)
    return json.dumps(info)


@mcp.tool()
def get_tag_member_value(
    name: str,
    member_path: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Read a specific member value from a structured or array tag.

    Use dot notation for structure members (e.g. 'PRE', 'Status.Active')
    and brackets for array indices (e.g. '[0]', '[2].EN').

    Args:
        name: Tag name.
        member_path: Path to the member (e.g. 'PRE', '[0]', '[2].EN').
        scope: 'controller' or 'program'.
        program_name: Required when scope is 'program'.
    """
    prj = _require_project()
    prog = program_name if program_name else None
    value = prj.get_tag_member_value(name, member_path, scope=scope,
                                     program_name=prog)
    return json.dumps({"member": f"{name}.{member_path}", "value": value})


# ===================================================================
# 3. Tag Operations
# ===================================================================

@mcp.tool()
def create_tag(
    name: str,
    data_type: str,
    scope: str = "controller",
    program_name: str = "",
    dimensions: str = "",
    description: str = "",
    radix: str = "",
    tag_class: str = "",
) -> str:
    """Create a new tag in the project.

    Supports all L5X data types: BOOL, SINT, INT, DINT, REAL, LREAL,
    STRING, TIMER, COUNTER, CONTROL, plus any UDT or AOI defined in the
    project. For arrays, specify dimensions (e.g. '10' or '3,4').

    Args:
        name: Tag name (letters, digits, underscore; max 40 chars).
        data_type: L5X data type name.
        scope: 'controller' or 'program'.
        program_name: Required when scope is 'program'.
        dimensions: Array dimensions (e.g. '10', '3,4'). Empty for scalar.
        description: Optional description text.
        radix: Display radix override (Decimal, Hex, Binary, etc.).
        tag_class: 'Standard' or 'Safety'. Auto-detected when empty:
            controller scope defaults to Standard; program scope auto-detects
            Safety from program type.
    """
    prj = _require_project()
    try:
        _tags.create_tag(
            prj, name, data_type,
            scope=scope,
            program_name=program_name or None,
            dimensions=dimensions or None,
            description=description or None,
            radix=radix or None,
            tag_class=tag_class or None,
        )
        return f"Created tag '{name}' (type={data_type}, scope={scope})"
    except Exception as e:
        return f"Error creating tag: {e}"


@mcp.tool()
def delete_tag(
    name: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Delete a tag from the project.

    Args:
        name: Tag name to delete.
        scope: 'controller' or 'program'.
        program_name: Required when scope is 'program'.
    """
    prj = _require_project()
    try:
        _tags.delete_tag(prj, name, scope=scope, program_name=program_name or None)
        return f"Deleted tag '{name}'"
    except Exception as e:
        return f"Error deleting tag: {e}"


@mcp.tool()
def rename_tag(
    old_name: str,
    new_name: str,
    scope: str = "controller",
    program_name: str = "",
    update_references: bool = True,
) -> str:
    """Rename a tag, optionally updating all references in rungs.

    Args:
        old_name: Current tag name.
        new_name: New tag name.
        scope: 'controller' or 'program'.
        program_name: Required when scope is 'program'.
        update_references: If true, updates all rung/ST references.
    """
    prj = _require_project()
    try:
        _tags.rename_tag(
            prj, old_name, new_name,
            scope=scope,
            program_name=program_name or None,
            update_references=update_references,
        )
        msg = f"Renamed tag '{old_name}' -> '{new_name}'"
        if update_references:
            msg += " (references updated)"
        return msg
    except Exception as e:
        return f"Error renaming tag: {e}"


@mcp.tool()
def set_tag_value(
    name: str,
    value: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Set the value of a scalar tag (DINT, REAL, BOOL, etc.).

    Updates both L5K and Decorated data formats to keep them in sync.

    Args:
        name: Tag name.
        value: Value as a string (e.g. '42', '3.14', '1' for true).
        scope: 'controller' or 'program'.
        program_name: Required when scope is 'program'.
    """
    prj = _require_project()
    try:
        # Auto-convert value to appropriate Python type
        info = _tags.get_tag_info(prj, name, scope=scope, program_name=program_name or None)
        dt = info.get('data_type', '')
        if dt in ('REAL', 'LREAL'):
            py_val = float(value)
        elif dt == 'BOOL':
            py_val = 1 if value.lower() in ('1', 'true', 'yes') else 0
        else:
            py_val = int(value)
        _tags.set_tag_value(prj, name, py_val, scope=scope, program_name=program_name or None)
        return f"Set '{name}' = {py_val}"
    except Exception as e:
        return f"Error setting tag value: {e}"


@mcp.tool()
def set_tag_member_value(
    name: str,
    member_path: str,
    value: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Set a member value in a structured or array tag.

    Use dot notation for structure members (e.g. 'PRE', 'Status.Active')
    and brackets for array indices (e.g. '[0]', '[2].EN').

    Args:
        name: Tag name.
        member_path: Path to the member (e.g. 'PRE', '[0]', '[2].EN').
        value: Value as a string.
        scope: 'controller' or 'program'.
        program_name: Required when scope is 'program'.
    """
    prj = _require_project()
    try:
        # Try int first, then float
        try:
            py_val = int(value)
        except ValueError:
            py_val = float(value)
        _tags.set_tag_member_value(
            prj, name, member_path, py_val,
            scope=scope, program_name=program_name or None,
        )
        return f"Set '{name}.{member_path}' = {py_val}"
    except Exception as e:
        return f"Error setting member value: {e}"


@mcp.tool()
def set_tag_description(
    name: str,
    description: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Set or update a tag's description text.

    Args:
        name: Tag name.
        description: Description text.
        scope: 'controller' or 'program'.
        program_name: Required when scope is 'program'.
    """
    prj = _require_project()
    try:
        _tags.set_tag_description(
            prj, name, description,
            scope=scope, program_name=program_name or None,
        )
        return f"Set description on '{name}'"
    except Exception as e:
        return f"Error setting description: {e}"


@mcp.tool()
def copy_tag(
    name: str,
    new_name: str,
    scope: str = "controller",
    program_name: str = "",
    to_scope: str = "",
    to_program_name: str = "",
) -> str:
    """Deep copy a tag to a new name, preserving all data and descriptions.

    Supports cross-scope copies (e.g. controller to program).

    Args:
        name: Source tag name.
        new_name: Name for the copy.
        scope: Source scope ('controller' or 'program').
        program_name: Required when source scope is 'program'.
        to_scope: Destination scope. Defaults to same as source.
        to_program_name: Required when destination scope is 'program'.
    """
    prj = _require_project()
    try:
        _tags.copy_tag(
            prj, name, new_name,
            source_scope=scope,
            source_program=program_name or None,
            dest_scope=to_scope or scope,
            dest_program=to_program_name or program_name or None,
        )
        return f"Copied '{name}' -> '{new_name}'"
    except Exception as e:
        return f"Error copying tag: {e}"


@mcp.tool()
def move_tag(
    name: str,
    from_scope: str = "controller",
    from_program: str = "",
    to_scope: str = "program",
    to_program: str = "",
) -> str:
    """Move a tag from one scope to another (e.g. controller to program).

    Args:
        name: Tag name to move.
        from_scope: Source scope ('controller' or 'program').
        from_program: Required when source scope is 'program'.
        to_scope: Destination scope ('controller' or 'program').
        to_program: Required when destination scope is 'program'.
    """
    prj = _require_project()
    try:
        _tags.move_tag(
            prj, name,
            from_scope=from_scope,
            from_program=from_program or None,
            to_scope=to_scope,
            to_program=to_program or None,
        )
        return f"Moved '{name}' from {from_scope} to {to_scope}"
    except Exception as e:
        return f"Error moving tag: {e}"


@mcp.tool()
def batch_create_tags(
    tag_specs_json: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Create multiple tags from a JSON array of specifications.

    Each spec object should have: name, data_type, and optionally
    description, dimensions, radix.

    Example: [{"name": "Tag1", "data_type": "DINT"}, {"name": "Tag2", "data_type": "REAL"}]

    Args:
        tag_specs_json: JSON array of tag specification objects.
        scope: Default scope for all tags.
        program_name: Default program name for all tags.
    """
    prj = _require_project()
    try:
        specs = json.loads(tag_specs_json)
        created = _tags.batch_create_tags(
            prj, specs,
            scope=scope,
            program_name=program_name or None,
        )
        return f"Created {len(created)} tags"
    except Exception as e:
        return f"Error in batch create: {e}"


@mcp.tool()
def create_alias_tag(
    name: str,
    alias_for: str,
    scope: str = "controller",
    program_name: str = "",
    description: str = "",
) -> str:
    """Create an alias tag pointing to another tag or I/O path.

    Alias tags inherit their data type from the target and have no data
    elements. Use aliases for modular programming — they allow programs
    to reference logical names that map to physical I/O or other tags.

    Args:
        name: Alias tag name (max 40 chars).
        alias_for: Target tag name, member path, or I/O point
            (e.g. 'MyTag', 'MyTag.Member', 'Local:1:I.Data.0').
        scope: 'controller' or 'program'.
        program_name: Required when scope is 'program'.
        description: Optional description text.
    """
    prj = _require_project()
    try:
        _tags.create_alias_tag(
            prj, name, alias_for,
            scope=scope,
            program_name=program_name or None,
            description=description or None,
        )
        return f"Created alias tag '{name}' -> '{alias_for}' (scope={scope})"
    except Exception as e:
        return f"Error creating alias tag: {e}"


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
def add_rung(
    program_name: str,
    routine_name: str,
    instruction_text: str,
    comment: str = "",
    position: int = -1,
) -> str:
    """Add a rung to an RLL routine.

    The instruction text must be valid RLL syntax ending with a semicolon.
    Examples: 'XIC(StartPB)OTE(MotorRun);', 'TON(Timer1,1000,0);'

    Args:
        program_name: Program containing the routine.
        routine_name: Name of the RLL routine.
        instruction_text: The rung instruction text.
        comment: Optional rung comment.
        position: Insert position (0-based). -1 to append at end.
    """
    prj = _require_project()
    try:
        pos = position if position >= 0 else None
        _programs.add_rung(
            prj, program_name, routine_name,
            instruction_text,
            comment=comment or None,
            position=pos,
        )
        return f"Added rung to '{program_name}/{routine_name}': {instruction_text[:60]}"
    except Exception as e:
        return f"Error adding rung: {e}"


@mcp.tool()
def delete_rung(
    program_name: str,
    routine_name: str,
    rung_number: int,
) -> str:
    """Delete a rung by its index number.

    Args:
        program_name: Program containing the routine.
        routine_name: Name of the RLL routine.
        rung_number: Zero-based rung index.
    """
    prj = _require_project()
    try:
        _programs.delete_rung(prj, program_name, routine_name, rung_number)
        return f"Deleted rung {rung_number} from '{program_name}/{routine_name}'"
    except Exception as e:
        return f"Error deleting rung: {e}"


@mcp.tool()
def modify_rung_text(
    program_name: str,
    routine_name: str,
    rung_number: int,
    new_text: str,
) -> str:
    """Replace the instruction text of an existing rung.

    Args:
        program_name: Program containing the routine.
        routine_name: Name of the RLL routine.
        rung_number: Zero-based rung index.
        new_text: New instruction text (must end with semicolon).
    """
    prj = _require_project()
    try:
        _programs.modify_rung_text(prj, program_name, routine_name, rung_number, new_text)
        return f"Modified rung {rung_number}: {new_text[:60]}"
    except Exception as e:
        return f"Error modifying rung: {e}"


@mcp.tool()
def set_rung_comment(
    program_name: str,
    routine_name: str,
    rung_number: int,
    comment: str,
) -> str:
    """Set or update the comment on a rung.

    Args:
        program_name: Program containing the routine.
        routine_name: Name of the RLL routine.
        rung_number: Zero-based rung index.
        comment: Comment text.
    """
    prj = _require_project()
    try:
        _programs.set_rung_comment(prj, program_name, routine_name, rung_number, comment)
        return f"Set comment on rung {rung_number}"
    except Exception as e:
        return f"Error setting comment: {e}"


@mcp.tool()
def duplicate_rung_with_substitution(
    program_name: str,
    routine_name: str,
    rung_number: int,
    substitutions_json: str,
    comment: str = "",
) -> str:
    """Duplicate a rung with tag name replacements.

    Creates a copy of the rung immediately after the original, with all
    tag names substituted according to the provided mapping. This is the
    primary tool for bulk rung generation (e.g. duplicating conveyor logic
    for multiple zones).

    Args:
        program_name: Program containing the routine.
        routine_name: Name of the RLL routine.
        rung_number: Zero-based index of the rung to duplicate.
        substitutions_json: JSON object mapping old tag names to new ones.
            Example: '{"OldTag": "NewTag", "Timer1": "Timer2"}'
        comment: Optional comment for the new rung.
    """
    prj = _require_project()
    try:
        subs = json.loads(substitutions_json)
        _programs.duplicate_rung_with_substitution(
            prj, program_name, routine_name, rung_number,
            subs,
            new_comment=comment or None,
        )
        return f"Duplicated rung {rung_number} with {len(subs)} substitutions"
    except Exception as e:
        return f"Error duplicating rung: {e}"


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
# 5. Import Operations
# ===================================================================

@mcp.tool()
def import_aoi(file_path: str, overwrite: bool = False) -> str:
    """Import an Add-On Instruction from an L5X export file.

    Automatically imports dependent UDTs and AOIs found in the source file.
    Updates the EditedDate so Studio 5000 accepts the import.

    Args:
        file_path: Path to the AOI .L5X export file.
        overwrite: If true, replace existing AOI with same name.
    """
    prj = _require_project()
    try:
        file_path = _normalize_path(file_path)
        elem = _aoi.import_aoi(prj, file_path, overwrite=overwrite)
        name = elem.get("Name", "?")
        return f"Imported AOI '{name}'"
    except Exception as e:
        return f"Error importing AOI: {e}"


@mcp.tool()
def import_udt(file_path: str, overwrite: bool = False) -> str:
    """Import a User-Defined Type from an L5X export file.

    Recursively imports transitive UDT dependencies.

    Args:
        file_path: Path to the UDT .L5X export file.
        overwrite: If true, replace existing UDT with same name.
    """
    prj = _require_project()
    try:
        file_path = _normalize_path(file_path)
        elem = _udt.import_udt(prj, file_path, overwrite=overwrite)
        name = elem.get("Name", "?")
        return f"Imported UDT '{name}'"
    except Exception as e:
        return f"Error importing UDT: {e}"


@mcp.tool()
def import_module(
    template_path: str,
    name: str,
    parent_module: str = "Local",
    address: str = "",
    slot: str = "",
    description: str = "",
) -> str:
    """Import an I/O module from a template L5X file.

    Copies the module definition, assigns the given name, and configures
    the parent module, address, and slot.

    Args:
        template_path: Path to the module template .L5X file.
        name: Name for the new module.
        parent_module: Parent module name (default: 'Local').
        address: IP address for Ethernet ports.
        slot: Slot number for backplane ports.
        description: Optional module description.
    """
    prj = _require_project()
    try:
        template_path = _normalize_path(template_path)
        _modules.import_module(
            prj, template_path, name,
            parent_module=parent_module,
            address=address or None,
            slot=slot or None,
            description=description or None,
        )
        return f"Imported module '{name}' under '{parent_module}'"
    except Exception as e:
        return f"Error importing module: {e}"


# ===================================================================
# 6. Analysis Tools
# ===================================================================

@mcp.tool()
def get_aoi_info(name: str) -> str:
    """Get detailed information about an Add-On Instruction.

    Returns name, revision, description, parameters, local tags, and routines.

    Args:
        name: AOI name.
    """
    prj = _require_project()
    try:
        info = _aoi.get_aoi_info(prj, name)
        return json.dumps(info, indent=2)
    except Exception as e:
        return f"Error getting AOI info: {e}"


@mcp.tool()
def get_aoi_parameters(name: str) -> str:
    """Get the parameter list for an Add-On Instruction.

    Returns each parameter's name, data type, usage (Input/Output/InOut),
    required flag, and description.

    Args:
        name: AOI name.
    """
    prj = _require_project()
    try:
        params = _aoi.get_aoi_parameters(prj, name)
        return json.dumps(params, indent=2)
    except Exception as e:
        return f"Error getting AOI parameters: {e}"


@mcp.tool()
def get_udt_info(name: str) -> str:
    """Get detailed information about a User-Defined Type.

    Returns name, family, description, and member list.

    Args:
        name: UDT name.
    """
    prj = _require_project()
    try:
        info = _udt.get_udt_info(prj, name)
        return json.dumps(info, indent=2)
    except Exception as e:
        return f"Error getting UDT info: {e}"


@mcp.tool()
def get_udt_members(name: str) -> str:
    """Get the member list for a User-Defined Type.

    Returns visible members only (excludes hidden backing fields).

    Args:
        name: UDT name.
    """
    prj = _require_project()
    try:
        members = _udt.get_udt_members(prj, name)
        return json.dumps(members, indent=2)
    except Exception as e:
        return f"Error getting UDT members: {e}"


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
# 7. Validation & Utilities
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
            "errors": result.errors[:50],  # Cap at 50 to avoid huge responses
            "warnings": result.warnings[:50],
        }
        return json.dumps(output, indent=2)
    except Exception as e:
        return f"Error running validation: {e}"


@mcp.tool()
def validate_rung_syntax(rung_text: str) -> str:
    """Check if a rung instruction text string is syntactically valid.

    Returns a list of error messages (empty list = valid).

    Args:
        rung_text: The instruction text to validate (e.g. 'XIC(tag1)OTE(tag2);').
    """
    errors = _rungs.validate_rung_syntax(rung_text)
    if not errors:
        return "Valid"
    return json.dumps(errors)


@mcp.tool()
def substitute_tags_in_rung(
    rung_text: str,
    substitutions_json: str,
) -> str:
    """Replace tag names in a rung instruction text string.

    Uses word-boundary-safe replacement to avoid partial matches.

    Args:
        rung_text: Original instruction text.
        substitutions_json: JSON object mapping old names to new names.
    """
    try:
        subs = json.loads(substitutions_json)
        result = _rungs.substitute_tags(rung_text, subs)
        return result
    except Exception as e:
        return f"Error substituting tags: {e}"


@mcp.tool()
def extract_tag_references_from_rung(rung_text: str) -> str:
    """Extract all tag names referenced in a rung instruction text.

    Returns base tag names (e.g. Timer1.DN -> Timer1, Array[0] -> Array).

    Args:
        rung_text: The instruction text to analyze.
    """
    refs = _rungs.extract_tag_references(rung_text)
    return json.dumps(sorted(refs))


# ===================================================================
# 8. Alarm Management
# ===================================================================

@mcp.tool()
def create_alarm_digital_tag(
    name: str,
    message: str,
    severity: int = 500,
    scope: str = "controller",
    program_name: str = "",
    description: str = "",
    ack_required: bool = True,
    latched: bool = False,
    tag_class: str = "",
) -> str:
    """Create an ALARM_DIGITAL tag for use with the ALMD instruction.

    ALARM_DIGITAL tags are standalone alarm tags that use <Data Format="Alarm">
    instead of L5K/Decorated data. They are driven by ALMD instructions in
    rung logic.

    Args:
        name: Tag name (max 40 chars).
        message: Alarm message text (e.g. 'Conveyor A0060 Faulted').
        severity: Alarm severity 1-1000 (500 = medium, 1000 = critical).
        scope: 'controller' or 'program'.
        program_name: Required when scope is 'program'.
        description: Optional tag description.
        ack_required: Whether the alarm requires acknowledgment.
        latched: Whether the alarm latches (stays active after condition clears).
        tag_class: 'Standard' or 'Safety'. Auto-detected when empty.
    """
    prj = _require_project()
    try:
        _tags.create_alarm_digital_tag(
            prj, name=name, message=message, severity=severity,
            scope=scope, program_name=program_name or None,
            description=description or None,
            ack_required=ack_required, latched=latched,
            tag_class=tag_class or None,
        )
        return (
            f"Created ALARM_DIGITAL tag '{name}' "
            f"(severity={severity}, message='{message}')"
        )
    except Exception as e:
        return f"Error creating ALARM_DIGITAL tag: {e}"


@mcp.tool()
def batch_create_alarm_digital_tags(
    tag_specs_json: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Create multiple ALARM_DIGITAL tags from a JSON array.

    Each spec object should have: name, message, and optionally severity
    (default 500), description, ack_required (default true), latched (default false).

    Example: [{"name": "AlarmMotor1", "message": "Motor 1 Fault"},
              {"name": "AlarmMotor2", "message": "Motor 2 Fault", "severity": 750}]

    Args:
        tag_specs_json: JSON array of alarm specification objects.
        scope: Default scope for all tags.
        program_name: Default program name for all tags.
    """
    prj = _require_project()
    try:
        specs = json.loads(tag_specs_json)
        created = _tags.batch_create_alarm_digital_tags(
            prj, specs, scope=scope, program_name=program_name or None,
        )
        return f"Created {len(created)} ALARM_DIGITAL tags"
    except Exception as e:
        return f"Error in batch create: {e}"


@mcp.tool()
def get_alarm_digital_info(
    name: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Get the configuration of an ALARM_DIGITAL or ALARM_ANALOG tag.

    Returns severity, message text, ack_required, latched, and all
    parameter values as JSON.

    Args:
        name: Tag name of the ALARM_DIGITAL/ALARM_ANALOG tag.
        scope: 'controller' or 'program'.
        program_name: Required when scope is 'program'.
    """
    prj = _require_project()
    try:
        info = _tags.get_alarm_digital_info(
            prj, name, scope=scope, program_name=program_name or None,
        )
        return json.dumps(info, indent=2)
    except Exception as e:
        return f"Error getting alarm info: {e}"


@mcp.tool()
def configure_alarm_digital_tag(
    name: str,
    scope: str = "controller",
    program_name: str = "",
    severity: int = -1,
    message: str = "",
    ack_required: str = "",
    latched: str = "",
) -> str:
    """Update configuration on an existing ALARM_DIGITAL tag.

    Only specified parameters are modified; others are left unchanged.
    Pass severity=-1 to leave unchanged. Pass empty string for message/
    ack_required/latched to leave unchanged.

    Args:
        name: Tag name of the ALARM_DIGITAL tag.
        scope: 'controller' or 'program'.
        program_name: Required when scope is 'program'.
        severity: New severity (1-1000). -1 to leave unchanged.
        message: New alarm message text. Empty to leave unchanged.
        ack_required: 'true' or 'false'. Empty to leave unchanged.
        latched: 'true' or 'false'. Empty to leave unchanged.
    """
    prj = _require_project()
    try:
        kwargs: dict = {}
        if severity >= 0:
            kwargs['severity'] = severity
        if message:
            kwargs['message'] = message
        if ack_required:
            kwargs['ack_required'] = ack_required.lower() == 'true'
        if latched:
            kwargs['latched'] = latched.lower() == 'true'

        _tags.configure_alarm_digital_tag(
            prj, name, scope=scope, program_name=program_name or None,
            **kwargs,
        )
        changes = ', '.join(f'{k}={v}' for k, v in kwargs.items())
        return f"Updated ALARM_DIGITAL tag '{name}': {changes}"
    except Exception as e:
        return f"Error configuring alarm: {e}"


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


@mcp.tool()
def list_alarm_definitions() -> str:
    """List all DatatypeAlarmDefinitions in the project.

    Returns each data type that has alarm definitions, along with the
    count and names of its MemberAlarmDefinitions.
    """
    prj = _require_project()
    try:
        results = prj.list_alarm_definitions()
        if not results:
            return "No alarm definitions found in the project."
        return json.dumps(results, indent=2)
    except Exception as e:
        return f"Error listing alarm definitions: {e}"


@mcp.tool()
def get_tag_alarm_conditions(
    name: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Get all alarm conditions on a tag.

    Returns the AlarmCondition elements attached to the tag, including
    name, condition type, input, severity, delay settings, and used status.

    Args:
        name: Tag name.
        scope: 'controller' or 'program'.
        program_name: Required when scope is 'program'.
    """
    prj = _require_project()
    try:
        conditions = _tags.get_tag_alarm_conditions(
            prj, name, scope=scope, program_name=program_name or None,
        )
        if not conditions:
            return f"Tag '{name}' has no alarm conditions."
        return json.dumps(conditions, indent=2)
    except Exception as e:
        return f"Error getting alarm conditions: {e}"


@mcp.tool()
def configure_tag_alarm_condition(
    tag_name: str,
    condition_name: str,
    scope: str = "controller",
    program_name: str = "",
    severity: int = -1,
    on_delay: int = -1,
    off_delay: int = -1,
    used: str = "",
    ack_required: str = "",
    message: str = "",
) -> str:
    """Update settings on a specific alarm condition within a tag.

    Modifies an existing AlarmCondition element on a tag. Only specified
    parameters are modified; pass -1 or empty string to leave unchanged.

    Args:
        tag_name: Tag name containing the alarm conditions.
        condition_name: Name of the specific AlarmCondition to modify.
        scope: 'controller' or 'program'.
        program_name: Required when scope is 'program'.
        severity: New severity (1-1000). -1 to leave unchanged.
        on_delay: On delay in ms. -1 to leave unchanged.
        off_delay: Off delay in ms. -1 to leave unchanged.
        used: 'true' or 'false'. Empty to leave unchanged.
        ack_required: 'true' or 'false'. Empty to leave unchanged.
        message: Alarm message text. Empty to leave unchanged.
    """
    prj = _require_project()
    try:
        kwargs: dict = {}
        if severity >= 0:
            kwargs['severity'] = severity
        if on_delay >= 0:
            kwargs['on_delay'] = on_delay
        if off_delay >= 0:
            kwargs['off_delay'] = off_delay
        if used:
            kwargs['used'] = used.lower() == 'true'
        if ack_required:
            kwargs['ack_required'] = ack_required.lower() == 'true'
        if message:
            kwargs['message'] = message

        _tags.configure_tag_alarm_condition(
            prj, tag_name, condition_name,
            scope=scope, program_name=program_name or None,
            **kwargs,
        )
        changes = ', '.join(f'{k}={v}' for k, v in kwargs.items())
        return (
            f"Updated alarm condition '{condition_name}' on tag "
            f"'{tag_name}': {changes}"
        )
    except Exception as e:
        return f"Error configuring alarm condition: {e}"


@mcp.tool()
def create_alarm_definition(
    data_type_name: str,
    members_json: str,
) -> str:
    """Create a DatatypeAlarmDefinition for a data type (UDT or AOI).

    Defines alarm conditions that will be automatically generated on every
    tag of this data type. Each member object should have: name, input
    (must start with '.'), condition_type, and optionally: severity (default
    500), on_delay (default 0), off_delay (default 0), message, ack_required
    (default false), expression (default '= 1').

    Args:
        data_type_name: Name of the data type to add alarm definitions to.
        members_json: JSON array of member alarm definition objects.
    """
    prj = _require_project()
    try:
        members = json.loads(members_json)
        prj.create_alarm_definition(data_type_name, members)
        return (
            f"Created alarm definition for '{data_type_name}' "
            f"with {len(members)} member alarm(s)"
        )
    except Exception as e:
        return f"Error creating alarm definition: {e}"


@mcp.tool()
def remove_alarm_definition(data_type_name: str) -> str:
    """Remove a DatatypeAlarmDefinition from the project.

    Removes the alarm definition for the specified data type. This does NOT
    remove AlarmConditions from existing tags of that type.

    Args:
        data_type_name: Name of the data type whose alarm definition to remove.
    """
    prj = _require_project()
    try:
        removed = prj.remove_alarm_definition(data_type_name)
        count = len(removed.findall('MemberAlarmDefinition'))
        return (
            f"Removed alarm definition for '{data_type_name}' "
            f"({count} member alarm(s) removed)"
        )
    except Exception as e:
        return f"Error removing alarm definition: {e}"


# ===================================================================
# 9. Component Export / Import
# ===================================================================

# --- Create from scratch ---

@mcp.tool()
def create_rung_export(
    program_name: str = "ExportedProgram",
    routine_name: str = "MainRoutine",
) -> str:
    """Create an empty Rung export file in memory and load it as the active project.

    The resulting in-memory project has TargetType='Rung' and can be
    populated with add_rung, create_tag, etc., then saved with save_project.

    Args:
        program_name: Name for the context program.
        routine_name: Name for the context routine.
    """
    global _project, _project_path
    try:
        source = _project if _project is not None else None
        prj = _comp_export.create_rung_export(
            project=source,
            program_name=program_name,
            routine_name=routine_name,
        )
        _project = prj
        _project_path = None
        return (
            f"Created empty Rung export in memory "
            f"(program='{program_name}', routine='{routine_name}'). "
            f"Use add_rung/create_tag to populate, then save_project to write."
        )
    except Exception as e:
        return f"Error creating rung export: {e}"


@mcp.tool()
def create_routine_export(
    program_name: str = "ExportedProgram",
    routine_name: str = "MainRoutine",
    routine_type: str = "RLL",
) -> str:
    """Create an empty Routine export file in memory and load it as the active project.

    Args:
        program_name: Name for the context program.
        routine_name: Name for the target routine.
        routine_type: Routine type ('RLL', 'ST', 'FBD', or 'SFC').
    """
    global _project, _project_path
    try:
        source = _project if _project is not None else None
        prj = _comp_export.create_routine_export(
            project=source,
            program_name=program_name,
            routine_name=routine_name,
            routine_type=routine_type,
        )
        _project = prj
        _project_path = None
        return (
            f"Created empty Routine export in memory "
            f"(routine='{routine_name}', type={routine_type}). "
            f"Use add_rung/create_tag to populate, then save_project to write."
        )
    except Exception as e:
        return f"Error creating routine export: {e}"


@mcp.tool()
def create_program_export(
    program_name: str = "ExportedProgram",
) -> str:
    """Create an empty Program export file in memory and load it as the active project.

    The program includes an empty MainRoutine (RLL type).

    Args:
        program_name: Name for the target program.
    """
    global _project, _project_path
    try:
        source = _project if _project is not None else None
        prj = _comp_export.create_program_export(
            project=source,
            program_name=program_name,
        )
        _project = prj
        _project_path = None
        return (
            f"Created empty Program export in memory "
            f"(program='{program_name}'). "
            f"Use add_rung/create_tag/create_routine to populate, "
            f"then save_project to write."
        )
    except Exception as e:
        return f"Error creating program export: {e}"


# --- Extract from project ---

@mcp.tool()
def export_rung(
    program_name: str,
    routine_name: str,
    rung_numbers: str,
    file_path: str = "",
    include_tags: bool = True,
) -> str:
    """Extract specific rungs from a routine into a standalone Rung export file.

    Includes the specified rungs along with referenced tags, UDT
    definitions, and AOI definitions as context dependencies.

    Args:
        program_name: Program containing the routine.
        routine_name: Name of the RLL routine.
        rung_numbers: Comma-separated rung indices (e.g. '0,1,2' or '5').
        file_path: Output file path. If empty, auto-generates a name.
        include_tags: Whether to include referenced tags and dependencies.
    """
    prj = _require_project()
    try:
        nums = [int(n.strip()) for n in rung_numbers.split(',') if n.strip()]
        fp = _normalize_path(file_path) if file_path else ""
        result = _comp_export.export_rung(
            prj, program_name, routine_name, nums,
            file_path=fp,
            include_tags=include_tags,
        )
        return f"Exported {len(nums)} rung(s) to: {result}"
    except Exception as e:
        return f"Error exporting rungs: {e}"


@mcp.tool()
def export_routine(
    program_name: str,
    routine_name: str,
    file_path: str = "",
    include_tags: bool = True,
) -> str:
    """Extract an entire routine into a standalone Routine export file.

    Includes all rungs/lines and referenced dependencies.

    Args:
        program_name: Program containing the routine.
        routine_name: Name of the routine.
        file_path: Output file path. If empty, auto-generates a name.
        include_tags: Whether to include referenced tags.
    """
    prj = _require_project()
    try:
        fp = _normalize_path(file_path) if file_path else ""
        result = _comp_export.export_routine(
            prj, program_name, routine_name,
            file_path=fp,
            include_tags=include_tags,
        )
        return f"Exported routine '{routine_name}' to: {result}"
    except Exception as e:
        return f"Error exporting routine: {e}"


@mcp.tool()
def export_program(
    program_name: str,
    file_path: str = "",
) -> str:
    """Extract an entire program into a standalone Program export file.

    Includes all program tags, routines, and referenced controller-scope
    tags, UDTs, and AOIs.

    Args:
        program_name: Name of the program to export.
        file_path: Output file path. If empty, auto-generates a name.
    """
    prj = _require_project()
    try:
        fp = _normalize_path(file_path) if file_path else ""
        result = _comp_export.export_program(
            prj, program_name,
            file_path=fp,
        )
        return f"Exported program '{program_name}' to: {result}"
    except Exception as e:
        return f"Error exporting program: {e}"


@mcp.tool()
def export_tag(
    tag_name: str,
    scope: str = "controller",
    program_name: str = "",
    file_path: str = "",
) -> str:
    """Extract a tag into a standalone export file.

    Tags are exported in a Rung-type shell (L5X has no standalone Tag
    export type). The tag's data type dependencies (UDTs, AOIs) are included.

    Args:
        tag_name: Name of the tag to export.
        scope: 'controller' or 'program'.
        program_name: Required when scope is 'program'.
        file_path: Output file path. If empty, auto-generates a name.
    """
    prj = _require_project()
    try:
        fp = _normalize_path(file_path) if file_path else ""
        result = _comp_export.export_tag(
            prj, tag_name,
            scope=scope,
            program_name=program_name,
            file_path=fp,
        )
        return f"Exported tag '{tag_name}' to: {result}"
    except Exception as e:
        return f"Error exporting tag: {e}"


@mcp.tool()
def export_udt(
    udt_name: str,
    file_path: str = "",
) -> str:
    """Extract a UDT definition into a standalone DataType export file.

    Includes transitive UDT dependencies as context.

    Args:
        udt_name: Name of the UDT to export.
        file_path: Output file path. If empty, auto-generates a name.
    """
    prj = _require_project()
    try:
        fp = _normalize_path(file_path) if file_path else ""
        result = _comp_export.export_udt(
            prj, udt_name,
            file_path=fp,
        )
        return f"Exported UDT '{udt_name}' to: {result}"
    except Exception as e:
        return f"Error exporting UDT: {e}"


@mcp.tool()
def export_aoi(
    aoi_name: str,
    file_path: str = "",
) -> str:
    """Extract an AOI definition into a standalone AOI export file.

    Includes dependent UDTs and nested AOIs. Updates the EditedDate.

    Args:
        aoi_name: Name of the AOI to export.
        file_path: Output file path. If empty, auto-generates a name.
    """
    prj = _require_project()
    try:
        fp = _normalize_path(file_path) if file_path else ""
        result = _comp_export.export_aoi(
            prj, aoi_name,
            file_path=fp,
        )
        return f"Exported AOI '{aoi_name}' to: {result}"
    except Exception as e:
        return f"Error exporting AOI: {e}"


# --- Import with validation ---

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


@mcp.tool()
def import_component(
    file_path: str,
    conflict_resolution: str = "report",
    target_program: str = "",
    target_routine: str = "",
    rung_position: int = -1,
) -> str:
    """Import a component export file into the loaded project.

    Automatically detects the component type (Rung, Routine, Program,
    DataType, AddOnInstructionDefinition) and imports with conflict
    detection and resolution.

    Args:
        file_path: Path to the component export .L5X file.
        conflict_resolution: How to handle conflicts:
            'report' = dry run (return conflicts only, no changes),
            'skip' = import non-conflicting items and skip conflicts,
            'overwrite' = replace existing items with imported versions,
            'fail' = abort on any conflict.
        target_program: Override target program name (for Rung/Routine imports).
        target_routine: Override target routine name (for Rung imports).
        rung_position: Insert position for rungs (0-based). -1 to append.
    """
    prj = _require_project()
    try:
        fp = _normalize_path(file_path)
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server on stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
