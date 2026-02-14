"""
Program and Routine CRUD operations for L5X files.

Provides functions to create, delete, and modify Programs, Routines, and
Rungs within a Rockwell Automation L5X project.  All operations produce
structurally correct XML that conforms to the L5X schema expected by
Studio 5000 / Logix Designer.

Programs in L5X are containers for routines and program-scoped tags.
Each program is assigned to a Task via the ScheduledPrograms element.
Routines contain the actual control logic -- either Relay Ladder Logic
(RLL), Structured Text (ST), Function Block Diagram (FBD), or Sequential
Function Chart (SFC).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Union

from lxml import etree

from .rungs import substitute_tags
from .schema import VALID_ROUTINE_TYPES, VALID_RUNG_TYPES
from .utils import (
    deep_copy,
    find_or_create,
    get_description,
    make_description_element,
    set_cdata_text,
    set_description,
    validate_tag_name,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_controller(project) -> etree._Element:
    """Return the Controller element from the project.

    Accepts either an L5XProject instance or a raw lxml root element.

    Returns:
        The ``Controller`` element.

    Raises:
        ValueError: If the Controller element is not found.
    """
    root = getattr(project, 'root', project)
    controller = root.find("Controller")
    if controller is None:
        raise ValueError("Controller element not found in L5X project")
    return controller


def _find_programs_container(project: etree._Element) -> etree._Element:
    """Return the Programs container element, creating it if absent.

    Args:
        project: The root ``RSLogix5000Content`` element.

    Returns:
        The ``Programs`` element under the Controller.
    """
    controller = _find_controller(project)
    return find_or_create(controller, "Programs")


def _find_program(project: etree._Element, name: str) -> etree._Element:
    """Find a Program element by name.

    Args:
        project: The root ``RSLogix5000Content`` element.
        name: The program name (case-insensitive match against the Name attribute).

    Returns:
        The matching ``Program`` element.

    Raises:
        KeyError: If no program with the given name exists.
    """
    programs = _find_programs_container(project)
    for prog in programs.findall("Program"):
        if prog.get("Name", "").lower() == name.lower():
            return prog
    raise KeyError(f"Program '{name}' not found")


def _find_routines_container(
    project: etree._Element, program_name: str
) -> etree._Element:
    """Return the Routines container for a given program.

    Args:
        project: The root ``RSLogix5000Content`` element.
        program_name: The name of the program.

    Returns:
        The ``Routines`` element within the specified program.
    """
    program = _find_program(project, program_name)
    return find_or_create(program, "Routines")


def _find_routine(
    project: etree._Element, program_name: str, routine_name: str
) -> etree._Element:
    """Find a Routine element by name within a program.

    Args:
        project: The root ``RSLogix5000Content`` element.
        program_name: The name of the containing program.
        routine_name: The routine name (case-insensitive match).

    Returns:
        The matching ``Routine`` element.

    Raises:
        KeyError: If the routine is not found in the specified program.
    """
    routines = _find_routines_container(project, program_name)
    for routine in routines.findall("Routine"):
        if routine.get("Name", "").lower() == routine_name.lower():
            return routine
    raise KeyError(
        f"Routine '{routine_name}' not found in program '{program_name}'"
    )


def _infer_routine_type(routine: etree._Element) -> str:
    """Infer the routine type from the Type attribute or child content elements.

    Rung export files may omit the ``Type`` attribute on ``<Routine>`` elements.
    In that case the type is inferred from the presence of content child
    elements (``RLLContent``, ``STContent``, ``FBDContent``, ``SFCContent``).

    Args:
        routine: The ``Routine`` XML element.

    Returns:
        The routine type string (``'RLL'``, ``'ST'``, ``'FBD'``, ``'SFC'``),
        or ``''`` if it cannot be determined.
    """
    explicit = routine.get("Type")
    if explicit:
        return explicit
    # Infer from child content elements.
    for content_tag, rtype in (
        ("RLLContent", "RLL"),
        ("STContent", "ST"),
        ("FBDContent", "FBD"),
        ("SFCContent", "SFC"),
    ):
        if routine.find(content_tag) is not None:
            return rtype
    return ""


def _find_rll_content(routine: etree._Element) -> etree._Element:
    """Return the RLLContent element for a routine, creating it if absent.

    Args:
        routine: The ``Routine`` element (must be Type='RLL').

    Returns:
        The ``RLLContent`` element.

    Raises:
        ValueError: If the routine is not an RLL routine.
    """
    routine_type = _infer_routine_type(routine)
    if routine_type != "RLL":
        raise ValueError(
            f"Routine '{routine.get('Name')}' is type '{routine_type}', "
            f"not 'RLL'"
        )
    return find_or_create(routine, "RLLContent")


def _find_st_content(routine: etree._Element) -> etree._Element:
    """Return the STContent element for a routine, creating it if absent.

    Args:
        routine: The ``Routine`` element (must be Type='ST').

    Returns:
        The ``STContent`` element.

    Raises:
        ValueError: If the routine is not an ST routine.
    """
    routine_type = _infer_routine_type(routine)
    if routine_type != "ST":
        raise ValueError(
            f"Routine '{routine.get('Name')}' is type '{routine_type}', "
            f"not 'ST'"
        )
    return find_or_create(routine, "STContent")


def _renumber_rungs(rll_content: etree._Element) -> None:
    """Renumber all Rung elements sequentially starting from 0.

    Args:
        rll_content: The ``RLLContent`` element whose child Rung elements
            should be renumbered.
    """
    for idx, rung in enumerate(rll_content.findall("Rung")):
        rung.set("Number", str(idx))


def _get_rung_by_number(
    rll_content: etree._Element, rung_number: int
) -> etree._Element:
    """Return the Rung element at the given number (index).

    Args:
        rll_content: The ``RLLContent`` element.
        rung_number: The zero-based rung index.

    Returns:
        The matching ``Rung`` element.

    Raises:
        IndexError: If no rung with the given number exists.
    """
    rungs = rll_content.findall("Rung")
    if rung_number < 0 or rung_number >= len(rungs):
        raise IndexError(
            f"Rung number {rung_number} is out of range "
            f"(routine has {len(rungs)} rungs)"
        )
    return rungs[rung_number]


def _ensure_semicolon(text: str) -> str:
    """Ensure that instruction text ends with a semicolon.

    Args:
        text: The rung instruction text.

    Returns:
        The text with a trailing semicolon guaranteed.
    """
    text = text.rstrip()
    if not text.endswith(";"):
        text += ";"
    return text


def _find_tasks_container(project: etree._Element) -> etree._Element:
    """Return the Tasks container element, creating it if absent.

    Args:
        project: The root ``RSLogix5000Content`` element.

    Returns:
        The ``Tasks`` element under the Controller.
    """
    controller = _find_controller(project)
    return find_or_create(controller, "Tasks")


def _find_task(project: etree._Element, task_name: str) -> etree._Element:
    """Find a Task element by name.

    Args:
        project: The root ``RSLogix5000Content`` element.
        task_name: The task name (case-insensitive match).

    Returns:
        The matching ``Task`` element.

    Raises:
        KeyError: If no task with the given name exists.
    """
    tasks = _find_tasks_container(project)
    for task in tasks.findall("Task"):
        if task.get("Name", "").lower() == task_name.lower():
            return task
    raise KeyError(f"Task '{task_name}' not found")


# ---------------------------------------------------------------------------
# Program CRUD
# ---------------------------------------------------------------------------

def create_program(
    project: etree._Element,
    name: str,
    description: str = None,
    main_routine_name: str = "MainRoutine",
    create_main_routine: bool = True,
) -> etree._Element:
    """Create a new program and add it to the project.

    The new program is created with the standard attributes expected by
    Studio 5000.  An empty ``<Tags>`` container is always created.  If
    *create_main_routine* is ``True`` (the default), an empty RLL routine
    with the name specified by *main_routine_name* is also created.

    Args:
        project: The root ``RSLogix5000Content`` element.
        name: The program name.  Must comply with L5X naming rules.
        description: Optional description text (stored as CDATA).
        main_routine_name: Name of the main routine to set.
            Defaults to ``'MainRoutine'``.
        create_main_routine: Whether to create the main routine automatically.
            Defaults to ``True``.

    Returns:
        The newly created ``Program`` element.

    Raises:
        ValueError: If *name* violates L5X naming rules or a program with
            this name already exists.
    """
    validate_tag_name(name)

    programs = _find_programs_container(project)

    # Check for duplicate name.
    for existing in programs.findall("Program"):
        if existing.get("Name", "").lower() == name.lower():
            raise ValueError(f"Program '{name}' already exists")

    # Build the Program element.
    program = etree.SubElement(
        programs,
        "Program",
        attrib={
            "Name": name,
            "TestEdits": "false",
            "MainRoutineName": main_routine_name,
            "FaultRoutineName": "",
            "Disabled": "false",
            "UseAsFolder": "false",
        },
    )

    # Description (must come first per L5X convention).
    if description is not None:
        set_description(program, description)

    # Empty Tags container.
    etree.SubElement(program, "Tags")

    # Routines container.
    routines_elem = etree.SubElement(program, "Routines")

    # Optionally create the main routine.
    if create_main_routine:
        routine = etree.SubElement(
            routines_elem,
            "Routine",
            attrib={"Name": main_routine_name, "Type": "RLL"},
        )
        etree.SubElement(routine, "RLLContent")

    return program


def delete_program(
    project: etree._Element, name: str
) -> etree._Element:
    """Delete a program from the project.

    The program is removed from the ``Programs`` container.  It is also
    unscheduled from any task that references it.

    Args:
        project: The root ``RSLogix5000Content`` element.
        name: The name of the program to delete.

    Returns:
        The removed ``Program`` element (detached from the tree).

    Raises:
        KeyError: If no program with the given name exists.
    """
    program = _find_program(project, name)
    programs = _find_programs_container(project)

    # Unschedule from all tasks.
    tasks = _find_tasks_container(project)
    for task in tasks.findall("Task"):
        scheduled = task.find("ScheduledPrograms")
        if scheduled is None:
            continue
        for sp in scheduled.findall("ScheduledProgram"):
            if sp.get("Name", "").lower() == name.lower():
                scheduled.remove(sp)

    programs.remove(program)
    return program


# ---------------------------------------------------------------------------
# Routine CRUD
# ---------------------------------------------------------------------------

def create_routine(
    project: etree._Element,
    program_name: str,
    routine_name: str,
    routine_type: str = "RLL",
    description: str = None,
) -> etree._Element:
    """Create a new routine within a program.

    Creates the appropriate content container based on the routine type:
    - ``'RLL'``: creates an ``<RLLContent/>`` child element.
    - ``'ST'``: creates an ``<STContent/>`` child element.
    - ``'FBD'``: creates an ``<FBDContent/>`` child element.
    - ``'SFC'``: creates an ``<SFCContent/>`` child element.

    Args:
        project: The root ``RSLogix5000Content`` element.
        program_name: The name of the program to add the routine to.
        routine_name: The name for the new routine.  Must comply with
            L5X naming rules.
        routine_type: The routine type.  One of ``'RLL'``, ``'ST'``,
            ``'FBD'``, ``'SFC'``.  Defaults to ``'RLL'``.
        description: Optional description text.

    Returns:
        The newly created ``Routine`` element.

    Raises:
        ValueError: If *routine_name* violates L5X naming rules, the
            routine type is invalid, or a routine with the same name
            already exists in the program.
        KeyError: If the program does not exist.
    """
    validate_tag_name(routine_name)

    routine_type_upper = routine_type.upper()
    if routine_type_upper not in VALID_ROUTINE_TYPES:
        raise ValueError(
            f"Invalid routine type '{routine_type}'. "
            f"Must be one of: {sorted(VALID_ROUTINE_TYPES)}"
        )

    routines = _find_routines_container(project, program_name)

    # Check for duplicate name.
    for existing in routines.findall("Routine"):
        if existing.get("Name", "").lower() == routine_name.lower():
            raise ValueError(
                f"Routine '{routine_name}' already exists in "
                f"program '{program_name}'"
            )

    # Build the Routine element.
    routine = etree.SubElement(
        routines,
        "Routine",
        attrib={"Name": routine_name, "Type": routine_type_upper},
    )

    if description is not None:
        set_description(routine, description)

    # Create the appropriate content container.
    content_tag = f"{routine_type_upper}Content"
    etree.SubElement(routine, content_tag)

    return routine


def delete_routine(
    project: etree._Element, program_name: str, routine_name: str
) -> etree._Element:
    """Delete a routine from a program.

    If the deleted routine is referenced as the program's
    ``MainRoutineName``, the attribute is cleared to prevent Studio 5000
    from referencing a nonexistent routine.

    Args:
        project: The root ``RSLogix5000Content`` element.
        program_name: The name of the containing program.
        routine_name: The name of the routine to delete.

    Returns:
        The removed ``Routine`` element (detached from the tree).

    Raises:
        KeyError: If the program or routine does not exist.
    """
    routine = _find_routine(project, program_name, routine_name)
    routines = _find_routines_container(project, program_name)
    routines.remove(routine)

    # Clear MainRoutineName if it pointed to the deleted routine.
    program = _find_program(project, program_name)
    main_name = program.get("MainRoutineName", "")
    if main_name.lower() == routine_name.lower():
        program.set("MainRoutineName", "")

    # Clear FaultRoutineName if it pointed to the deleted routine.
    fault_name = program.get("FaultRoutineName", "")
    if fault_name.lower() == routine_name.lower():
        program.set("FaultRoutineName", "")

    return routine


# ---------------------------------------------------------------------------
# RLL Rung operations
# ---------------------------------------------------------------------------

def add_rung(
    project: etree._Element,
    program_name: str,
    routine_name: str,
    instruction_text: str,
    comment: str = None,
    position: int = None,
) -> etree._Element:
    """Add a rung to an RLL routine.

    The rung is inserted at the specified *position* (zero-based index).
    If *position* is ``None``, the rung is appended at the end.  All
    rungs are renumbered sequentially after insertion.

    The instruction text is wrapped in a CDATA section and must end with
    a semicolon (one is appended automatically if missing).

    Args:
        project: The root ``RSLogix5000Content`` element.
        program_name: The name of the program containing the routine.
        routine_name: The name of the RLL routine.
        instruction_text: The ladder logic instruction text
            (e.g. ``'XIC(tag)OTE(out);'``).
        comment: Optional rung comment text.
        position: Zero-based insertion index.  ``None`` to append.

    Returns:
        The newly created ``Rung`` element.

    Raises:
        KeyError: If the program or routine does not exist.
        ValueError: If the routine is not an RLL routine.
        IndexError: If *position* is out of range.
    """
    routine = _find_routine(project, program_name, routine_name)
    rll_content = _find_rll_content(routine)

    instruction_text = _ensure_semicolon(instruction_text)

    rungs = rll_content.findall("Rung")
    rung_count = len(rungs)

    # Validate position.
    if position is not None:
        if position < 0 or position > rung_count:
            raise IndexError(
                f"Position {position} is out of range "
                f"(routine has {rung_count} rungs; valid range is 0..{rung_count})"
            )

    # Build the Rung element.
    rung = etree.Element(
        "Rung",
        attrib={"Number": "0", "Type": "N"},
    )

    # Comment (optional).
    if comment is not None:
        comment_elem = etree.SubElement(rung, "Comment")
        set_cdata_text(comment_elem, comment)

    # Instruction text.
    text_elem = etree.SubElement(rung, "Text")
    set_cdata_text(text_elem, instruction_text)

    # Insert at the requested position.
    if position is not None:
        rll_content.insert(position, rung)
    else:
        rll_content.append(rung)

    # Renumber all rungs.
    _renumber_rungs(rll_content)

    return rung


def delete_rung(
    project: etree._Element,
    program_name: str,
    routine_name: str,
    rung_number: int,
) -> etree._Element:
    """Delete a rung by its number (zero-based index).

    Remaining rungs are renumbered sequentially after deletion.

    Args:
        project: The root ``RSLogix5000Content`` element.
        program_name: The name of the program containing the routine.
        routine_name: The name of the RLL routine.
        rung_number: The zero-based rung index to delete.

    Returns:
        The removed ``Rung`` element (detached from the tree).

    Raises:
        KeyError: If the program or routine does not exist.
        ValueError: If the routine is not an RLL routine.
        IndexError: If *rung_number* is out of range.
    """
    routine = _find_routine(project, program_name, routine_name)
    rll_content = _find_rll_content(routine)

    rung = _get_rung_by_number(rll_content, rung_number)
    rll_content.remove(rung)

    _renumber_rungs(rll_content)
    return rung


def modify_rung_text(
    project: etree._Element,
    program_name: str,
    routine_name: str,
    rung_number: int,
    new_text: str,
) -> None:
    """Replace the instruction text of a rung.

    Args:
        project: The root ``RSLogix5000Content`` element.
        program_name: The name of the program containing the routine.
        routine_name: The name of the RLL routine.
        rung_number: The zero-based rung index to modify.
        new_text: The new instruction text.  A trailing semicolon is
            appended if missing.

    Raises:
        KeyError: If the program or routine does not exist.
        ValueError: If the routine is not an RLL routine or the rung
            has no ``Text`` child.
        IndexError: If *rung_number* is out of range.
    """
    routine = _find_routine(project, program_name, routine_name)
    rll_content = _find_rll_content(routine)

    new_text = _ensure_semicolon(new_text)

    rung = _get_rung_by_number(rll_content, rung_number)
    text_elem = rung.find("Text")
    if text_elem is None:
        text_elem = etree.SubElement(rung, "Text")
    set_cdata_text(text_elem, new_text)


def set_rung_comment(
    project: etree._Element,
    program_name: str,
    routine_name: str,
    rung_number: int,
    comment: str,
) -> None:
    """Set or update the comment on a rung.

    If the rung already has a ``Comment`` child, its text is replaced.
    Otherwise a new ``Comment`` element is created and inserted before
    the ``Text`` element.

    Args:
        project: The root ``RSLogix5000Content`` element.
        program_name: The name of the program containing the routine.
        routine_name: The name of the RLL routine.
        rung_number: The zero-based rung index.
        comment: The comment text.

    Raises:
        KeyError: If the program or routine does not exist.
        ValueError: If the routine is not an RLL routine.
        IndexError: If *rung_number* is out of range.
    """
    routine = _find_routine(project, program_name, routine_name)
    rll_content = _find_rll_content(routine)

    rung = _get_rung_by_number(rll_content, rung_number)
    comment_elem = rung.find("Comment")

    if comment_elem is None:
        # Insert Comment as the first child (before Text).
        comment_elem = etree.Element("Comment")
        rung.insert(0, comment_elem)

    set_cdata_text(comment_elem, comment)


def copy_rung(
    project: etree._Element,
    source_program: str,
    source_routine: str,
    source_rung: int,
    dest_program: str,
    dest_routine: str,
    dest_position: int = None,
    tag_substitutions: dict = None,
) -> etree._Element:
    """Copy a rung from one location to another.

    The source rung is deep-copied so the original is not affected.
    Optionally applies tag name substitutions to the copied rung's
    instruction text.

    Args:
        project: The root ``RSLogix5000Content`` element.
        source_program: Name of the program containing the source routine.
        source_routine: Name of the source RLL routine.
        source_rung: Zero-based index of the rung to copy.
        dest_program: Name of the program containing the destination routine.
        dest_routine: Name of the destination RLL routine.
        dest_position: Zero-based insertion index in the destination routine.
            ``None`` to append.
        tag_substitutions: Optional dictionary mapping old tag base names
            to new tag base names.  Applied to the instruction text of the
            copied rung.

    Returns:
        The newly inserted ``Rung`` element in the destination routine.

    Raises:
        KeyError: If any program or routine does not exist.
        ValueError: If either routine is not an RLL routine.
        IndexError: If *source_rung* or *dest_position* is out of range.
    """
    # Locate the source rung and deep-copy it.
    src_routine = _find_routine(project, source_program, source_routine)
    src_rll = _find_rll_content(src_routine)
    src_rung_elem = _get_rung_by_number(src_rll, source_rung)

    new_rung = deep_copy(src_rung_elem)

    # Apply tag substitutions if provided.
    if tag_substitutions:
        text_elem = new_rung.find("Text")
        if text_elem is not None and text_elem.text:
            new_text = substitute_tags(text_elem.text, tag_substitutions)
            set_cdata_text(text_elem, new_text)

    # Insert into the destination routine.
    dst_routine = _find_routine(project, dest_program, dest_routine)
    dst_rll = _find_rll_content(dst_routine)

    dst_rungs = dst_rll.findall("Rung")
    dst_count = len(dst_rungs)

    if dest_position is not None:
        if dest_position < 0 or dest_position > dst_count:
            raise IndexError(
                f"Destination position {dest_position} is out of range "
                f"(routine has {dst_count} rungs; valid range is 0..{dst_count})"
            )
        dst_rll.insert(dest_position, new_rung)
    else:
        dst_rll.append(new_rung)

    _renumber_rungs(dst_rll)
    return new_rung


def duplicate_rung_with_substitution(
    project: etree._Element,
    program_name: str,
    routine_name: str,
    rung_number: int,
    substitution_map: dict,
    new_comment: str = None,
) -> etree._Element:
    """Duplicate a rung in the same routine with tag substitutions.

    The new rung is inserted immediately after the source rung.  The
    instruction text of the duplicate has tag names replaced according
    to *substitution_map*.  Optionally, the duplicate's comment can be
    overridden with *new_comment*.

    Args:
        project: The root ``RSLogix5000Content`` element.
        program_name: The name of the program containing the routine.
        routine_name: The name of the RLL routine.
        rung_number: The zero-based index of the rung to duplicate.
        substitution_map: Dictionary mapping old tag base names to new
            tag base names.
        new_comment: Optional replacement comment for the duplicated rung.
            If ``None``, the original comment is preserved.

    Returns:
        The newly created duplicate ``Rung`` element.

    Raises:
        KeyError: If the program or routine does not exist.
        ValueError: If the routine is not an RLL routine.
        IndexError: If *rung_number* is out of range.
    """
    routine = _find_routine(project, program_name, routine_name)
    rll_content = _find_rll_content(routine)

    src_rung = _get_rung_by_number(rll_content, rung_number)
    new_rung = deep_copy(src_rung)

    # Apply tag substitutions.
    if substitution_map:
        text_elem = new_rung.find("Text")
        if text_elem is not None and text_elem.text:
            new_text = substitute_tags(text_elem.text, substitution_map)
            set_cdata_text(text_elem, new_text)

    # Override comment if requested.
    if new_comment is not None:
        comment_elem = new_rung.find("Comment")
        if comment_elem is None:
            comment_elem = etree.Element("Comment")
            new_rung.insert(0, comment_elem)
        set_cdata_text(comment_elem, new_comment)

    # Insert immediately after the source rung.
    insert_position = rung_number + 1
    rungs = rll_content.findall("Rung")
    if insert_position >= len(rungs):
        rll_content.append(new_rung)
    else:
        # Find the actual element index in the parent (accounts for
        # non-Rung children that may exist in RLLContent).
        ref_rung = rungs[insert_position]
        parent_index = list(rll_content).index(ref_rung)
        rll_content.insert(parent_index, new_rung)

    _renumber_rungs(rll_content)
    return new_rung


# ---------------------------------------------------------------------------
# Structured Text operations
# ---------------------------------------------------------------------------

def add_st_line(
    project: etree._Element,
    program_name: str,
    routine_name: str,
    line_text: str,
    position: int = None,
) -> etree._Element:
    """Add a line to a Structured Text routine.

    Each line in an ST routine is represented as a ``<Line>`` element
    within the ``<STContent>`` container.  Lines are numbered sequentially
    starting from 0.

    Args:
        project: The root ``RSLogix5000Content`` element.
        program_name: The name of the program containing the routine.
        routine_name: The name of the ST routine.
        line_text: The Structured Text line content.
        position: Zero-based insertion index.  ``None`` to append.

    Returns:
        The newly created ``Line`` element.

    Raises:
        KeyError: If the program or routine does not exist.
        ValueError: If the routine is not an ST routine.
        IndexError: If *position* is out of range.
    """
    routine = _find_routine(project, program_name, routine_name)
    st_content = _find_st_content(routine)

    lines = st_content.findall("Line")
    line_count = len(lines)

    if position is not None:
        if position < 0 or position > line_count:
            raise IndexError(
                f"Position {position} is out of range "
                f"(routine has {line_count} lines; valid range is 0..{line_count})"
            )

    # Build the Line element.
    line_elem = etree.Element("Line", attrib={"Number": "0"})
    set_cdata_text(line_elem, line_text)

    if position is not None:
        st_content.insert(position, line_elem)
    else:
        st_content.append(line_elem)

    # Renumber all lines.
    for idx, ln in enumerate(st_content.findall("Line")):
        ln.set("Number", str(idx))

    return line_elem


def set_st_content(
    project: etree._Element,
    program_name: str,
    routine_name: str,
    lines: List[str],
) -> None:
    """Replace all content of a Structured Text routine.

    Removes all existing ``<Line>`` elements and recreates them from the
    provided list of line strings.

    Args:
        project: The root ``RSLogix5000Content`` element.
        program_name: The name of the program containing the routine.
        routine_name: The name of the ST routine.
        lines: List of Structured Text lines.  Each string becomes one
            ``<Line>`` element.

    Raises:
        KeyError: If the program or routine does not exist.
        ValueError: If the routine is not an ST routine.
    """
    routine = _find_routine(project, program_name, routine_name)
    st_content = _find_st_content(routine)

    # Remove all existing Line elements.
    for existing_line in st_content.findall("Line"):
        st_content.remove(existing_line)

    # Add new lines.
    for idx, line_text in enumerate(lines):
        line_elem = etree.SubElement(
            st_content, "Line", attrib={"Number": str(idx)}
        )
        set_cdata_text(line_elem, line_text)


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def add_jsr_rung(
    project: etree._Element,
    program_name: str,
    routine_name: str,
    target_routine: str,
    position: int = None,
    comment: str = None,
) -> etree._Element:
    """Add a JSR (Jump to Subroutine) rung to an RLL routine.

    Creates a rung with the instruction ``JSR(target_routine,0);`` which
    is the standard pattern for calling a subroutine routine from a main
    or calling routine.

    Args:
        project: The root ``RSLogix5000Content`` element.
        program_name: The name of the program containing the routine.
        routine_name: The name of the RLL routine to add the JSR rung to.
        target_routine: The name of the routine to jump to.
        position: Zero-based insertion index.  ``None`` to append.
        comment: Optional rung comment text.

    Returns:
        The newly created ``Rung`` element.

    Raises:
        KeyError: If the program or routine does not exist.
        ValueError: If the routine is not an RLL routine.
        IndexError: If *position* is out of range.
    """
    instruction_text = f"JSR({target_routine},0);"
    return add_rung(
        project,
        program_name,
        routine_name,
        instruction_text,
        comment=comment,
        position=position,
    )


# ---------------------------------------------------------------------------
# Task scheduling
# ---------------------------------------------------------------------------

def schedule_program(
    project: etree._Element, task_name: str, program_name: str
) -> None:
    """Add a program to a task's scheduled programs.

    If the program is already scheduled in the task, this is a no-op.

    Args:
        project: The root ``RSLogix5000Content`` element.
        task_name: The name of the task to schedule the program in.
        program_name: The name of the program to schedule.

    Raises:
        KeyError: If the task does not exist.
        KeyError: If the program does not exist (validated before scheduling).
    """
    # Validate that the program exists.
    _find_program(project, program_name)

    task = _find_task(project, task_name)
    scheduled = find_or_create(task, "ScheduledPrograms")

    # Check if already scheduled.
    for sp in scheduled.findall("ScheduledProgram"):
        if sp.get("Name", "").lower() == program_name.lower():
            return  # Already scheduled; nothing to do.

    etree.SubElement(
        scheduled, "ScheduledProgram", attrib={"Name": program_name}
    )


def unschedule_program(
    project: etree._Element, task_name: str, program_name: str
) -> None:
    """Remove a program from a task's scheduled programs.

    If the program is not scheduled in the task, this is a no-op.

    Args:
        project: The root ``RSLogix5000Content`` element.
        task_name: The name of the task.
        program_name: The name of the program to unschedule.

    Raises:
        KeyError: If the task does not exist.
    """
    task = _find_task(project, task_name)
    scheduled = task.find("ScheduledPrograms")
    if scheduled is None:
        return

    for sp in scheduled.findall("ScheduledProgram"):
        if sp.get("Name", "").lower() == program_name.lower():
            scheduled.remove(sp)
            return
