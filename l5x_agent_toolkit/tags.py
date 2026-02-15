"""
Tag CRUD operations for L5X (Rockwell Automation PLC) files.

Provides functions to create, read, update, delete, copy, move, and rename
tags in an L5X project.  All operations produce structurally correct XML
that Studio 5000 Logix Designer can import without errors.

Tags live in one of two scopes:
  - **Controller** scope (global tags visible to all programs)
  - **Program** scope (local to a single program)

Each tag is an XML ``<Tag>`` element that contains:
  - Attributes (Name, DataType, Radix, etc.)
  - An optional ``<Description>`` child with CDATA text
  - A ``<Data Format="L5K">`` child with compact text representation
  - A ``<Data Format="Decorated">`` child with verbose XML representation

This module handles atomic/base types (BOOL, SINT, INT, DINT, REAL, etc.),
built-in structures (TIMER, COUNTER, CONTROL), user-defined types (UDTs),
Add-On Instruction backing tags, and arrays of any of those types.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Dict, List, Optional, Union

from lxml import etree

from . import data_format
from .utils import (
    set_cdata_text,
    make_description_element,
    validate_tag_name,
    deep_copy,
    get_description,
    set_description,
    get_element_cdata,
)
from .schema import (
    BASE_DATA_TYPES,
    BUILTIN_STRUCTURES,
    VALID_EXTERNAL_ACCESS,
    MAX_TAG_NAME_LENGTH,
    ALARM_DIGITAL_DEFAULTS,
    ALARM_SEVERITY_MIN,
    ALARM_SEVERITY_MAX,
    TAG_CHILD_ORDER,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_tags_element(project, scope: str, program_name: Optional[str]) -> etree._Element:
    """Return the ``<Tags>`` container element for the requested scope.

    For controller scope, returns the ``<Tags>`` child of the Controller
    element.  For program scope, returns the ``<Tags>`` child of the
    specified ``<Program>`` element.

    Args:
        project: L5XProject instance.
        scope: ``'controller'`` or ``'program'``.
        program_name: Required when *scope* is ``'program'``.

    Returns:
        The ``<Tags>`` XML element.

    Raises:
        ValueError: If *scope* is invalid.
        ValueError: If *scope* is ``'program'`` but *program_name* is not provided.
        KeyError: If the specified program does not exist.
    """
    if scope == 'controller':
        tags_elem = project.controller_tags_element
        if tags_elem is None:
            # Create the Tags container if it doesn't exist yet
            # (e.g. in rung export files that lack a controller Tags element).
            tags_elem = etree.SubElement(project.controller, 'Tags')
        return tags_elem
    elif scope == 'program':
        if not program_name:
            raise ValueError(
                "program_name is required when scope is 'program'"
            )
        program_elem = project.get_program_element(program_name)
        if program_elem is None:
            raise KeyError(f"Program '{program_name}' not found in project")
        tags_elem = program_elem.find('Tags')
        if tags_elem is None:
            # Create the Tags container if it doesn't exist yet.
            tags_elem = etree.SubElement(program_elem, 'Tags')
        return tags_elem
    else:
        raise ValueError(
            f"Invalid scope '{scope}'. Must be 'controller' or 'program'"
        )


def _append_with_tail(container: etree._Element, child: etree._Element) -> None:
    """Append *child* to *container* with whitespace tail matching siblings.

    lxml preserves text/tail from parsed XML but newly created elements
    have no tail, causing them to run together on the same line as the
    preceding closing tag.  This helper copies the tail from the last
    existing sibling (or derives it from the container's text) so that
    newly added elements start on their own line.
    """
    existing = list(container)
    if existing:
        # Match the tail of the last existing child.
        ref_tail = existing[-1].tail
    elif container.text and '\n' in container.text:
        # No children yet — derive indent from container's text.
        ref_tail = container.text
    else:
        ref_tail = None

    container.append(child)

    if ref_tail:
        # The element we just appended gets the sibling tail so it starts
        # on a new line.  The *previous* last sibling keeps its original
        # tail (which already has the right spacing).
        child.tail = ref_tail


def _is_safety_scope(project, program_name: Optional[str]) -> bool:
    """Return ``True`` if *program_name* refers to a safety program."""
    if not program_name:
        return False
    try:
        return project.is_safety_program(program_name)
    except (KeyError, AttributeError):
        return False


def _resolve_tag_class(
    scope: str,
    tag_class: Optional[str],
    project,
    program_name: Optional[str],
) -> Optional[str]:
    """Determine the Class attribute for a new tag.

    Returns the class string to set, or ``None`` if no Class attribute
    should be emitted (program-scoped standard tags).
    """
    if tag_class:
        return tag_class
    if scope == 'controller':
        return 'Standard'
    # Program scope — auto-detect from program type.
    if _is_safety_scope(project, program_name):
        return 'Safety'
    return None  # program-scoped standard tags have no Class


def _find_tag_element(
    project, name: str, scope: str, program_name: Optional[str]
) -> Optional[etree._Element]:
    """Locate a ``<Tag>`` element by name within the given scope.

    Args:
        project: L5XProject instance.
        name: Tag name to find.
        scope: ``'controller'`` or ``'program'``.
        program_name: Required when *scope* is ``'program'``.

    Returns:
        The matching ``<Tag>`` element, or ``None`` if not found.
    """
    try:
        if scope == 'controller':
            return project.get_controller_tag_element(name)
        else:
            if not program_name:
                raise ValueError(
                    "program_name is required when scope is 'program'"
                )
            return project.get_program_tag_element(program_name, name)
    except KeyError:
        return None


def _default_radix(data_type: str) -> Optional[str]:
    """Return the default display radix for a data type, or ``None`` if the
    type is a structure (structures don't carry a Radix attribute on the
    ``<Tag>`` element itself).

    Args:
        data_type: The L5X data type name.

    Returns:
        A radix string like ``'Decimal'`` or ``'Float'``, or ``None``.
    """
    if data_type in BASE_DATA_TYPES:
        return BASE_DATA_TYPES[data_type]['radix']
    # Structures (built-in and UDT) do not carry Radix on the Tag element.
    return None


def _is_structure_type(project, data_type: str) -> bool:
    """Return ``True`` if *data_type* is a structure (built-in, UDT, or AOI).

    Args:
        project: L5XProject instance (used for UDT/AOI lookup).
        data_type: The data type name to check.

    Returns:
        ``True`` if the type is a structure of any kind.
    """
    if data_type in BUILTIN_STRUCTURES:
        return True
    # Check user-defined types.
    try:
        udt_elem = project.get_data_type_element(data_type)
        if udt_elem is not None:
            return True
    except (KeyError, AttributeError):
        pass
    # Check Add-On Instructions.
    try:
        aoi_elem = project.get_aoi_element(data_type)
        if aoi_elem is not None:
            return True
    except (KeyError, AttributeError):
        pass
    return False


def _resolve_data_type(project, data_type: str) -> str:
    """Verify that *data_type* is a recognized type and return its canonical
    name.

    Raises:
        KeyError: If the data type is not recognized.
    """
    # Base types.
    if data_type in BASE_DATA_TYPES:
        return data_type
    # Built-in structures.
    if data_type in BUILTIN_STRUCTURES:
        return data_type
    # User-defined types.
    try:
        udt_elem = project.get_data_type_element(data_type)
        if udt_elem is not None:
            return data_type
    except (KeyError, AttributeError):
        pass
    # Add-On Instructions.
    try:
        aoi_elem = project.get_aoi_element(data_type)
        if aoi_elem is not None:
            return data_type
    except (KeyError, AttributeError):
        pass
    raise KeyError(
        f"Data type '{data_type}' is not recognized. "
        f"It must be a base type ({', '.join(sorted(BASE_DATA_TYPES))}), "
        f"a built-in structure ({', '.join(sorted(BUILTIN_STRUCTURES))}), "
        f"a UDT, or an AOI defined in the project."
    )


def _get_routines_for_scope(
    project, scope: str, program_name: Optional[str]
) -> List[etree._Element]:
    """Return all ``<Routine>`` elements relevant to the given scope.

    For controller scope, returns routines from *all* programs (since
    controller tags can be referenced anywhere).  For program scope,
    returns only the routines within the specified program.

    Args:
        project: L5XProject instance.
        scope: ``'controller'`` or ``'program'``.
        program_name: Required when *scope* is ``'program'``.

    Returns:
        List of ``<Routine>`` elements.
    """
    routines = []
    if scope == 'controller':
        # Controller tags can be referenced in any program's routines.
        controller = project.controller_tags_element.getparent()
        programs_elem = controller.find('Programs')
        if programs_elem is not None:
            for prog in programs_elem.findall('Program'):
                routines_elem = prog.find('Routines')
                if routines_elem is not None:
                    routines.extend(routines_elem.findall('Routine'))
    else:
        prog_elem = project.get_program_element(program_name)
        if prog_elem is not None:
            routines_elem = prog_elem.find('Routines')
            if routines_elem is not None:
                routines.extend(routines_elem.findall('Routine'))
    return routines


def _update_rung_references(
    routines: List[etree._Element], old_name: str, new_name: str
) -> int:
    """Replace all occurrences of *old_name* with *new_name* in rung text
    across the given routines.

    Uses word-boundary-aware regex to prevent partial matches (e.g.
    renaming ``Tag1`` must not affect ``Tag10``).

    Args:
        routines: List of ``<Routine>`` elements to scan.
        old_name: Current tag name.
        new_name: Replacement tag name.

    Returns:
        The number of rung text substitutions made.
    """
    # Build a regex that matches the old name as a complete identifier
    # token.  In L5X rung text, a tag name can be followed by a dot
    # (member access), bracket (array index), comma, close-paren, space,
    # semicolon, or end of string.
    pattern = re.compile(
        r'(?<![A-Za-z0-9_])'       # Not preceded by a word char
        + re.escape(old_name)
        + r'(?=[.\[\)\, ;}\]\n]|$)' # Followed by delimiter or end
    )
    count = 0
    for routine in routines:
        # RLL routines store rungs in <RLLContent><Rung><Text>
        rll_content = routine.find('RLLContent')
        if rll_content is None:
            continue
        for rung in rll_content.findall('Rung'):
            text_elem = rung.find('Text')
            if text_elem is None:
                continue
            original = text_elem.text
            if original is None:
                continue
            # Extract raw text (may be CDATA-wrapped internally by lxml).
            new_text, n = pattern.subn(new_name, original)
            if n > 0:
                # Preserve CDATA wrapping.
                text_elem.text = etree.CDATA(new_text)
                count += n

        # Also handle Structured Text routines.
        st_content = routine.find('STContent')
        if st_content is None:
            continue
        for line in st_content.findall('Line'):
            text_elem = line.find('Text') if line.find('Text') is not None else line
            original = text_elem.text
            if original is None:
                continue
            new_text, n = pattern.subn(new_name, original)
            if n > 0:
                text_elem.text = etree.CDATA(new_text)
                count += n

    return count


def _format_python_value(value, data_type: str) -> str:
    """Convert a Python value to an L5X-compatible string representation.

    Args:
        value: The Python value (int, float, bool, str).
        data_type: The L5X data type name.

    Returns:
        String representation suitable for XML Value attributes and L5K data.
    """
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, float) or data_type in ('REAL', 'LREAL'):
        return str(float(value))
    if isinstance(value, int):
        return str(int(value))
    return str(value)


# ---------------------------------------------------------------------------
# Alarm Conditions
# ---------------------------------------------------------------------------

# Attributes copied from MemberAlarmDefinition to AlarmCondition.
_ALARM_COPY_ATTRS = [
    'Input', 'ConditionType', 'Limit', 'Severity',
    'OnDelay', 'OffDelay', 'ShelveDuration', 'MaxShelveDuration',
    'Deadband', 'AlarmSetOperIncluded', 'AlarmSetRollupIncluded',
    'AckRequired', 'Latched', 'EvaluationPeriod', 'Expression',
]

# Additional boolean attributes defaulting to false on AlarmCondition.
_ALARM_BOOL_ATTRS = [
    'InFault', 'ProgAck', 'OperAck', 'ProgReset', 'OperReset',
    'ProgSuppress', 'OperSuppress', 'ProgUnsuppress', 'OperUnsuppress',
    'OperShelve', 'ProgUnshelve', 'OperUnshelve',
    'ProgDisable', 'OperDisable', 'ProgEnable', 'OperEnable',
    'AlarmCountReset',
]


def _add_alarm_conditions(
    project, tag_elem: etree._Element, data_type: str
) -> None:
    """Generate and append ``<AlarmConditions>`` to *tag_elem* if the data
    type has a ``DatatypeAlarmDefinition`` in the project.

    Studio 5000 expects every tag instance whose data type carries alarm
    definitions to include an ``<AlarmConditions>`` element as the FIRST
    child of the ``<Tag>`` element.  Each ``MemberAlarmDefinition`` in the
    type definition becomes an ``<AlarmCondition>`` on the tag instance.

    If the project has no ``AlarmDefinitions`` for *data_type*, this
    function is a no-op.
    """
    try:
        dtad = project.get_alarm_definition(data_type)
    except (AttributeError, TypeError):
        return
    if dtad is None:
        return

    alarm_conditions = etree.SubElement(tag_elem, 'AlarmConditions')

    for mad in dtad.findall('MemberAlarmDefinition'):
        name = mad.get('Name', '')
        ac = etree.SubElement(alarm_conditions, 'AlarmCondition')
        ac.set('Name', name)
        ac.set('AlarmConditionDefinition', name)

        # Copy attributes from the definition.
        for attr in _ALARM_COPY_ATTRS:
            val = mad.get(attr)
            if val is not None:
                ac.set(attr, val)

        # Tag-instance attributes.
        ac.set('Used', 'true')

        # Boolean attributes that default to false on a new tag instance.
        for attr in _ALARM_BOOL_ATTRS:
            ac.set(attr, 'false')

        # Empty AlarmConfig element (tag instances don't carry messages).
        etree.SubElement(ac, 'AlarmConfig')


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_tag(
    project,
    name: str,
    data_type: str,
    scope: str = 'controller',
    program_name: Optional[str] = None,
    dimensions: Optional[str] = None,
    description: Optional[str] = None,
    radix: Optional[str] = None,
    constant: bool = False,
    external_access: str = 'Read/Write',
    tag_class: Optional[str] = None,
) -> etree._Element:
    """Create a new tag and add it to the appropriate scope.

    Builds a complete ``<Tag>`` element including both ``<Data Format="L5K">``
    and ``<Data Format="Decorated">`` children, then appends it to the
    ``<Tags>`` container in the specified scope.

    Args:
        project: L5XProject instance.
        name: Tag name (validated against L5X naming rules).
        data_type: Data type (``DINT``, ``TIMER``, custom UDT name, etc.).
        scope: ``'controller'`` or ``'program'``.
        program_name: Required if *scope* is ``'program'``.
        dimensions: Array dimensions as a string (e.g. ``'10'`` or ``'10,5'``).
            ``None`` for scalar tags.
        description: Optional description text.
        radix: Display radix.  Auto-detected from *data_type* if ``None``.
        constant: Whether the tag is constant.
        external_access: Access level (``'Read/Write'``, ``'Read Only'``, or
            ``'None'``).
        tag_class: Tag class (``'Standard'`` or ``'Safety'``).  When ``None``,
            auto-detects: ``'Standard'`` for controller scope, ``'Safety'``
            if the target program is a safety program, omitted otherwise.

    Returns:
        The created ``<Tag>`` XML element.

    Raises:
        ValueError: If *name* is invalid or tag already exists.
        KeyError: If *data_type* is not recognized, or program not found.
    """
    logger.info("Creating tag %r (type=%s, scope=%s)", name, data_type, scope)
    # Validate the tag name.
    validate_tag_name(name)

    # Validate data type.
    data_type = _resolve_data_type(project, data_type)

    # Validate external access.
    if external_access not in VALID_EXTERNAL_ACCESS:
        raise ValueError(
            f"Invalid external_access '{external_access}'. "
            f"Must be one of: {', '.join(sorted(VALID_EXTERNAL_ACCESS))}"
        )

    # Check that the tag doesn't already exist.
    if tag_exists(project, name, scope, program_name):
        scope_desc = (
            f"program '{program_name}'" if scope == 'program' else 'controller'
        )
        raise ValueError(
            f"Tag '{name}' already exists in {scope_desc} scope"
        )

    # Determine the radix.
    if radix is None:
        radix = _default_radix(data_type)

    # Build the <Tag> element.
    # Attribute order matches Studio 5000 exports:
    #   Name, [Class], TagType, DataType, [Radix], [Dimensions], Constant, ExternalAccess, [OpcUaAccess]
    tag_attrib: dict[str, str] = {'Name': name}

    resolved_class = _resolve_tag_class(scope, tag_class, project, program_name)
    if resolved_class:
        tag_attrib['Class'] = resolved_class

    tag_attrib['TagType'] = 'Base'
    tag_attrib['DataType'] = data_type

    # Radix is only set for atomic types (not structures).
    if radix is not None:
        tag_attrib['Radix'] = radix

    # Dimensions for arrays.
    if dimensions is not None:
        tag_attrib['Dimensions'] = str(dimensions)

    tag_attrib['Constant'] = 'true' if constant else 'false'
    tag_attrib['ExternalAccess'] = external_access

    tag_elem = etree.Element('Tag', attrib=tag_attrib)

    # Generate AlarmConditions if the data type has a DatatypeAlarmDefinition.
    # Must come BEFORE Description and Data per L5X schema ordering.
    _add_alarm_conditions(project, tag_elem, data_type)

    # Add description if provided.
    if description is not None:
        desc_elem = make_description_element(description)
        tag_elem.append(desc_elem)

    # Generate both Data elements (L5K and Decorated) using data_format.
    data_elems = data_format.generate_tag_data_elements(
        data_type,
        dimensions=dimensions,
        radix=radix,
        project=project,
    )
    for de in data_elems:
        tag_elem.append(de)

    # Insert the tag into the appropriate <Tags> container.
    tags_container = _get_tags_element(project, scope, program_name)
    _append_with_tail(tags_container, tag_elem)

    return tag_elem


def delete_tag(
    project,
    name: str,
    scope: str = 'controller',
    program_name: Optional[str] = None,
) -> etree._Element:
    """Delete a tag from the specified scope.

    Removes the ``<Tag>`` element from its parent ``<Tags>`` container
    and returns the removed element (which can be inspected or re-inserted
    elsewhere).

    Args:
        project: L5XProject instance.
        name: Tag name to delete.
        scope: ``'controller'`` or ``'program'``.
        program_name: Required if *scope* is ``'program'``.

    Returns:
        The removed ``<Tag>`` element.

    Raises:
        KeyError: If the tag does not exist in the specified scope.
    """
    logger.info("Deleting tag %r (scope=%s)", name, scope or "controller")
    tag_elem = _find_tag_element(project, name, scope, program_name)
    if tag_elem is None:
        scope_desc = (
            f"program '{program_name}'" if scope == 'program' else 'controller'
        )
        raise KeyError(
            f"Tag '{name}' not found in {scope_desc} scope"
        )

    parent = tag_elem.getparent()
    parent.remove(tag_elem)
    return tag_elem


def rename_tag(
    project,
    old_name: str,
    new_name: str,
    scope: str = 'controller',
    program_name: Optional[str] = None,
    update_references: bool = True,
) -> None:
    """Rename a tag, optionally updating all references in routine logic.

    Changes the ``Name`` attribute of the tag element.  If
    *update_references* is ``True``, scans all relevant routines and
    performs word-boundary-safe substitution of the old name with the new
    name in rung text and structured text.

    Args:
        project: L5XProject instance.
        old_name: Current tag name.
        new_name: Desired new tag name.
        scope: ``'controller'`` or ``'program'``.
        program_name: Required if *scope* is ``'program'``.
        update_references: If ``True``, update all routine rung/ST text
            that references the old tag name.

    Raises:
        KeyError: If the tag does not exist.
        ValueError: If *new_name* is invalid or already exists.
    """
    logger.info("Renaming tag %r → %r", old_name, new_name)
    # Validate new name.
    validate_tag_name(new_name)

    # Find the existing tag.
    tag_elem = _find_tag_element(project, old_name, scope, program_name)
    if tag_elem is None:
        scope_desc = (
            f"program '{program_name}'" if scope == 'program' else 'controller'
        )
        raise KeyError(
            f"Tag '{old_name}' not found in {scope_desc} scope"
        )

    # Check that the new name isn't already taken.
    if tag_exists(project, new_name, scope, program_name):
        scope_desc = (
            f"program '{program_name}'" if scope == 'program' else 'controller'
        )
        raise ValueError(
            f"Tag '{new_name}' already exists in {scope_desc} scope"
        )

    # Update the Name attribute.
    tag_elem.set('Name', new_name)

    # Update references in routines if requested.
    if update_references:
        routines = _get_routines_for_scope(project, scope, program_name)
        _update_rung_references(routines, old_name, new_name)


def copy_tag(
    project,
    source_name: str,
    new_name: str,
    source_scope: str = 'controller',
    source_program: Optional[str] = None,
    dest_scope: str = 'controller',
    dest_program: Optional[str] = None,
) -> etree._Element:
    """Deep copy a tag to a new name, optionally in a different scope.

    Creates an independent deep copy of the source tag element (including
    all children such as Data, Description, and AlarmConditions), sets the
    new name, and inserts it into the destination scope.

    Args:
        project: L5XProject instance.
        source_name: Name of the tag to copy.
        new_name: Name for the copy.
        source_scope: Source scope (``'controller'`` or ``'program'``).
        source_program: Source program name (if applicable).
        dest_scope: Destination scope (``'controller'`` or ``'program'``).
        dest_program: Destination program name (if applicable).

    Returns:
        The newly created ``<Tag>`` element.

    Raises:
        KeyError: If the source tag does not exist.
        ValueError: If *new_name* is invalid or already exists in the
            destination scope.
    """
    logger.info("Copying tag %r → %r", source_name, new_name)
    # Validate new name.
    validate_tag_name(new_name)

    # Find source tag.
    source_elem = _find_tag_element(
        project, source_name, source_scope, source_program
    )
    if source_elem is None:
        scope_desc = (
            f"program '{source_program}'"
            if source_scope == 'program'
            else 'controller'
        )
        raise KeyError(
            f"Tag '{source_name}' not found in {scope_desc} scope"
        )

    # Verify destination doesn't already have this name.
    if tag_exists(project, new_name, dest_scope, dest_program):
        scope_desc = (
            f"program '{dest_program}'"
            if dest_scope == 'program'
            else 'controller'
        )
        raise ValueError(
            f"Tag '{new_name}' already exists in {scope_desc} scope"
        )

    # Deep copy and rename.
    new_elem = deep_copy(source_elem)
    new_elem.set('Name', new_name)

    # Insert into destination.
    dest_tags = _get_tags_element(project, dest_scope, dest_program)
    _append_with_tail(dest_tags, new_elem)

    return new_elem


def move_tag(
    project,
    name: str,
    from_scope: str = 'controller',
    from_program: Optional[str] = None,
    to_scope: str = 'controller',
    to_program: Optional[str] = None,
) -> None:
    """Move a tag from one scope to another.

    Removes the tag from the source scope, inserts it into the destination
    scope, and updates references in the relevant routines.

    If moving from controller to program scope, references in routines
    *outside* the destination program will become unresolved.  If moving
    from program to controller scope, the tag becomes globally available.

    Args:
        project: L5XProject instance.
        name: Tag name to move.
        from_scope: Source scope (``'controller'`` or ``'program'``).
        from_program: Source program name (if applicable).
        to_scope: Destination scope (``'controller'`` or ``'program'``).
        to_program: Destination program name (if applicable).

    Raises:
        KeyError: If the tag does not exist in the source scope.
        ValueError: If the tag already exists in the destination scope.
    """
    logger.info("Moving tag %r to %s scope", name, to_scope)
    # Verify the tag exists in the source.
    tag_elem = _find_tag_element(project, name, from_scope, from_program)
    if tag_elem is None:
        scope_desc = (
            f"program '{from_program}'"
            if from_scope == 'program'
            else 'controller'
        )
        raise KeyError(
            f"Tag '{name}' not found in {scope_desc} scope"
        )

    # Verify the destination doesn't already have a tag with this name.
    if tag_exists(project, name, to_scope, to_program):
        scope_desc = (
            f"program '{to_program}'"
            if to_scope == 'program'
            else 'controller'
        )
        raise ValueError(
            f"Tag '{name}' already exists in {scope_desc} scope"
        )

    # Remove from source.
    source_parent = tag_elem.getparent()
    source_parent.remove(tag_elem)

    # Insert into destination.
    dest_tags = _get_tags_element(project, to_scope, to_program)
    _append_with_tail(dest_tags, tag_elem)


def set_tag_value(
    project,
    name: str,
    value,
    scope: str = 'controller',
    program_name: Optional[str] = None,
) -> None:
    """Set the value of a scalar (non-structured, non-array) tag.

    Updates both the ``<Data Format="L5K">`` compact text representation
    and the ``<Data Format="Decorated">`` verbose XML representation.

    Args:
        project: L5XProject instance.
        name: Tag name.
        value: Python value (``int``, ``float``, ``bool``).
        scope: ``'controller'`` or ``'program'``.
        program_name: Required if *scope* is ``'program'``.

    Raises:
        KeyError: If the tag does not exist.
        ValueError: If the tag is not a scalar type.
    """
    logger.info("Setting value for tag %r", name)
    tag_elem = _find_tag_element(project, name, scope, program_name)
    if tag_elem is None:
        scope_desc = (
            f"program '{program_name}'" if scope == 'program' else 'controller'
        )
        raise KeyError(
            f"Tag '{name}' not found in {scope_desc} scope"
        )

    data_type = tag_elem.get('DataType', '')
    dimensions = tag_elem.get('Dimensions')

    if dimensions is not None:
        raise ValueError(
            f"Tag '{name}' is an array (Dimensions={dimensions}). "
            f"Use set_tag_member_value() with an index path instead."
        )

    if _is_structure_type(project, data_type):
        raise ValueError(
            f"Tag '{name}' is a structured type '{data_type}'. "
            f"Use set_tag_member_value() to set individual member values."
        )

    str_value = _format_python_value(value, data_type)

    # Update L5K data.
    for data_elem in tag_elem.findall('Data'):
        fmt = data_elem.get('Format', '')
        if fmt == 'L5K':
            data_elem.text = etree.CDATA(str_value)
        elif fmt == 'Decorated':
            # For scalar types, the Decorated data contains a <DataValue>
            # element with a Value attribute.
            data_value = data_elem.find('DataValue')
            if data_value is not None:
                data_value.set('Value', str_value)


def set_tag_member_value(
    project,
    name: str,
    member_path: str,
    value,
    scope: str = 'controller',
    program_name: Optional[str] = None,
) -> None:
    """Set a specific member value in a structured or array tag.

    Updates both the L5K and Decorated data representations for the
    specified member.

    Args:
        project: L5XProject instance.
        name: Tag name.
        member_path: Dot-separated member path for structures (e.g.
            ``'PRE'`` or ``'Status.Active'``), or bracket-indexed path
            for arrays (e.g. ``'[0]'`` or ``'[2].Member'``).
        value: Python value (``int``, ``float``, ``bool``).
        scope: ``'controller'`` or ``'program'``.
        program_name: Required if *scope* is ``'program'``.

    Raises:
        KeyError: If the tag or member does not exist.
    """
    logger.info("Setting member %r on tag %r", member_path, name)
    tag_elem = _find_tag_element(project, name, scope, program_name)
    if tag_elem is None:
        scope_desc = (
            f"program '{program_name}'" if scope == 'program' else 'controller'
        )
        raise KeyError(
            f"Tag '{name}' not found in {scope_desc} scope"
        )

    data_type = tag_elem.get('DataType', '')
    str_value = _format_python_value(value, data_type)

    # --- Update Decorated data ---
    decorated_data = None
    for data_elem in tag_elem.findall('Data'):
        if data_elem.get('Format') == 'Decorated':
            decorated_data = data_elem
            break

    if decorated_data is not None:
        _set_decorated_member(decorated_data, member_path, str_value)

    # --- Update L5K data ---
    # L5K data is a compact text representation.  For structures, it's a
    # bracket-delimited list.  Updating it precisely requires understanding
    # the member ordering.  We delegate to data_format if available,
    # otherwise we rebuild from the Decorated data.
    for data_elem in tag_elem.findall('Data'):
        if data_elem.get('Format') == 'L5K':
            _rebuild_l5k_from_decorated(tag_elem, data_elem, project)
            break


def _set_decorated_member(
    decorated_data: etree._Element,
    member_path: str,
    str_value: str,
) -> None:
    """Navigate the Decorated data tree and set the value at *member_path*.

    Handles both structure members (dot-separated) and array indices
    (bracket-enclosed).

    Args:
        decorated_data: The ``<Data Format="Decorated">`` element.
        member_path: Path like ``'PRE'``, ``'[0]'``, ``'[2].EN'``, or
            ``'Status.Active'``.
        str_value: The string value to set.

    Raises:
        KeyError: If the member path cannot be resolved.
    """
    # Parse member_path into segments.
    segments = _parse_member_path(member_path)

    # Start from the first child of Decorated data (Structure, Array, or
    # DataValue).
    current = None
    for child in decorated_data:
        if child.tag in ('Structure', 'Array', 'DataValue'):
            current = child
            break

    if current is None:
        raise KeyError(
            f"No Structure, Array, or DataValue found in Decorated data"
        )

    # Navigate through segments.
    for i, seg in enumerate(segments):
        is_last = (i == len(segments) - 1)

        if seg['type'] == 'index':
            # Array index: find <Element Index="[n]">
            index_str = f"[{seg['value']}]"
            found = None
            for elem in current.findall('Element'):
                if elem.get('Index') == index_str:
                    found = elem
                    break
            if found is None:
                raise KeyError(
                    f"Array index {index_str} not found in Decorated data"
                )
            if is_last:
                found.set('Value', str_value)
                return
            # If the element contains a nested Structure, descend into it.
            nested = found.find('Structure')
            if nested is not None:
                current = nested
            else:
                current = found
        elif seg['type'] == 'member':
            # Structure member: find <DataValueMember Name="...">
            member_name = seg['value']
            found = None
            for elem in current.findall('DataValueMember'):
                if elem.get('Name') == member_name:
                    found = elem
                    break
            # Also check StructureMember for nested structures.
            if found is None:
                for elem in current.findall('StructureMember'):
                    if elem.get('Name') == member_name:
                        found = elem
                        break
            if found is None:
                raise KeyError(
                    f"Member '{member_name}' not found in Decorated data"
                )
            if is_last:
                found.set('Value', str_value)
                return
            # If this member contains a nested Structure, descend.
            nested = found.find('Structure')
            if nested is not None:
                current = nested
            else:
                current = found


def _parse_member_path(member_path: str) -> List[Dict[str, str]]:
    """Parse a member path into a list of path segments.

    Each segment is a dict with keys ``'type'`` (``'member'`` or ``'index'``)
    and ``'value'`` (the member name or index string).

    Examples::

        'PRE'             -> [{'type': 'member', 'value': 'PRE'}]
        '[0]'             -> [{'type': 'index', 'value': '0'}]
        '[2].EN'          -> [{'type': 'index', 'value': '2'},
                              {'type': 'member', 'value': 'EN'}]
        'Status.Active'   -> [{'type': 'member', 'value': 'Status'},
                              {'type': 'member', 'value': 'Active'}]
        '[1,2]'           -> [{'type': 'index', 'value': '1,2'}]

    Args:
        member_path: The path string to parse.

    Returns:
        List of segment dictionaries.
    """
    segments = []
    pos = 0
    path = member_path.strip()

    while pos < len(path):
        if path[pos] == '[':
            # Array index.
            end = path.index(']', pos)
            index_val = path[pos + 1:end].strip()
            segments.append({'type': 'index', 'value': index_val})
            pos = end + 1
            # Skip trailing dot.
            if pos < len(path) and path[pos] == '.':
                pos += 1
        elif path[pos] == '.':
            # Skip leading dot.
            pos += 1
        else:
            # Member name: consume until dot, bracket, or end.
            end = pos
            while end < len(path) and path[end] not in ('.', '['):
                end += 1
            member_name = path[pos:end]
            if member_name:
                segments.append({'type': 'member', 'value': member_name})
            pos = end

    return segments


def _rebuild_l5k_from_decorated(
    tag_elem: etree._Element, l5k_data: etree._Element,
    project=None,
) -> None:
    """Rebuild the L5K compact text from the current Decorated data.

    This is called after modifying the Decorated data to keep the two
    representations in sync.

    For scalar types, the L5K text is simply the value.  For structures,
    it is a bracket-delimited list of member values.  For arrays, it is
    a bracket-delimited list of element values.

    AOI-typed tags require special handling: BOOLs must be packed into a
    bitfield at position 0 (matching Studio 5000's memory layout).  When
    *project* is provided, AOI types are detected and the L5K is rebuilt
    using :func:`data_format.rebuild_aoi_l5k_from_decorated`.

    Args:
        tag_elem: The parent ``<Tag>`` element.
        l5k_data: The ``<Data Format="L5K">`` element to update.
        project: Optional L5XProject for AOI type detection and resolution.
    """
    decorated_data = None
    for data_elem in tag_elem.findall('Data'):
        if data_elem.get('Format') == 'Decorated':
            decorated_data = data_elem
            break

    if decorated_data is None:
        return

    # Determine the top-level container type.
    for child in decorated_data:
        if child.tag == 'DataValue':
            # Scalar: L5K text is the value.
            val = child.get('Value', '0')
            l5k_data.text = etree.CDATA(val)
            return
        elif child.tag == 'Structure':
            # Check if this is an AOI type — use proper BOOL bit-packing.
            if project is not None:
                dt_name = tag_elem.get('DataType', '')
                try:
                    dt_def = project.get_data_type_definition(dt_name)
                    if (dt_def is not None
                            and dt_def.tag == 'AddOnInstructionDefinition'):
                        l5k_text = data_format.rebuild_aoi_l5k_from_decorated(
                            dt_def, child, project)
                        l5k_data.text = etree.CDATA(l5k_text)
                        return
                except (KeyError, AttributeError):
                    pass
            # Non-AOI structure: flat member-by-member approach.
            l5k_text = _structure_to_l5k(child)
            l5k_data.text = etree.CDATA(l5k_text)
            return
        elif child.tag == 'Array':
            l5k_text = _array_to_l5k(child)
            l5k_data.text = etree.CDATA(l5k_text)
            return


def _structure_to_l5k(structure_elem: etree._Element) -> str:
    """Convert a ``<Structure>`` element to L5K compact text.

    The L5K representation of a structure is a comma-separated list of
    member values enclosed in brackets.

    Args:
        structure_elem: The ``<Structure>`` element.

    Returns:
        L5K text string like ``'[0,0,0]'``.
    """
    values = []
    for member in structure_elem:
        if member.tag == 'DataValueMember':
            val = member.get('Value', '0')
            values.append(val)
        elif member.tag == 'StructureMember':
            nested = member.find('Structure')
            if nested is not None:
                values.append(_structure_to_l5k(nested))
            else:
                values.append('0')
        elif member.tag == 'ArrayMember':
            nested_array = member.find('Array')
            if nested_array is not None:
                values.append(_array_to_l5k(nested_array))
            else:
                # Build from Element children directly.
                elems = []
                for elem in member.findall('Element'):
                    elems.append(elem.get('Value', '0'))
                values.append('[' + ','.join(elems) + ']')
    return '[' + ','.join(values) + ']'


def _array_to_l5k(array_elem: etree._Element) -> str:
    """Convert an ``<Array>`` element to L5K compact text.

    Args:
        array_elem: The ``<Array>`` element.

    Returns:
        L5K text string like ``'[0,0,0,0,0]'``.
    """
    values = []
    for elem in array_elem.findall('Element'):
        # Element might contain a nested Structure (array of structures).
        nested = elem.find('Structure')
        if nested is not None:
            values.append(_structure_to_l5k(nested))
        else:
            values.append(elem.get('Value', '0'))
    return '[' + ','.join(values) + ']'


def set_tag_description(
    project,
    name: str,
    description: str,
    scope: str = 'controller',
    program_name: Optional[str] = None,
) -> None:
    """Set or update a tag's description.

    If the tag has no ``<Description>`` child, one is created.  If it
    already has one, its CDATA text is updated.  Pass ``None`` as
    *description* to remove the description entirely.

    Args:
        project: L5XProject instance.
        name: Tag name.
        description: The description text (or ``None`` to remove).
        scope: ``'controller'`` or ``'program'``.
        program_name: Required if *scope* is ``'program'``.

    Raises:
        KeyError: If the tag does not exist.
    """
    tag_elem = _find_tag_element(project, name, scope, program_name)
    if tag_elem is None:
        scope_desc = (
            f"program '{program_name}'" if scope == 'program' else 'controller'
        )
        raise KeyError(
            f"Tag '{name}' not found in {scope_desc} scope"
        )

    set_description(tag_elem, description)


def get_tag_info(
    project,
    name: str,
    scope: str = 'controller',
    program_name: Optional[str] = None,
) -> dict:
    """Get detailed information about a tag.

    Returns a dictionary with the tag's attributes, description, and
    current value (for scalar types).

    Args:
        project: L5XProject instance.
        name: Tag name.
        scope: ``'controller'`` or ``'program'``.
        program_name: Required if *scope* is ``'program'``.

    Returns:
        A dictionary with keys:
        - ``'name'``: Tag name (str)
        - ``'data_type'``: Data type name (str)
        - ``'dimensions'``: Dimensions string or ``None``
        - ``'description'``: Description text or ``None``
        - ``'value'``: Current value for scalar types, or ``None``
        - ``'radix'``: Display radix or ``None``
        - ``'constant'``: Boolean
        - ``'external_access'``: External access string
        - ``'tag_type'``: Tag type (``'Base'``, ``'Alias'``, ``'Produced'``,
          ``'Consumed'``)
        - ``'class'``: ``'Standard'``, ``'Safety'``, or ``None``
        - ``'alias_for'``: Alias target path or ``None``
        - ``'produce_info'``: Dict with produce details or ``None``
        - ``'consume_info'``: Dict with consume details or ``None``

    Raises:
        KeyError: If the tag does not exist.
    """
    logger.debug("Getting info for tag %r (scope=%s)", name, scope)
    tag_elem = _find_tag_element(project, name, scope, program_name)
    if tag_elem is None:
        scope_desc = (
            f"program '{program_name}'" if scope == 'program' else 'controller'
        )
        raise KeyError(
            f"Tag '{name}' not found in {scope_desc} scope"
        )

    # Extract description.
    desc = get_description(tag_elem)

    # Extract value from Decorated data.
    # For scalar types this returns the atomic value.
    # For structured types (UDTs, TIMER, COUNTER, etc.) this returns
    # a dict of all member values.  For arrays, a list.
    value = None
    for data_elem in tag_elem.findall('Data'):
        if data_elem.get('Format') == 'Decorated':
            children = list(data_elem)
            if children:
                value = project._parse_decorated_data(children[0])
            break

    info = {
        'name': tag_elem.get('Name', ''),
        'data_type': tag_elem.get('DataType', ''),
        'dimensions': tag_elem.get('Dimensions'),
        'description': desc,
        'value': value,
        'radix': tag_elem.get('Radix'),
        'constant': tag_elem.get('Constant', 'false').lower() == 'true',
        'external_access': tag_elem.get('ExternalAccess', 'Read/Write'),
        'tag_type': tag_elem.get('TagType', 'Base'),
        'class': tag_elem.get('Class'),
        'alias_for': tag_elem.get('AliasFor'),
        'produce_info': _extract_produce_info(tag_elem),
        'consume_info': _extract_consume_info(tag_elem),
    }
    return info


def find_tag(
    project,
    name: str,
) -> dict:
    """Search for a tag across all scopes and return full details.

    Searches controller scope first, then iterates every program scope.
    Returns the first match with complete tag information including the
    scope where it was found and all member values for structured types.

    This is the recommended entry point when the caller does not know
    which scope a tag lives in.

    Args:
        project: L5XProject instance.
        name: Tag name to search for.

    Returns:
        A dictionary with all the keys from :func:`get_tag_info` plus:
        - ``'scope'``: ``'controller'`` or ``'program'``
        - ``'program_name'``: Program name (empty string for controller scope)

    Raises:
        KeyError: If the tag is not found in any scope.
    """
    # Try controller scope first.
    tag_elem = _find_tag_element(project, name, 'controller', None)
    if tag_elem is not None:
        info = get_tag_info(project, name, scope='controller')
        info['scope'] = 'controller'
        info['program_name'] = ''
        return info

    # Search all program scopes.
    for prog_elem in project._all_program_elements():
        prog_name = prog_elem.get('Name', '')
        tag_elem = _find_tag_element(project, name, 'program', prog_name)
        if tag_elem is not None:
            info = get_tag_info(project, name, scope='program',
                                program_name=prog_name)
            info['scope'] = 'program'
            info['program_name'] = prog_name
            return info

    raise KeyError(
        f"Tag '{name}' not found in controller scope or any program scope."
    )


def _extract_produce_info(tag_elem: etree._Element) -> Optional[dict]:
    """Extract ``<ProduceInfo>`` details from a Produced tag, or ``None``."""
    pi = tag_elem.find('ProduceInfo')
    if pi is None:
        return None
    return {
        'produce_count': pi.get('ProduceCount'),
        'unicast_permitted': pi.get('UnicastPermitted'),
        'min_rpi': pi.get('MinimumRPI'),
        'max_rpi': pi.get('MaximumRPI'),
        'default_rpi': pi.get('DefaultRPI'),
    }


def _extract_consume_info(tag_elem: etree._Element) -> Optional[dict]:
    """Extract ``<ConsumeInfo>`` details from a Consumed tag, or ``None``."""
    ci = tag_elem.find('ConsumeInfo')
    if ci is None:
        return None
    return {
        'producer': ci.get('Producer'),
        'remote_tag': ci.get('RemoteTag'),
        'remote_instance': ci.get('RemoteInstance'),
        'rpi': ci.get('RPI'),
        'unicast': ci.get('Unicast'),
    }


def tag_exists(
    project,
    name: str,
    scope: str = 'controller',
    program_name: Optional[str] = None,
) -> bool:
    """Check if a tag exists in the given scope.

    Args:
        project: L5XProject instance.
        name: Tag name to check.
        scope: ``'controller'`` or ``'program'``.
        program_name: Required if *scope* is ``'program'``.

    Returns:
        ``True`` if the tag exists, ``False`` otherwise.
    """
    elem = _find_tag_element(project, name, scope, program_name)
    return elem is not None


def batch_create_tags(
    project,
    tag_specs: List[dict],
    scope: str = 'controller',
    program_name: Optional[str] = None,
) -> List[etree._Element]:
    """Create multiple tags from a list of specification dictionaries.

    Each dictionary in *tag_specs* should contain keys matching the
    parameters of :func:`create_tag`: ``'name'``, ``'data_type'``, and
    optionally ``'description'``, ``'dimensions'``, ``'radix'``,
    ``'constant'``, ``'external_access'``.

    The *scope* and *program_name* apply to all tags unless overridden
    in individual specs (via ``'scope'`` and ``'program_name'`` keys).

    Args:
        project: L5XProject instance.
        tag_specs: List of dictionaries, each describing a tag to create.
        scope: Default scope for all tags.
        program_name: Default program name for all tags.

    Returns:
        List of created ``<Tag>`` elements, one per spec.

    Raises:
        ValueError: If any tag name is invalid or already exists.
        KeyError: If any data type is not recognized.
    """
    created = []
    for spec in tag_specs:
        tag_scope = spec.get('scope', scope)
        tag_program = spec.get('program_name', program_name)

        tag_elem = create_tag(
            project,
            name=spec['name'],
            data_type=spec['data_type'],
            scope=tag_scope,
            program_name=tag_program,
            dimensions=spec.get('dimensions'),
            description=spec.get('description'),
            radix=spec.get('radix'),
            constant=spec.get('constant', False),
            external_access=spec.get('external_access', 'Read/Write'),
        )
        created.append(tag_elem)

    return created


# ===================================================================
# Alias Tag Operations
# ===================================================================

def create_alias_tag(
    project,
    name: str,
    alias_for: str,
    scope: str = 'controller',
    program_name: Optional[str] = None,
    description: Optional[str] = None,
    tag_class: Optional[str] = None,
) -> etree._Element:
    """Create an alias tag pointing to another tag or I/O path.

    Alias tags have ``TagType="Alias"`` and an ``AliasFor`` attribute.
    They inherit their data type from the aliased target and have no
    ``<Data>`` children.

    Args:
        project: L5XProject instance.
        name: Alias tag name (max 40 chars).
        alias_for: The target tag name, member path, or I/O point
            (e.g. ``'MyTag'``, ``'MyTag.Member'``, ``'Local:1:I.Data.0'``).
        scope: ``'controller'`` or ``'program'``.
        program_name: Required if *scope* is ``'program'``.
        description: Optional description text.
        tag_class: Tag class (``'Standard'`` or ``'Safety'``).  Auto-detected
            when ``None``.

    Returns:
        The created ``<Tag>`` element.

    Raises:
        ValueError: If *name* is invalid, empty *alias_for*, or tag exists.
    """
    logger.info("Creating alias tag %r → %r", name, alias_for)
    validate_tag_name(name)

    if not alias_for or not alias_for.strip():
        raise ValueError("alias_for must not be empty")

    if tag_exists(project, name, scope, program_name):
        scope_desc = (
            f"program '{program_name}'" if scope == 'program' else 'controller'
        )
        raise ValueError(
            f"Tag '{name}' already exists in {scope_desc} scope"
        )

    # Build alias tag — no DataType, no Radix, no Constant, no Data elements.
    tag_attrib: dict[str, str] = {'Name': name}

    resolved_class = _resolve_tag_class(scope, tag_class, project, program_name)
    if resolved_class:
        tag_attrib['Class'] = resolved_class

    tag_attrib['TagType'] = 'Alias'
    tag_attrib['AliasFor'] = alias_for
    tag_attrib['ExternalAccess'] = 'Read/Write'

    tag_elem = etree.Element('Tag', attrib=tag_attrib)

    if description:
        desc_elem = make_description_element(description)
        tag_elem.append(desc_elem)

    tags_container = _get_tags_element(project, scope, program_name)
    _append_with_tail(tags_container, tag_elem)

    return tag_elem


# ===================================================================
# ALARM_DIGITAL Tag Operations
# ===================================================================

def create_alarm_digital_tag(
    project,
    name: str,
    message: str,
    severity: int = 500,
    scope: str = 'controller',
    program_name: Optional[str] = None,
    description: Optional[str] = None,
    ack_required: bool = True,
    latched: bool = False,
    tag_class: Optional[str] = None,
) -> etree._Element:
    """Create an ALARM_DIGITAL tag with ``<Data Format="Alarm">``.

    ALARM_DIGITAL tags are standalone alarm tags driven by ALMD
    instructions in rung logic.  They use a special Alarm data format
    instead of the standard L5K/Decorated formats.

    Args:
        project: L5XProject instance.
        name: Tag name (max 40 chars).
        message: Alarm message text.
        severity: Alarm severity (1-1000).
        scope: ``'controller'`` or ``'program'``.
        program_name: Required when *scope* is ``'program'``.
        description: Optional tag description.
        ack_required: Whether acknowledgment is required.
        latched: Whether the alarm latches.
        tag_class: Tag class (``'Standard'`` or ``'Safety'``).  Auto-detected
            when ``None``.

    Returns:
        The created ``<Tag>`` element.
    """
    validate_tag_name(name)

    if not message:
        raise ValueError("message must be a non-empty string")
    if not (ALARM_SEVERITY_MIN <= severity <= ALARM_SEVERITY_MAX):
        raise ValueError(
            f"severity must be {ALARM_SEVERITY_MIN}-{ALARM_SEVERITY_MAX}, "
            f"got {severity}"
        )

    if tag_exists(project, name, scope, program_name):
        raise ValueError(
            f"Tag '{name}' already exists in "
            f"{'controller' if scope == 'controller' else 'program ' + str(program_name)}"
        )

    # Build tag element — attribute order matches Studio 5000 exports.
    # Alarm tags omit Constant (Studio 5000 does not include it).
    tag_elem = etree.Element('Tag')
    tag_elem.set('Name', name)
    resolved_class = _resolve_tag_class(scope, tag_class, project, program_name)
    if resolved_class:
        tag_elem.set('Class', resolved_class)
    tag_elem.set('TagType', 'Base')
    tag_elem.set('DataType', 'ALARM_DIGITAL')
    tag_elem.set('ExternalAccess', 'Read/Write')
    tag_elem.set('OpcUaAccess', 'None')

    # Description (before Data per TAG_CHILD_ORDER)
    if description:
        desc_elem = make_description_element(description)
        tag_elem.append(desc_elem)

    # Build <Data Format="Alarm">
    data_elem = etree.SubElement(tag_elem, 'Data')
    data_elem.set('Format', 'Alarm')

    # AlarmDigitalParameters
    params_elem = etree.SubElement(data_elem, 'AlarmDigitalParameters')
    defaults = dict(ALARM_DIGITAL_DEFAULTS)
    defaults['Severity'] = str(severity)
    defaults['AckRequired'] = str(ack_required).lower()
    defaults['Latched'] = str(latched).lower()
    for attr, val in defaults.items():
        params_elem.set(attr, val)

    # AlarmConfig with message
    alarm_config = etree.SubElement(data_elem, 'AlarmConfig')
    messages_elem = etree.SubElement(alarm_config, 'Messages')
    msg_elem = etree.SubElement(messages_elem, 'Message')
    msg_elem.set('Type', 'AM')
    text_elem = etree.SubElement(msg_elem, 'Text')
    text_elem.set('Lang', 'en-US')
    text_elem.text = etree.CDATA(message)

    # Insert into container
    tags_container = _get_tags_element(project, scope, program_name)
    _append_with_tail(tags_container, tag_elem)

    return tag_elem


def batch_create_alarm_digital_tags(
    project,
    tag_specs: List[dict],
    scope: str = 'controller',
    program_name: Optional[str] = None,
) -> List[etree._Element]:
    """Create multiple ALARM_DIGITAL tags from a list of specs.

    Each dictionary should contain ``'name'`` and ``'message'``, and
    optionally ``'severity'``, ``'description'``, ``'ack_required'``,
    ``'latched'``.

    Args:
        project: L5XProject instance.
        tag_specs: List of alarm specification dicts.
        scope: Default scope for all tags.
        program_name: Default program name for all tags.

    Returns:
        List of created ``<Tag>`` elements.
    """
    created = []
    for spec in tag_specs:
        tag_elem = create_alarm_digital_tag(
            project,
            name=spec['name'],
            message=spec['message'],
            severity=spec.get('severity', 500),
            scope=spec.get('scope', scope),
            program_name=spec.get('program_name', program_name),
            description=spec.get('description'),
            ack_required=spec.get('ack_required', True),
            latched=spec.get('latched', False),
        )
        created.append(tag_elem)
    return created


def get_alarm_digital_info(
    project,
    name: str,
    scope: str = 'controller',
    program_name: Optional[str] = None,
) -> dict:
    """Read configuration of an ALARM_DIGITAL or ALARM_ANALOG tag.

    Args:
        project: L5XProject instance.
        name: Tag name.
        scope: ``'controller'`` or ``'program'``.
        program_name: Required when *scope* is ``'program'``.

    Returns:
        Dictionary with alarm parameters and message text.
    """
    tag_elem = _find_tag_element(project, name, scope, program_name)
    if tag_elem is None:
        raise KeyError(
            f"Tag '{name}' not found in "
            f"{'controller' if scope == 'controller' else 'program ' + str(program_name)}"
        )

    dt = tag_elem.get('DataType', '')
    if dt not in ('ALARM_DIGITAL', 'ALARM_ANALOG'):
        raise ValueError(
            f"Tag '{name}' is DataType '{dt}', expected "
            f"ALARM_DIGITAL or ALARM_ANALOG"
        )

    result: Dict[str, Any] = {
        'name': name,
        'data_type': dt,
        'description': get_description(tag_elem),
    }

    data_elem = tag_elem.find("Data[@Format='Alarm']")
    if data_elem is None:
        return result

    # Extract parameters
    params_el = data_elem.find('AlarmDigitalParameters')
    if params_el is None:
        params_el = data_elem.find('AlarmAnalogParameters')
    if params_el is not None:
        for attr_name, attr_val in params_el.attrib.items():
            # Convert to appropriate Python types
            key = attr_name
            if attr_val in ('true', 'false'):
                result[key] = attr_val == 'true'
            else:
                try:
                    result[key] = int(attr_val)
                except ValueError:
                    result[key] = attr_val

    # Extract message text
    msg_text = None
    alarm_config = data_elem.find('AlarmConfig')
    if alarm_config is not None:
        text_el = alarm_config.find('.//Text')
        if text_el is not None and text_el.text:
            msg_text = text_el.text.strip()
    result['message'] = msg_text

    return result


def configure_alarm_digital_tag(
    project,
    name: str,
    scope: str = 'controller',
    program_name: Optional[str] = None,
    severity: Optional[int] = None,
    message: Optional[str] = None,
    ack_required: Optional[bool] = None,
    latched: Optional[bool] = None,
) -> None:
    """Update configuration on an existing ALARM_DIGITAL tag.

    Only specified (non-None) parameters are modified.

    Args:
        project: L5XProject instance.
        name: Tag name.
        scope: ``'controller'`` or ``'program'``.
        program_name: Required when *scope* is ``'program'``.
        severity: New severity (1-1000) or None to leave unchanged.
        message: New alarm message or None to leave unchanged.
        ack_required: New ack_required or None to leave unchanged.
        latched: New latched or None to leave unchanged.
    """
    tag_elem = _find_tag_element(project, name, scope, program_name)
    if tag_elem is None:
        raise KeyError(f"Tag '{name}' not found")

    dt = tag_elem.get('DataType', '')
    if dt != 'ALARM_DIGITAL':
        raise ValueError(
            f"Tag '{name}' is DataType '{dt}', expected ALARM_DIGITAL"
        )

    data_elem = tag_elem.find("Data[@Format='Alarm']")
    if data_elem is None:
        raise ValueError(f"Tag '{name}' has no <Data Format=\"Alarm\"> element")

    params_el = data_elem.find('AlarmDigitalParameters')
    if params_el is None:
        raise ValueError(f"Tag '{name}' has no AlarmDigitalParameters")

    if severity is not None:
        if not (ALARM_SEVERITY_MIN <= severity <= ALARM_SEVERITY_MAX):
            raise ValueError(
                f"severity must be {ALARM_SEVERITY_MIN}-{ALARM_SEVERITY_MAX}"
            )
        params_el.set('Severity', str(severity))

    if ack_required is not None:
        params_el.set('AckRequired', str(ack_required).lower())

    if latched is not None:
        params_el.set('Latched', str(latched).lower())

    if message is not None:
        alarm_config = data_elem.find('AlarmConfig')
        if alarm_config is None:
            alarm_config = etree.SubElement(data_elem, 'AlarmConfig')
        # Find or create Messages/Message/Text structure
        messages_el = alarm_config.find('Messages')
        if messages_el is None:
            messages_el = etree.SubElement(alarm_config, 'Messages')
        msg_el = messages_el.find('Message')
        if msg_el is None:
            msg_el = etree.SubElement(messages_el, 'Message')
            msg_el.set('Type', 'AM')
        text_el = msg_el.find('Text')
        if text_el is None:
            text_el = etree.SubElement(msg_el, 'Text')
            text_el.set('Lang', 'en-US')
        text_el.text = etree.CDATA(message)


# ===================================================================
# Alarm Listing
# ===================================================================

def list_alarms(
    project,
    alarm_type: Optional[str] = None,
    scope: Optional[str] = None,
    program_name: Optional[str] = None,
) -> List[dict]:
    """List all alarm tags and alarm conditions in the project.

    Args:
        project: L5XProject instance.
        alarm_type: Filter by ``'digital'``, ``'analog'``, ``'condition'``,
                    or ``None`` for all.
        scope: ``'controller'``, ``'program'``, or ``None`` for all scopes.
        program_name: Required when *scope* is ``'program'``.

    Returns:
        List of dicts with alarm summary info.
    """
    results: List[dict] = []

    def _scan_tags(tags_el: Optional[etree._Element], tag_scope: str,
                   tag_program: Optional[str] = None) -> None:
        if tags_el is None:
            return
        for tag in tags_el.findall('Tag'):
            dt = tag.get('DataType', '')
            tag_name = tag.get('Name', '')

            if dt == 'ALARM_DIGITAL' and alarm_type in (None, 'digital'):
                info: Dict[str, Any] = {
                    'name': tag_name,
                    'alarm_type': 'digital',
                    'scope': tag_scope,
                }
                if tag_program:
                    info['program'] = tag_program
                # Extract severity and message
                data_el = tag.find("Data[@Format='Alarm']")
                if data_el is not None:
                    p = data_el.find('AlarmDigitalParameters')
                    if p is not None:
                        info['severity'] = int(p.get('Severity', '500'))
                    text_el = data_el.find('.//Text')
                    if text_el is not None and text_el.text:
                        info['message'] = text_el.text.strip()
                results.append(info)

            elif dt == 'ALARM_ANALOG' and alarm_type in (None, 'analog'):
                info = {
                    'name': tag_name,
                    'alarm_type': 'analog',
                    'scope': tag_scope,
                }
                if tag_program:
                    info['program'] = tag_program
                data_el = tag.find("Data[@Format='Alarm']")
                if data_el is not None:
                    p = data_el.find('AlarmAnalogParameters')
                    if p is not None:
                        info['severity'] = int(p.get('Severity', '500'))
                results.append(info)

            elif alarm_type in (None, 'condition'):
                ac_el = tag.find('AlarmConditions')
                if ac_el is not None and len(ac_el) > 0:
                    info = {
                        'name': tag_name,
                        'alarm_type': 'condition',
                        'data_type': dt,
                        'scope': tag_scope,
                        'condition_count': len(ac_el.findall('AlarmCondition')),
                    }
                    if tag_program:
                        info['program'] = tag_program
                    results.append(info)

    # Scan controller tags
    if scope in (None, 'controller'):
        ctrl_tags = project.controller.find('Tags')
        _scan_tags(ctrl_tags, 'controller')

    # Scan program tags
    if scope in (None, 'program'):
        programs_el = project.controller.find('Programs')
        if programs_el is not None:
            for prog in programs_el.findall('Program'):
                pname = prog.get('Name', '')
                if program_name and pname != program_name:
                    continue
                _scan_tags(prog.find('Tags'), 'program', pname)

    return results


# ===================================================================
# Tag Alarm Conditions (on tags with DatatypeAlarmDefinitions)
# ===================================================================

def get_tag_alarm_conditions(
    project,
    name: str,
    scope: str = 'controller',
    program_name: Optional[str] = None,
) -> List[dict]:
    """Read alarm conditions on a tag.

    Args:
        project: L5XProject instance.
        name: Tag name.
        scope: ``'controller'`` or ``'program'``.
        program_name: Required when *scope* is ``'program'``.

    Returns:
        List of dicts, one per AlarmCondition.
    """
    tag_elem = _find_tag_element(project, name, scope, program_name)
    if tag_elem is None:
        raise KeyError(f"Tag '{name}' not found")

    ac_container = tag_elem.find('AlarmConditions')
    if ac_container is None:
        return []

    conditions = []
    for ac in ac_container.findall('AlarmCondition'):
        info: Dict[str, Any] = {}
        for attr_name, attr_val in ac.attrib.items():
            if attr_val in ('true', 'false'):
                info[attr_name] = attr_val == 'true'
            else:
                try:
                    info[attr_name] = int(attr_val)
                except ValueError:
                    try:
                        info[attr_name] = float(attr_val)
                    except ValueError:
                        info[attr_name] = attr_val

        # Extract message if present
        alarm_config = ac.find('AlarmConfig')
        msg_text = None
        if alarm_config is not None:
            text_el = alarm_config.find('.//Text')
            if text_el is not None and text_el.text:
                msg_text = text_el.text.strip()
        info['message'] = msg_text
        conditions.append(info)

    return conditions


def configure_tag_alarm_condition(
    project,
    tag_name: str,
    condition_name: str,
    scope: str = 'controller',
    program_name: Optional[str] = None,
    severity: Optional[int] = None,
    on_delay: Optional[int] = None,
    off_delay: Optional[int] = None,
    used: Optional[bool] = None,
    ack_required: Optional[bool] = None,
    message: Optional[str] = None,
) -> None:
    """Update a specific AlarmCondition on a tag.

    Only specified (non-None) parameters are modified.

    Args:
        project: L5XProject instance.
        tag_name: Tag name containing alarm conditions.
        condition_name: Name of the AlarmCondition to modify.
        scope: ``'controller'`` or ``'program'``.
        program_name: Required when *scope* is ``'program'``.
        severity: New severity (1-1000) or None.
        on_delay: On delay in ms or None.
        off_delay: Off delay in ms or None.
        used: Whether the alarm is used or None.
        ack_required: Whether ack is required or None.
        message: Alarm message text or None.
    """
    tag_elem = _find_tag_element(project, tag_name, scope, program_name)
    if tag_elem is None:
        raise KeyError(f"Tag '{tag_name}' not found")

    ac_container = tag_elem.find('AlarmConditions')
    if ac_container is None:
        raise ValueError(f"Tag '{tag_name}' has no AlarmConditions")

    # Find the specific condition
    target_ac = None
    for ac in ac_container.findall('AlarmCondition'):
        if ac.get('Name') == condition_name:
            target_ac = ac
            break
    if target_ac is None:
        raise KeyError(
            f"AlarmCondition '{condition_name}' not found on tag '{tag_name}'"
        )

    if severity is not None:
        if not (ALARM_SEVERITY_MIN <= severity <= ALARM_SEVERITY_MAX):
            raise ValueError(
                f"severity must be {ALARM_SEVERITY_MIN}-{ALARM_SEVERITY_MAX}"
            )
        target_ac.set('Severity', str(severity))

    if on_delay is not None:
        if on_delay < 0:
            raise ValueError("on_delay must be non-negative")
        target_ac.set('OnDelay', str(on_delay))

    if off_delay is not None:
        if off_delay < 0:
            raise ValueError("off_delay must be non-negative")
        target_ac.set('OffDelay', str(off_delay))

    if used is not None:
        target_ac.set('Used', str(used).lower())

    if ack_required is not None:
        target_ac.set('AckRequired', str(ack_required).lower())

    if message is not None:
        alarm_config = target_ac.find('AlarmConfig')
        if alarm_config is None:
            alarm_config = etree.SubElement(target_ac, 'AlarmConfig')
        # Clear existing children if it was an empty <AlarmConfig/>
        messages_el = alarm_config.find('Messages')
        if messages_el is None:
            messages_el = etree.SubElement(alarm_config, 'Messages')
        msg_el = messages_el.find('Message')
        if msg_el is None:
            msg_el = etree.SubElement(messages_el, 'Message')
            msg_el.set('Type', 'CAM')
        text_el = msg_el.find('Text')
        if text_el is None:
            text_el = etree.SubElement(msg_el, 'Text')
            text_el.set('Lang', 'en-US')
        text_el.text = etree.CDATA(message)


# ===================================================================
# L5K Data Stripping
# ===================================================================

def strip_l5k_data(
    project,
    scope: str = '',
    program_name: Optional[str] = None,
) -> int:
    """Remove ``<Data Format="L5K">`` elements from tags.

    Studio 5000 can reconstruct L5K data from the Decorated format during
    import.  Stripping L5K data is useful when it may be out of sync with
    Decorated data, causing 'Data type mismatch' import errors.

    Args:
        project: L5XProject instance.
        scope: ``'controller'``, ``'program'``, or ``''`` for all scopes.
        program_name: Required when *scope* is ``'program'``.

    Returns:
        Number of ``<Data Format="L5K">`` elements removed.
    """
    count = 0
    tag_containers: List[etree._Element] = []

    if scope in ('', 'controller'):
        ctrl_tags = project.controller.find('Tags')
        if ctrl_tags is not None:
            tag_containers.append(ctrl_tags)

    if scope in ('', 'program'):
        programs_el = project.controller.find('Programs')
        if programs_el is not None:
            for prog in programs_el.findall('Program'):
                if program_name and prog.get('Name') != program_name:
                    continue
                prog_tags = prog.find('Tags')
                if prog_tags is not None:
                    tag_containers.append(prog_tags)

    for tags_el in tag_containers:
        for tag in tags_el.findall('Tag'):
            for data_elem in tag.findall('Data'):
                if data_elem.get('Format') == 'L5K':
                    tag.remove(data_elem)
                    count += 1

    # Update ExportOptions to remove L5KData flag so Studio 5000
    # does not expect L5K data that is no longer present.
    if count > 0:
        root = project.root
        export_opts = root.get('ExportOptions', '')
        if 'L5KData' in export_opts:
            new_opts = ' '.join(
                opt for opt in export_opts.split() if opt != 'L5KData'
            )
            root.set('ExportOptions', new_opts)

    return count
