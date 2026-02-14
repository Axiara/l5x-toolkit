"""
Component export operations for L5X files.

Builds standalone L5X export files from scratch or extracts components
from a loaded project into portable export files.  The generated files
conform to Studio 5000's import expectations: UTF-8 BOM, CDATA sections,
correct element ordering, and proper Use="Context"/"Target" annotations.

Supported component types:
  - Rung (one or more rungs from an RLL routine)
  - Routine (complete routine with all rungs/lines)
  - Program (complete program with tags and routines)
  - Tag (controller- or program-scope tag with type dependencies)
  - DataType / UDT (user-defined type with transitive dependencies)
  - AddOnInstructionDefinition / AOI (with UDT and AOI dependencies)
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import List, Optional, Set, Tuple

from lxml import etree

from .schema import (
    BASE_DATA_TYPES,
    BUILTIN_STRUCTURES,
    CONTROLLER_CHILD_ORDER,
    DEFAULT_EXPORT_OPTIONS,
    EXPORT_DATE_FORMAT,
    INSTRUCTION_CATALOG,
)
from .utils import (
    deep_copy,
    find_or_create,
    indent_xml,
    insert_in_order,
    set_element_cdata,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_base_or_builtin_type(data_type: str) -> bool:
    """Return True if *data_type* is a base atomic type or built-in structure."""
    return (
        data_type.upper() in BASE_DATA_TYPES
        or data_type.upper() in {k.upper() for k in BUILTIN_STRUCTURES}
        or data_type.upper() in {"BIT", "STRING"}
    )


def _to_pascal_case(name: str) -> str:
    """Convert a name to PascalCase with no spaces or invalid characters.

    Already-PascalCase names pass through unchanged.  Underscores and
    hyphens are treated as word boundaries.
    """
    # Split on non-alphanumeric boundaries
    parts = re.split(r'[^A-Za-z0-9]+', name)
    result = ''.join(part.capitalize() for part in parts if part)
    return result or 'Export'


def _generate_export_filename(
    base_name: str,
    component_type: str,
    output_dir: str = "",
) -> str:
    """Generate a PascalCase filename per the naming convention.

    Pattern: ``{BaseName}_{ComponentType}.L5X``

    Args:
        base_name: The component name (will be PascalCased).
        component_type: The type suffix (e.g. ``'Rung'``, ``'Routine'``).
        output_dir: Directory for the file.  If empty, uses current dir.

    Returns:
        Absolute file path for the export file.
    """
    safe_name = _to_pascal_case(base_name)
    filename = f"{safe_name}_{component_type}.L5X"
    if output_dir:
        return os.path.join(output_dir, filename)
    return os.path.abspath(filename)


def _build_export_shell(
    target_type: str,
    target_name: str = "",
    controller_name: str = "Exported",
    processor_type: str = "1769-L33ER",
    major_rev: str = "37",
    minor_rev: str = "11",
    software_revision: str = "37.01",
    target_sub_type: str = "",
    target_class: str = "",
    target_count: int = -1,
    export_options: str = DEFAULT_EXPORT_OPTIONS,
) -> etree._Element:
    """Build the RSLogix5000Content root element for an export file.

    Creates the minimal shell structure with ``Controller Use="Context"``
    and empty child containers following CONTROLLER_CHILD_ORDER.

    Returns:
        The root ``RSLogix5000Content`` element.
    """
    now = datetime.now().strftime(EXPORT_DATE_FORMAT)

    # Build root attributes
    root_attrib = {
        'SchemaRevision': '1.0',
        'SoftwareRevision': software_revision,
        'TargetType': target_type,
        'ContainsContext': 'true',
        'ExportDate': now,
        'ExportOptions': export_options,
    }
    if target_name:
        root_attrib['TargetName'] = target_name
    if target_sub_type:
        root_attrib['TargetSubType'] = target_sub_type
    if target_class:
        root_attrib['TargetClass'] = target_class
    if target_count >= 0:
        root_attrib['TargetCount'] = str(target_count)

    root = etree.Element('RSLogix5000Content', attrib=root_attrib)

    # Controller with Use="Context"
    ctrl_attrib = {
        'Use': 'Context',
        'Name': controller_name,
        'ProcessorType': processor_type,
        'MajorRev': major_rev,
        'MinorRev': minor_rev,
    }
    controller = etree.SubElement(root, 'Controller', attrib=ctrl_attrib)

    # Add empty context containers in schema order
    for container_name in ('DataTypes', 'AddOnInstructionDefinitions',
                           'Tags', 'Programs'):
        container = etree.SubElement(controller, container_name)
        container.set('Use', 'Context')

    return root


def _get_export_metadata(project) -> dict:
    """Extract metadata from a loaded project to use in export headers.

    Returns dict with controller_name, processor_type, major_rev,
    minor_rev, software_revision.
    """
    return {
        'controller_name': project.controller_name or 'Exported',
        'processor_type': project.processor_type or '1769-L33ER',
        'major_rev': project.controller.get('MajorRev', '37'),
        'minor_rev': project.controller.get('MinorRev', '11'),
        'software_revision': project.software_revision or '37.01',
    }


# ---------------------------------------------------------------------------
# Dependency collection
# ---------------------------------------------------------------------------

def _collect_tag_type_dependencies(
    project,
    data_type: str,
    visited: Optional[Set[str]] = None,
) -> Tuple[Set[str], Set[str]]:
    """Recursively resolve all UDT and AOI names needed by a data type.

    Args:
        project: The loaded L5XProject.
        data_type: The data type name to resolve.
        visited: Set of already-visited type names (prevents cycles).

    Returns:
        ``(udt_names, aoi_names)`` -- sets of names that must be included.
    """
    if visited is None:
        visited = set()

    udt_names: Set[str] = set()
    aoi_names: Set[str] = set()

    if _is_base_or_builtin_type(data_type):
        return udt_names, aoi_names
    if data_type.upper() in visited:
        return udt_names, aoi_names

    visited.add(data_type.upper())

    # Check if it's a UDT
    dt_el = project.data_types_element
    if dt_el is not None:
        for dt in dt_el.findall('DataType'):
            if dt.get('Name', '').upper() == data_type.upper():
                udt_names.add(dt.get('Name'))
                # Recurse into members
                members = dt.find('Members')
                if members is not None:
                    for member in members.findall('Member'):
                        mdt = member.get('DataType', '')
                        if mdt and not _is_base_or_builtin_type(mdt):
                            sub_udts, sub_aois = _collect_tag_type_dependencies(
                                project, mdt, visited
                            )
                            udt_names |= sub_udts
                            aoi_names |= sub_aois
                return udt_names, aoi_names

    # Check if it's an AOI
    aoi_el = project.aoi_definitions_element
    if aoi_el is not None:
        for aoi in aoi_el.findall('AddOnInstructionDefinition'):
            if aoi.get('Name', '').upper() == data_type.upper():
                aoi_names.add(aoi.get('Name'))
                # AOI parameters and local tags may reference UDTs
                for param in aoi.iter('Parameter'):
                    pdt = param.get('DataType', '')
                    if pdt and not _is_base_or_builtin_type(pdt):
                        sub_udts, sub_aois = _collect_tag_type_dependencies(
                            project, pdt, visited
                        )
                        udt_names |= sub_udts
                        aoi_names |= sub_aois
                for ltag in aoi.iter('LocalTag'):
                    ldt = ltag.get('DataType', '')
                    if ldt and not _is_base_or_builtin_type(ldt):
                        sub_udts, sub_aois = _collect_tag_type_dependencies(
                            project, ldt, visited
                        )
                        udt_names |= sub_udts
                        aoi_names |= sub_aois
                return udt_names, aoi_names

    return udt_names, aoi_names


def _extract_aoi_names_from_rung_text(rung_text: str) -> Set[str]:
    """Scan rung instruction text for non-standard instruction names (potential AOIs).

    Returns set of instruction names not in the standard catalog.
    """
    from .rungs import tokenize, TokenType

    aoi_refs: Set[str] = set()
    standard = {k.upper() for k in INSTRUCTION_CATALOG}

    try:
        tokens = tokenize(rung_text)
        for token in tokens:
            if token.type == TokenType.INSTRUCTION:
                if token.value.upper() not in standard:
                    aoi_refs.add(token.value)
    except Exception:
        pass

    return aoi_refs


def _collect_alarm_defs_for_tag(
    project,
    tag_el: etree._Element,
) -> Set[str]:
    """Return DatatypeAlarmDefinition names required by a tag's AlarmConditions.

    AlarmConditions on a tag may originate from alarm definitions on the
    tag's own data type *or* on nested member types (e.g. a ``Debounce``
    member inside an AOI).  This helper matches each AlarmCondition back
    to the correct DatatypeAlarmDefinition by name.
    """
    ac_container = tag_el.find('AlarmConditions')
    if ac_container is None:
        return set()
    cond_def_names = {
        ac.get('AlarmConditionDefinition', ac.get('Name', ''))
        for ac in ac_container.findall('AlarmCondition')
    }
    cond_def_names.discard('')
    if not cond_def_names:
        return set()

    result: Set[str] = set()
    alarm_defs_el = project.alarm_definitions_element
    if alarm_defs_el is None:
        return result
    for dtad in alarm_defs_el.findall('DatatypeAlarmDefinition'):
        for mad in dtad.findall('MemberAlarmDefinition'):
            if mad.get('Name') in cond_def_names:
                result.add(dtad.get('Name'))
                break
    return result


def _collect_rung_dependencies(
    project,
    program_name: str,
    rung_elements: List[etree._Element],
) -> dict:
    """Analyze rungs to determine all referenced tags, UDTs, and AOIs.

    Returns dict with keys:
        - ``controller_tag_names``: set of controller-scope tag names
        - ``program_tag_names``: set of program-scope tag names
        - ``udt_names``: set of UDT names (with transitive deps)
        - ``aoi_names``: set of AOI names (with transitive deps)
    """
    from .rungs import extract_tag_references

    all_tag_refs: Set[str] = set()
    all_aoi_refs: Set[str] = set()

    for rung in rung_elements:
        text_el = rung.find('Text')
        if text_el is None or not text_el.text:
            continue
        rung_text = text_el.text.strip()
        all_tag_refs |= extract_tag_references(rung_text)
        all_aoi_refs |= _extract_aoi_names_from_rung_text(rung_text)

    # Resolve tag references to controller/program scope
    controller_tag_names: Set[str] = set()
    program_tag_names: Set[str] = set()
    udt_names: Set[str] = set()
    aoi_names: Set[str] = set(all_aoi_refs)

    # Check program-scope tags first
    try:
        prog_el = project.get_program_element(program_name)
        prog_tags_el = prog_el.find('Tags')
        if prog_tags_el is not None:
            prog_tag_map = {
                t.get('Name', '').upper(): t
                for t in prog_tags_el.findall('Tag')
            }
        else:
            prog_tag_map = {}
    except KeyError:
        prog_tag_map = {}

    # Check controller-scope tags
    ctrl_tags_el = project.controller_tags_element
    ctrl_tag_map = {}
    if ctrl_tags_el is not None:
        ctrl_tag_map = {
            t.get('Name', '').upper(): t
            for t in ctrl_tags_el.findall('Tag')
        }

    alarm_def_names: Set[str] = set()

    for ref in all_tag_refs:
        ref_upper = ref.upper()
        if ref_upper in prog_tag_map:
            tag_el = prog_tag_map[ref_upper]
            program_tag_names.add(tag_el.get('Name'))
            dt = tag_el.get('DataType', '')
            if dt and not _is_base_or_builtin_type(dt):
                sub_udts, sub_aois = _collect_tag_type_dependencies(project, dt)
                udt_names |= sub_udts
                aoi_names |= sub_aois
            alarm_def_names |= _collect_alarm_defs_for_tag(project, tag_el)
        if ref_upper in ctrl_tag_map:
            tag_el = ctrl_tag_map[ref_upper]
            controller_tag_names.add(tag_el.get('Name'))
            dt = tag_el.get('DataType', '')
            if dt and not _is_base_or_builtin_type(dt):
                sub_udts, sub_aois = _collect_tag_type_dependencies(project, dt)
                udt_names |= sub_udts
                aoi_names |= sub_aois
            alarm_def_names |= _collect_alarm_defs_for_tag(project, tag_el)

    # Resolve AOI dependencies (they may reference UDTs and other AOIs)
    visited_aois: Set[str] = set()
    aoi_queue = list(aoi_names)
    while aoi_queue:
        aoi_name = aoi_queue.pop(0)
        if aoi_name.upper() in visited_aois:
            continue
        visited_aois.add(aoi_name.upper())
        aoi_names.add(aoi_name)
        sub_udts, sub_aois = _collect_tag_type_dependencies(project, aoi_name)
        udt_names |= sub_udts
        for sa in sub_aois:
            if sa.upper() not in visited_aois:
                aoi_queue.append(sa)

    return {
        'controller_tag_names': controller_tag_names,
        'program_tag_names': program_tag_names,
        'udt_names': udt_names,
        'aoi_names': aoi_names,
        'alarm_def_names': alarm_def_names,
    }


def _add_context_dependencies(
    project,
    export_controller: etree._Element,
    udt_names: Set[str],
    aoi_names: Set[str],
    alarm_def_names: Optional[Set[str]] = None,
) -> None:
    """Deep-copy UDT, AOI, and AlarmDefinition elements into the export shell."""
    # Add UDTs
    if udt_names:
        dt_container = export_controller.find('DataTypes')
        if dt_container is None:
            dt_container = etree.SubElement(export_controller, 'DataTypes')
            dt_container.set('Use', 'Context')
        src_dt = project.data_types_element
        if src_dt is not None:
            for dt in src_dt.findall('DataType'):
                if dt.get('Name') in udt_names:
                    cloned = deep_copy(dt)
                    dt_container.append(cloned)

    # Add AOIs
    if aoi_names:
        aoi_container = export_controller.find('AddOnInstructionDefinitions')
        if aoi_container is None:
            aoi_container = etree.SubElement(
                export_controller, 'AddOnInstructionDefinitions'
            )
            aoi_container.set('Use', 'Context')
        src_aoi = project.aoi_definitions_element
        if src_aoi is not None:
            for aoi in src_aoi.findall('AddOnInstructionDefinition'):
                if aoi.get('Name') in aoi_names:
                    cloned = deep_copy(aoi)
                    aoi_container.append(cloned)

    # Add DatatypeAlarmDefinitions
    if alarm_def_names:
        src_alarm_defs = project.alarm_definitions_element
        if src_alarm_defs is not None:
            alarm_container = export_controller.find('AlarmDefinitions')
            if alarm_container is None:
                alarm_container = etree.Element('AlarmDefinitions')
                alarm_container.set('Use', 'Context')
                # Insert after AddOnInstructionDefinitions, before Tags
                # per CONTROLLER_CHILD_ORDER
                aoi_el = export_controller.find(
                    'AddOnInstructionDefinitions'
                )
                if aoi_el is not None:
                    idx = list(export_controller).index(aoi_el) + 1
                else:
                    tags_el = export_controller.find('Tags')
                    if tags_el is not None:
                        idx = list(export_controller).index(tags_el)
                    else:
                        idx = len(export_controller)
                export_controller.insert(idx, alarm_container)
            for dtad in src_alarm_defs.findall(
                'DatatypeAlarmDefinition'
            ):
                if dtad.get('Name') in alarm_def_names:
                    cloned = deep_copy(dtad)
                    alarm_container.append(cloned)


def _add_tag_elements(
    project,
    export_controller: etree._Element,
    controller_tag_names: Set[str],
) -> None:
    """Deep-copy controller-scope tags into the export shell."""
    if not controller_tag_names:
        return
    tags_container = export_controller.find('Tags')
    if tags_container is None:
        tags_container = etree.SubElement(export_controller, 'Tags')
        tags_container.set('Use', 'Context')
    ctrl_tags = project.controller_tags_element
    if ctrl_tags is None:
        return
    for tag in ctrl_tags.findall('Tag'):
        if tag.get('Name') in controller_tag_names:
            cloned = deep_copy(tag)
            tags_container.append(cloned)


def _save_export(
    root: etree._Element,
    file_path: str,
) -> str:
    """Write an export XML tree to disk using L5XProject.write().

    Returns the absolute file path.
    """
    from .project import L5XProject
    prj = L5XProject.from_element(root)
    indent_xml(root)
    abs_path = os.path.abspath(file_path)
    prj.write(abs_path)
    return abs_path


# ---------------------------------------------------------------------------
# Create-from-scratch functions
# ---------------------------------------------------------------------------

def create_rung_export(
    project=None,
    program_name: str = "ExportedProgram",
    routine_name: str = "MainRoutine",
) -> 'L5XProject':
    """Create an empty Rung export file in memory.

    Returns an L5XProject instance backed by a minimal Rung-type L5X
    structure.  The caller can then use existing tools (``add_rung``,
    ``create_tag``, etc.) to populate it, then ``save_project`` to write.

    Args:
        project: Optional source project to inherit metadata from.
        program_name: Name for the context program.
        routine_name: Name for the context routine.

    Returns:
        A new :class:`L5XProject` instance with ``TargetType='Rung'``.
    """
    from .project import L5XProject

    meta = _get_export_metadata(project) if project else {}
    root = _build_export_shell(
        target_type='Rung',
        target_count=0,
        controller_name=meta.get('controller_name', 'Exported'),
        processor_type=meta.get('processor_type', '1769-L33ER'),
        major_rev=meta.get('major_rev', '37'),
        minor_rev=meta.get('minor_rev', '11'),
        software_revision=meta.get('software_revision', '37.01'),
    )

    controller = root.find('Controller')
    programs = controller.find('Programs')

    program = etree.SubElement(programs, 'Program')
    program.set('Use', 'Context')
    program.set('Name', program_name)

    routines = etree.SubElement(program, 'Routines')
    routines.set('Use', 'Context')

    routine = etree.SubElement(routines, 'Routine')
    routine.set('Name', routine_name)
    routine.set('Type', 'RLL')

    etree.SubElement(routine, 'RLLContent')

    return L5XProject.from_element(root)


def create_routine_export(
    project=None,
    program_name: str = "ExportedProgram",
    routine_name: str = "MainRoutine",
    routine_type: str = "RLL",
) -> 'L5XProject':
    """Create an empty Routine export file in memory.

    Args:
        project: Optional source project to inherit metadata from.
        program_name: Name for the context program.
        routine_name: Name for the target routine.
        routine_type: Routine type (``'RLL'``, ``'ST'``, ``'FBD'``, ``'SFC'``).

    Returns:
        A new :class:`L5XProject` instance with ``TargetType='Routine'``.
    """
    from .project import L5XProject

    meta = _get_export_metadata(project) if project else {}
    root = _build_export_shell(
        target_type='Routine',
        target_name=routine_name,
        target_sub_type=routine_type,
        target_class='Standard',
        controller_name=meta.get('controller_name', 'Exported'),
        processor_type=meta.get('processor_type', '1769-L33ER'),
        major_rev=meta.get('major_rev', '37'),
        minor_rev=meta.get('minor_rev', '11'),
        software_revision=meta.get('software_revision', '37.01'),
    )

    controller = root.find('Controller')
    programs = controller.find('Programs')

    program = etree.SubElement(programs, 'Program')
    program.set('Use', 'Context')
    program.set('Name', program_name)

    tags_el = etree.SubElement(program, 'Tags')
    tags_el.set('Use', 'Context')

    routines = etree.SubElement(program, 'Routines')
    routines.set('Use', 'Context')

    routine = etree.SubElement(routines, 'Routine')
    routine.set('Use', 'Target')
    routine.set('Name', routine_name)
    routine.set('Type', routine_type)

    # Add appropriate content element
    content_map = {
        'RLL': 'RLLContent',
        'ST': 'STContent',
        'FBD': 'FBDContent',
        'SFC': 'SFCContent',
    }
    content_tag = content_map.get(routine_type.upper(), 'RLLContent')
    etree.SubElement(routine, content_tag)

    return L5XProject.from_element(root)


def create_program_export(
    project=None,
    program_name: str = "ExportedProgram",
) -> 'L5XProject':
    """Create an empty Program export file in memory.

    The program includes an empty MainRoutine (RLL type).

    Args:
        project: Optional source project to inherit metadata from.
        program_name: Name for the target program.

    Returns:
        A new :class:`L5XProject` instance with ``TargetType='Program'``.
    """
    from .project import L5XProject

    meta = _get_export_metadata(project) if project else {}
    root = _build_export_shell(
        target_type='Program',
        target_name=program_name,
        target_class='Standard',
        controller_name=meta.get('controller_name', 'Exported'),
        processor_type=meta.get('processor_type', '1769-L33ER'),
        major_rev=meta.get('major_rev', '37'),
        minor_rev=meta.get('minor_rev', '11'),
        software_revision=meta.get('software_revision', '37.01'),
    )

    controller = root.find('Controller')
    programs = controller.find('Programs')

    program = etree.SubElement(programs, 'Program')
    program.set('Use', 'Target')
    program.set('Name', program_name)
    program.set('TestEdits', 'false')
    program.set('MainRoutineName', 'MainRoutine')
    program.set('Disabled', 'false')

    etree.SubElement(program, 'Tags')

    routines = etree.SubElement(program, 'Routines')

    routine = etree.SubElement(routines, 'Routine')
    routine.set('Name', 'MainRoutine')
    routine.set('Type', 'RLL')
    etree.SubElement(routine, 'RLLContent')

    return L5XProject.from_element(root)


def create_udt_export(
    project=None,
    udt_name: str = "ExportedType",
) -> 'L5XProject':
    """Create an empty DataType (UDT) export file in memory.

    The UDT is created with no members -- the caller must add them.

    Args:
        project: Optional source project to inherit metadata from.
        udt_name: Name for the target UDT.

    Returns:
        A new :class:`L5XProject` with ``TargetType='DataType'``.
    """
    from .project import L5XProject

    meta = _get_export_metadata(project) if project else {}
    root = _build_export_shell(
        target_type='DataType',
        target_name=udt_name,
        controller_name=meta.get('controller_name', 'Exported'),
        processor_type=meta.get('processor_type', '1769-L33ER'),
        major_rev=meta.get('major_rev', '37'),
        minor_rev=meta.get('minor_rev', '11'),
        software_revision=meta.get('software_revision', '37.01'),
    )

    controller = root.find('Controller')
    dt_container = controller.find('DataTypes')

    udt = etree.SubElement(dt_container, 'DataType')
    udt.set('Use', 'Target')
    udt.set('Name', udt_name)
    udt.set('Family', 'NoFamily')
    udt.set('Class', 'User')
    etree.SubElement(udt, 'Members')

    return L5XProject.from_element(root)


def create_aoi_export(
    project=None,
    aoi_name: str = "ExportedAOI",
    revision: str = "1.0",
) -> 'L5XProject':
    """Create an empty AddOnInstructionDefinition export file in memory.

    The AOI is created with EnableIn/EnableOut parameters and an empty
    Logic routine.

    Args:
        project: Optional source project to inherit metadata from.
        aoi_name: Name for the target AOI.
        revision: Revision string (e.g. ``'1.0'``).

    Returns:
        A new :class:`L5XProject` with
        ``TargetType='AddOnInstructionDefinition'``.
    """
    from .project import L5XProject

    now_utc = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"

    meta = _get_export_metadata(project) if project else {}
    sw_rev = meta.get('software_revision', '37.01')
    root = _build_export_shell(
        target_type='AddOnInstructionDefinition',
        target_name=aoi_name,
        target_class='Standard',
        controller_name=meta.get('controller_name', 'Exported'),
        processor_type=meta.get('processor_type', '1769-L33ER'),
        major_rev=meta.get('major_rev', '37'),
        minor_rev=meta.get('minor_rev', '11'),
        software_revision=sw_rev,
    )

    controller = root.find('Controller')
    aoi_container = controller.find('AddOnInstructionDefinitions')

    aoi_def = etree.SubElement(aoi_container, 'AddOnInstructionDefinition')
    aoi_def.set('Use', 'Target')
    aoi_def.set('Name', aoi_name)
    aoi_def.set('Revision', revision)
    aoi_def.set('ExecutePrescan', 'false')
    aoi_def.set('ExecutePostscan', 'false')
    aoi_def.set('ExecuteEnableInFalse', 'false')
    aoi_def.set('CreatedDate', now_utc)
    aoi_def.set('EditedDate', now_utc)
    aoi_def.set('SoftwareRevision', f'v{sw_rev}')

    # Standard EnableIn / EnableOut parameters
    params = etree.SubElement(aoi_def, 'Parameters')
    enable_in = etree.SubElement(params, 'Parameter')
    enable_in.set('Name', 'EnableIn')
    enable_in.set('DataType', 'BOOL')
    enable_in.set('Usage', 'Input')
    enable_in.set('Required', 'false')
    enable_in.set('Visible', 'false')

    enable_out = etree.SubElement(params, 'Parameter')
    enable_out.set('Name', 'EnableOut')
    enable_out.set('DataType', 'BOOL')
    enable_out.set('Usage', 'Output')
    enable_out.set('Required', 'false')
    enable_out.set('Visible', 'false')

    etree.SubElement(aoi_def, 'LocalTags')

    routines = etree.SubElement(aoi_def, 'Routines')
    logic = etree.SubElement(routines, 'Routine')
    logic.set('Name', 'Logic')
    logic.set('Type', 'RLL')
    rll = etree.SubElement(logic, 'RLLContent')
    rung = etree.SubElement(rll, 'Rung')
    rung.set('Number', '0')
    rung.set('Type', 'N')
    text_el = etree.SubElement(rung, 'Text')
    set_element_cdata(text_el, 'NOP();')

    return L5XProject.from_element(root)


# ---------------------------------------------------------------------------
# Extract-from-project functions
# ---------------------------------------------------------------------------

def export_rung(
    project,
    program_name: str,
    routine_name: str,
    rung_numbers: List[int],
    file_path: str = "",
    include_tags: bool = True,
) -> str:
    """Extract specific rungs from a routine into a standalone Rung export file.

    Includes the specified rungs (deep-copied) along with referenced tags,
    UDT definitions, and AOI definitions.

    Args:
        project: The loaded L5XProject.
        program_name: Program containing the routine.
        routine_name: Name of the RLL routine.
        rung_numbers: List of zero-based rung indices to export.
        file_path: Output file path.  If empty, auto-generates.
        include_tags: Whether to include referenced tags and dependencies.

    Returns:
        The absolute path of the saved file.

    Raises:
        KeyError: If the program, routine, or rung does not exist.
        ValueError: If the routine is not RLL.
    """
    meta = _get_export_metadata(project)

    # Get the source rung elements
    routine = project.get_routine_element(program_name, routine_name)
    rll_content = routine.find('RLLContent')
    if rll_content is None:
        raise ValueError(
            f"Routine '{routine_name}' has no RLLContent."
        )

    rung_elements = []
    all_rungs = rll_content.findall('Rung')
    for num in rung_numbers:
        found = False
        for rung in all_rungs:
            if int(rung.get('Number', '-1')) == num:
                rung_elements.append(rung)
                found = True
                break
        if not found:
            raise KeyError(
                f"Rung {num} not found in routine '{routine_name}'."
            )

    # Build export shell
    root = _build_export_shell(
        target_type='Rung',
        target_count=len(rung_elements),
        **meta,
    )
    controller = root.find('Controller')

    # Collect dependencies
    deps = {'controller_tag_names': set(), 'program_tag_names': set(),
            'udt_names': set(), 'aoi_names': set(),
            'alarm_def_names': set()}
    if include_tags:
        deps = _collect_rung_dependencies(project, program_name, rung_elements)

    # Add dependencies to export
    _add_context_dependencies(
        project, controller,
        deps['udt_names'], deps['aoi_names'],
        deps.get('alarm_def_names', set()),
    )
    _add_tag_elements(project, controller, deps['controller_tag_names'])

    # Build program/routine structure with rungs
    programs = controller.find('Programs')
    program = etree.SubElement(programs, 'Program')
    program.set('Use', 'Context')
    program.set('Name', program_name)

    # Add program-scope tags if any
    if deps['program_tag_names']:
        prog_tags = etree.SubElement(program, 'Tags')
        prog_tags.set('Use', 'Context')
        try:
            src_prog = project.get_program_element(program_name)
            src_tags = src_prog.find('Tags')
            if src_tags is not None:
                for tag in src_tags.findall('Tag'):
                    if tag.get('Name') in deps['program_tag_names']:
                        prog_tags.append(deep_copy(tag))
        except KeyError:
            pass

    routines = etree.SubElement(program, 'Routines')
    routines.set('Use', 'Context')
    export_routine = etree.SubElement(routines, 'Routine')
    export_routine.set('Name', routine_name)
    export_routine.set('Type', 'RLL')
    export_rll = etree.SubElement(export_routine, 'RLLContent')

    for i, rung in enumerate(rung_elements):
        cloned = deep_copy(rung)
        cloned.set('Number', str(i))
        export_rll.append(cloned)

    # Generate filename
    if not file_path:
        base = f"{routine_name}Rungs"
        file_path = _generate_export_filename(base, 'Rung')

    return _save_export(root, file_path)


def export_routine(
    project,
    program_name: str,
    routine_name: str,
    file_path: str = "",
    include_tags: bool = True,
) -> str:
    """Extract an entire routine into a standalone Routine export file.

    Args:
        project: The loaded L5XProject.
        program_name: Program containing the routine.
        routine_name: Name of the routine.
        file_path: Output file path.  If empty, auto-generates.
        include_tags: Whether to include referenced tags.

    Returns:
        The absolute path of the saved file.

    Raises:
        KeyError: If the program or routine does not exist.
    """
    meta = _get_export_metadata(project)
    routine = project.get_routine_element(program_name, routine_name)
    routine_type = project._infer_routine_type(routine)

    root = _build_export_shell(
        target_type='Routine',
        target_name=routine_name,
        target_sub_type=routine_type,
        target_class='Standard',
        **meta,
    )
    controller = root.find('Controller')

    # Collect dependencies from all rungs in the routine
    deps = {'controller_tag_names': set(), 'program_tag_names': set(),
            'udt_names': set(), 'aoi_names': set(),
            'alarm_def_names': set()}
    if include_tags and routine_type == 'RLL':
        rll = routine.find('RLLContent')
        if rll is not None:
            rung_elements = rll.findall('Rung')
            deps = _collect_rung_dependencies(
                project, program_name, rung_elements
            )

    _add_context_dependencies(
        project, controller, deps['udt_names'], deps['aoi_names'],
        deps.get('alarm_def_names', set()),
    )
    _add_tag_elements(project, controller, deps['controller_tag_names'])

    # Build program/routine structure
    programs = controller.find('Programs')
    program = etree.SubElement(programs, 'Program')
    program.set('Use', 'Context')
    program.set('Name', program_name)

    if deps['program_tag_names']:
        prog_tags = etree.SubElement(program, 'Tags')
        prog_tags.set('Use', 'Context')
        try:
            src_prog = project.get_program_element(program_name)
            src_tags = src_prog.find('Tags')
            if src_tags is not None:
                for tag in src_tags.findall('Tag'):
                    if tag.get('Name') in deps['program_tag_names']:
                        prog_tags.append(deep_copy(tag))
        except KeyError:
            pass

    routines_el = etree.SubElement(program, 'Routines')
    routines_el.set('Use', 'Context')

    cloned_routine = deep_copy(routine)
    cloned_routine.set('Use', 'Target')
    routines_el.append(cloned_routine)

    if not file_path:
        file_path = _generate_export_filename(routine_name, 'Routine')

    return _save_export(root, file_path)


def export_program(
    project,
    program_name: str,
    file_path: str = "",
) -> str:
    """Extract an entire program into a standalone Program export file.

    Includes all program tags, routines, and referenced controller-scope
    tags, UDTs, and AOIs.

    Args:
        project: The loaded L5XProject.
        program_name: Name of the program to export.
        file_path: Output file path.  If empty, auto-generates.

    Returns:
        The absolute path of the saved file.

    Raises:
        KeyError: If the program does not exist.
    """
    meta = _get_export_metadata(project)
    program_el = project.get_program_element(program_name)
    program_class = program_el.get('Class', 'Standard')

    root = _build_export_shell(
        target_type='Program',
        target_name=program_name,
        target_class=program_class,
        **meta,
    )
    controller = root.find('Controller')

    # Collect all rung dependencies across all routines in the program
    all_udt_names: Set[str] = set()
    all_aoi_names: Set[str] = set()
    all_ctrl_tag_names: Set[str] = set()
    all_alarm_def_names: Set[str] = set()

    routines_container = program_el.find('Routines')
    if routines_container is not None:
        for routine in routines_container.findall('Routine'):
            rtype = project._infer_routine_type(routine)
            if rtype == 'RLL':
                rll = routine.find('RLLContent')
                if rll is not None:
                    rungs = rll.findall('Rung')
                    deps = _collect_rung_dependencies(
                        project, program_name, rungs
                    )
                    all_udt_names |= deps['udt_names']
                    all_aoi_names |= deps['aoi_names']
                    all_ctrl_tag_names |= deps['controller_tag_names']
                    all_alarm_def_names |= deps.get(
                        'alarm_def_names', set()
                    )

    # Also collect type dependencies from program-scope tags
    prog_tags_el = program_el.find('Tags')
    if prog_tags_el is not None:
        for tag in prog_tags_el.findall('Tag'):
            dt = tag.get('DataType', '')
            if dt and not _is_base_or_builtin_type(dt):
                sub_udts, sub_aois = _collect_tag_type_dependencies(project, dt)
                all_udt_names |= sub_udts
                all_aoi_names |= sub_aois
            all_alarm_def_names |= _collect_alarm_defs_for_tag(
                project, tag
            )

    _add_context_dependencies(
        project, controller, all_udt_names, all_aoi_names,
        all_alarm_def_names,
    )
    _add_tag_elements(project, controller, all_ctrl_tag_names)

    # Deep-copy the entire program element
    programs = controller.find('Programs')
    cloned_program = deep_copy(program_el)
    cloned_program.set('Use', 'Target')
    programs.append(cloned_program)

    if not file_path:
        file_path = _generate_export_filename(program_name, 'Program')

    return _save_export(root, file_path)


def export_tag(
    project,
    tag_name: str,
    scope: str = "controller",
    program_name: str = "",
    file_path: str = "",
) -> str:
    """Extract a tag into an export file.

    Tags are exported in a Rung-type shell (L5X has no standalone Tag
    export type).  The tag's data type dependencies (UDTs, AOIs) are
    included.

    Args:
        project: The loaded L5XProject.
        tag_name: Name of the tag.
        scope: ``'controller'`` or ``'program'``.
        program_name: Required when scope is ``'program'``.
        file_path: Output file path.  If empty, auto-generates.

    Returns:
        The absolute path of the saved file.

    Raises:
        KeyError: If the tag does not exist.
    """
    meta = _get_export_metadata(project)
    tag_el = project.get_tag_element(tag_name, scope, program_name or None)

    root = _build_export_shell(
        target_type='Rung',
        target_count=0,
        **meta,
    )
    controller = root.find('Controller')

    # Collect type dependencies
    dt = tag_el.get('DataType', '')
    udt_names: Set[str] = set()
    aoi_names: Set[str] = set()
    alarm_def_names: Set[str] = set()
    if dt and not _is_base_or_builtin_type(dt):
        udt_names, aoi_names = _collect_tag_type_dependencies(project, dt)
    alarm_def_names = _collect_alarm_defs_for_tag(project, tag_el)

    _add_context_dependencies(
        project, controller, udt_names, aoi_names, alarm_def_names,
    )

    # Add the tag to appropriate container
    cloned_tag = deep_copy(tag_el)
    if scope == 'controller':
        tags_container = controller.find('Tags')
        tags_container.append(cloned_tag)
    else:
        programs = controller.find('Programs')
        program = etree.SubElement(programs, 'Program')
        program.set('Use', 'Context')
        program.set('Name', program_name)
        prog_tags = etree.SubElement(program, 'Tags')
        prog_tags.set('Use', 'Context')
        prog_tags.append(cloned_tag)

    if not file_path:
        file_path = _generate_export_filename(tag_name, 'Tag')

    return _save_export(root, file_path)


def export_udt(
    project,
    udt_name: str,
    file_path: str = "",
) -> str:
    """Extract a UDT definition into a standalone DataType export file.

    Includes transitive UDT dependencies.

    Args:
        project: The loaded L5XProject.
        udt_name: Name of the UDT to export.
        file_path: Output file path.  If empty, auto-generates.

    Returns:
        The absolute path of the saved file.

    Raises:
        KeyError: If the UDT does not exist.
    """
    meta = _get_export_metadata(project)
    udt_el = project.get_data_type_element(udt_name)

    root = _build_export_shell(
        target_type='DataType',
        target_name=udt_name,
        **meta,
    )
    controller = root.find('Controller')

    # Collect transitive dependencies
    dep_udts, _ = _collect_tag_type_dependencies(project, udt_name)
    dep_udts.discard(udt_name)  # Don't duplicate the target

    # Add dependency UDTs first
    dt_container = controller.find('DataTypes')
    src_dt = project.data_types_element
    if src_dt is not None and dep_udts:
        for dt in src_dt.findall('DataType'):
            if dt.get('Name') in dep_udts:
                dt_container.append(deep_copy(dt))

    # Add the target UDT
    cloned = deep_copy(udt_el)
    cloned.set('Use', 'Target')
    dt_container.append(cloned)

    if not file_path:
        file_path = _generate_export_filename(udt_name, 'DataType')

    return _save_export(root, file_path)


def export_aoi(
    project,
    aoi_name: str,
    file_path: str = "",
) -> str:
    """Extract an AOI definition into a standalone AOI export file.

    Includes dependent UDTs and nested AOIs.  Updates the ``EditedDate``
    on the exported AOI.

    Args:
        project: The loaded L5XProject.
        aoi_name: Name of the AOI to export.
        file_path: Output file path.  If empty, auto-generates.

    Returns:
        The absolute path of the saved file.

    Raises:
        KeyError: If the AOI does not exist.
    """
    from .aoi import _update_edited_date

    meta = _get_export_metadata(project)
    aoi_el = project.get_aoi_element(aoi_name)
    aoi_class = aoi_el.get('Class', 'Standard')
    aoi_rev = aoi_el.get('Revision', '1.0')
    now_utc = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"

    root = _build_export_shell(
        target_type='AddOnInstructionDefinition',
        target_name=aoi_name,
        target_class=aoi_class,
        **meta,
    )
    # Set AOI-specific attributes on root
    root.set('TargetRevision', aoi_rev)
    root.set('TargetLastEdited', now_utc)

    controller = root.find('Controller')

    # Collect all dependencies
    udt_names, aoi_dep_names = _collect_tag_type_dependencies(
        project, aoi_name
    )
    aoi_dep_names.discard(aoi_name)  # Don't duplicate the target

    # Also scan rung text for AOI calls
    for text_el in aoi_el.iter('Text'):
        if text_el.text:
            refs = _extract_aoi_names_from_rung_text(text_el.text.strip())
            for ref in refs:
                if ref.upper() != aoi_name.upper():
                    aoi_dep_names.add(ref)
                    sub_udts, sub_aois = _collect_tag_type_dependencies(
                        project, ref
                    )
                    udt_names |= sub_udts
                    aoi_dep_names |= sub_aois
    aoi_dep_names.discard(aoi_name)

    # Add dependency UDTs
    if udt_names:
        dt_container = controller.find('DataTypes')
        src_dt = project.data_types_element
        if src_dt is not None:
            for dt in src_dt.findall('DataType'):
                if dt.get('Name') in udt_names:
                    dt_container.append(deep_copy(dt))

    # Add dependency AOIs
    aoi_container = controller.find('AddOnInstructionDefinitions')
    if aoi_dep_names:
        src_aoi = project.aoi_definitions_element
        if src_aoi is not None:
            for aoi in src_aoi.findall('AddOnInstructionDefinition'):
                if aoi.get('Name') in aoi_dep_names:
                    cloned = deep_copy(aoi)
                    _update_edited_date(cloned)
                    aoi_container.append(cloned)

    # Add the target AOI
    cloned_aoi = deep_copy(aoi_el)
    cloned_aoi.set('Use', 'Target')
    _update_edited_date(cloned_aoi)
    aoi_container.append(cloned_aoi)

    if not file_path:
        file_path = _generate_export_filename(aoi_name, 'AddOnInstruction')

    return _save_export(root, file_path)
