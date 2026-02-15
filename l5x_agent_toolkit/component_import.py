"""
Component import operations for L5X files.

Imports standalone L5X component export files back into a loaded project
with comprehensive conflict detection and resolution.  Supports importing
Rung, Routine, Program, DataType, and AddOnInstructionDefinition exports.

Conflict types detected:
  - ``definition_mismatch``: A UDT/AOI/tag exists in the project but
    differs structurally from the import file version.
  - ``name_exists``: A program, routine, or tag with the same name
    already exists in the target scope.

Conflict resolution strategies:
  - ``report``: Dry-run -- returns conflicts without making changes.
  - ``skip``: Imports non-conflicting items, skips conflicts.
  - ``overwrite``: Replaces existing items with imported versions.
  - ``fail``: Aborts on the first conflict detected.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from lxml import etree

from .aoi import _update_edited_date
from .schema import (
    BASE_DATA_TYPES,
    BUILTIN_STRUCTURES,
    CONTROLLER_CHILD_ORDER,
)
from .utils import (
    deep_copy,
    find_or_create,
    insert_in_order,
    parse_l5x,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ImportConflict:
    """A single conflict detected during import analysis.

    Attributes:
        category: Component category (``'udt'``, ``'aoi'``, ``'tag'``,
            ``'program'``, ``'routine'``).
        name: Name of the conflicting element.
        conflict_type: ``'definition_mismatch'`` or ``'name_exists'``.
        description: Human-readable explanation.
        source_detail: What the import file has.
        target_detail: What the project has.
    """
    category: str
    name: str
    conflict_type: str
    description: str
    source_detail: str
    target_detail: str

    def to_dict(self) -> dict:
        """Convert to a plain dictionary for JSON serialization."""
        return {
            'category': self.category,
            'name': self.name,
            'conflict_type': self.conflict_type,
            'description': self.description,
            'source_detail': self.source_detail,
            'target_detail': self.target_detail,
        }


@dataclass
class ImportResult:
    """Result of an import or import-analysis operation.

    Attributes:
        success: Whether the operation completed without fatal errors.
        conflicts: List of detected conflicts.
        imported: Counts of successfully imported items by category.
        skipped: Names of items that were skipped due to conflicts.
    """
    success: bool = True
    conflicts: List[ImportConflict] = field(default_factory=list)
    imported: Dict[str, int] = field(default_factory=lambda: {
        'rungs': 0, 'tags': 0, 'udts': 0, 'aois': 0,
        'routines': 0, 'programs': 0,
    })
    skipped: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to a plain dictionary for JSON serialization."""
        return {
            'success': self.success,
            'conflict_count': len(self.conflicts),
            'conflicts': [c.to_dict() for c in self.conflicts],
            'imported': self.imported,
            'skipped': self.skipped,
        }


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------

def _is_base_or_builtin_type(data_type: str) -> bool:
    """Return True if the data type is a base atomic type or built-in."""
    return (
        data_type.upper() in BASE_DATA_TYPES
        or data_type.upper() in {k.upper() for k in BUILTIN_STRUCTURES}
        or data_type.upper() in {"BIT", "STRING"}
    )


def _get_visible_members(dt_element: etree._Element) -> list[dict]:
    """Extract visible member definitions from a DataType element."""
    members = []
    members_el = dt_element.find('Members')
    if members_el is None:
        return members
    for m in members_el.findall('Member'):
        if m.get('Hidden', 'false').lower() == 'true':
            continue
        members.append({
            'name': m.get('Name', ''),
            'data_type': m.get('DataType', ''),
            'dimension': m.get('Dimension', '0'),
        })
    return members


def _compare_udt_definitions(
    existing: etree._Element,
    incoming: etree._Element,
) -> tuple[bool, str, str]:
    """Compare two DataType elements structurally.

    Returns:
        ``(is_equivalent, existing_summary, incoming_summary)``
    """
    ex_members = _get_visible_members(existing)
    in_members = _get_visible_members(incoming)

    ex_summary = ', '.join(
        f"{m['name']}:{m['data_type']}" +
        (f"[{m['dimension']}]" if m['dimension'] != '0' else '')
        for m in ex_members
    )
    in_summary = ', '.join(
        f"{m['name']}:{m['data_type']}" +
        (f"[{m['dimension']}]" if m['dimension'] != '0' else '')
        for m in in_members
    )

    if len(ex_members) != len(in_members):
        return False, ex_summary, in_summary

    for ex, inc in zip(ex_members, in_members):
        if (ex['name'].upper() != inc['name'].upper()
                or ex['data_type'].upper() != inc['data_type'].upper()
                or ex['dimension'] != inc['dimension']):
            return False, ex_summary, in_summary

    return True, ex_summary, in_summary


def _get_aoi_signature(aoi_element: etree._Element) -> list[dict]:
    """Extract parameter signature from an AOI element."""
    params = []
    params_el = aoi_element.find('Parameters')
    if params_el is None:
        return params
    for p in params_el.findall('Parameter'):
        params.append({
            'name': p.get('Name', ''),
            'data_type': p.get('DataType', ''),
            'usage': p.get('Usage', ''),
        })
    return params


def _compare_aoi_definitions(
    existing: etree._Element,
    incoming: etree._Element,
) -> tuple[bool, str, str]:
    """Compare two AOI definitions structurally (parameters and local tags).

    Returns:
        ``(is_equivalent, existing_summary, incoming_summary)``
    """
    ex_params = _get_aoi_signature(existing)
    in_params = _get_aoi_signature(incoming)

    ex_summary = ', '.join(
        f"{p['name']}:{p['data_type']}({p['usage']})" for p in ex_params
    )
    in_summary = ', '.join(
        f"{p['name']}:{p['data_type']}({p['usage']})" for p in in_params
    )

    if len(ex_params) != len(in_params):
        return False, ex_summary, in_summary

    for ex, inc in zip(ex_params, in_params):
        if (ex['name'].upper() != inc['name'].upper()
                or ex['data_type'].upper() != inc['data_type'].upper()
                or ex['usage'].upper() != inc['usage'].upper()):
            return False, ex_summary, in_summary

    # Also compare local tag count and types
    ex_locals = existing.find('LocalTags')
    in_locals = incoming.find('LocalTags')
    ex_lt_count = len(ex_locals.findall('LocalTag')) if ex_locals is not None else 0
    in_lt_count = len(in_locals.findall('LocalTag')) if in_locals is not None else 0
    if ex_lt_count != in_lt_count:
        return False, ex_summary + f" [{ex_lt_count} locals]", \
               in_summary + f" [{in_lt_count} locals]"

    return True, ex_summary, in_summary


def _compare_tag_definitions(
    existing: etree._Element,
    incoming: etree._Element,
) -> tuple[bool, str, str]:
    """Compare two tag elements by DataType and Dimensions.

    Returns:
        ``(is_equivalent, existing_summary, incoming_summary)``
    """
    ex_dt = existing.get('DataType', '')
    ex_dim = existing.get('Dimensions', '0')
    in_dt = incoming.get('DataType', '')
    in_dim = incoming.get('Dimensions', '0')

    ex_summary = f"{ex_dt}" + (f"[{ex_dim}]" if ex_dim != '0' else '')
    in_summary = f"{in_dt}" + (f"[{in_dim}]" if in_dim != '0' else '')

    is_eq = (ex_dt.upper() == in_dt.upper() and ex_dim == in_dim)
    return is_eq, ex_summary, in_summary


# ---------------------------------------------------------------------------
# Source file helpers
# ---------------------------------------------------------------------------

def _get_source_controller(source_root: etree._Element) -> etree._Element:
    """Get the Controller element from a source export file."""
    ctrl = source_root.find('Controller')
    if ctrl is None:
        raise ValueError("Source file has no <Controller> element.")
    return ctrl


def _get_source_target_type(source_root: etree._Element) -> str:
    """Get the TargetType from the source export file root."""
    return source_root.get('TargetType', 'Controller')


def _find_existing_udt(project, name: str) -> Optional[etree._Element]:
    """Find a UDT in the project by name (case-insensitive)."""
    dt_el = project.data_types_element
    if dt_el is None:
        return None
    for dt in dt_el.findall('DataType'):
        if dt.get('Name', '').upper() == name.upper():
            return dt
    return None


def _find_existing_aoi(project, name: str) -> Optional[etree._Element]:
    """Find an AOI in the project by name (case-insensitive)."""
    aoi_el = project.aoi_definitions_element
    if aoi_el is None:
        return None
    for aoi in aoi_el.findall('AddOnInstructionDefinition'):
        if aoi.get('Name', '').upper() == name.upper():
            return aoi
    return None


def _find_existing_tag(
    project, name: str, scope: str, program_name: str = "",
) -> Optional[etree._Element]:
    """Find a tag in the project by name and scope."""
    if scope == 'controller':
        tags_el = project.controller_tags_element
        if tags_el is None:
            return None
        for tag in tags_el.findall('Tag'):
            if tag.get('Name', '').upper() == name.upper():
                return tag
    else:
        try:
            prog_el = project.get_program_element(program_name)
            tags_el = prog_el.find('Tags')
            if tags_el is None:
                return None
            for tag in tags_el.findall('Tag'):
                if tag.get('Name', '').upper() == name.upper():
                    return tag
        except (KeyError, ValueError):
            return None
    return None


def _find_existing_program(project, name: str) -> Optional[etree._Element]:
    """Find a program in the project by name (case-insensitive)."""
    progs = project.programs_element
    if progs is None:
        return None
    for prog in progs.findall('Program'):
        if prog.get('Name', '').upper() == name.upper():
            return prog
    return None


def _find_existing_routine(
    project, program_name: str, routine_name: str,
) -> Optional[etree._Element]:
    """Find a routine by name within a program."""
    try:
        prog_el = project.get_program_element(program_name)
    except (KeyError, ValueError):
        return None
    routines_el = prog_el.find('Routines')
    if routines_el is None:
        return None
    for r in routines_el.findall('Routine'):
        if r.get('Name', '').upper() == routine_name.upper():
            return r
    return None


# ---------------------------------------------------------------------------
# Conflict checking
# ---------------------------------------------------------------------------

def _check_udt_conflicts(
    project,
    source_controller: etree._Element,
) -> list[ImportConflict]:
    """Check all UDTs in the source for conflicts with the project."""
    conflicts = []
    src_dt = source_controller.find('DataTypes')
    if src_dt is None:
        return conflicts

    for dt in src_dt.findall('DataType'):
        name = dt.get('Name', '')
        if not name:
            continue
        # Skip if it's the target (will be handled separately)
        if dt.get('Use', '') == 'Target':
            continue
        existing = _find_existing_udt(project, name)
        if existing is not None:
            is_eq, ex_sum, in_sum = _compare_udt_definitions(existing, dt)
            if not is_eq:
                conflicts.append(ImportConflict(
                    category='udt',
                    name=name,
                    conflict_type='definition_mismatch',
                    description=(
                        f"UDT '{name}' exists in the project with a "
                        f"different definition."
                    ),
                    source_detail=f"Import: {in_sum}",
                    target_detail=f"Project: {ex_sum}",
                ))
    return conflicts


def _check_aoi_conflicts(
    project,
    source_controller: etree._Element,
) -> list[ImportConflict]:
    """Check all AOIs in the source for conflicts with the project."""
    conflicts = []
    src_aoi = source_controller.find('AddOnInstructionDefinitions')
    if src_aoi is None:
        return conflicts

    for aoi in src_aoi.findall('AddOnInstructionDefinition'):
        name = aoi.get('Name', '')
        if not name:
            continue
        if aoi.get('Use', '') == 'Target':
            continue
        existing = _find_existing_aoi(project, name)
        if existing is not None:
            is_eq, ex_sum, in_sum = _compare_aoi_definitions(existing, aoi)
            if not is_eq:
                conflicts.append(ImportConflict(
                    category='aoi',
                    name=name,
                    conflict_type='definition_mismatch',
                    description=(
                        f"AOI '{name}' exists in the project with a "
                        f"different definition."
                    ),
                    source_detail=f"Import: {in_sum}",
                    target_detail=f"Project: {ex_sum}",
                ))
    return conflicts


def _check_tag_conflicts(
    project,
    source_controller: etree._Element,
    source_program_name: str = "",
) -> list[ImportConflict]:
    """Check all tags in the source for conflicts with the project."""
    conflicts = []

    # Check controller-scope tags
    src_tags = source_controller.find('Tags')
    if src_tags is not None:
        for tag in src_tags.findall('Tag'):
            name = tag.get('Name', '')
            if not name:
                continue
            existing = _find_existing_tag(project, name, 'controller')
            if existing is not None:
                is_eq, ex_sum, in_sum = _compare_tag_definitions(
                    existing, tag
                )
                if not is_eq:
                    conflicts.append(ImportConflict(
                        category='tag',
                        name=name,
                        conflict_type='definition_mismatch',
                        description=(
                            f"Controller tag '{name}' exists with a "
                            f"different data type."
                        ),
                        source_detail=f"Import: {in_sum}",
                        target_detail=f"Project: {ex_sum}",
                    ))

    # Check program-scope tags
    src_progs = source_controller.find('Programs')
    if src_progs is not None:
        for prog in src_progs.findall('Program'):
            prog_name = prog.get('Name', '')
            src_ptags = prog.find('Tags')
            if src_ptags is None:
                continue
            for tag in src_ptags.findall('Tag'):
                tname = tag.get('Name', '')
                if not tname:
                    continue
                target_prog = source_program_name or prog_name
                existing = _find_existing_tag(
                    project, tname, 'program', target_prog
                )
                if existing is not None:
                    is_eq, ex_sum, in_sum = _compare_tag_definitions(
                        existing, tag
                    )
                    if not is_eq:
                        conflicts.append(ImportConflict(
                            category='tag',
                            name=f"{target_prog}.{tname}",
                            conflict_type='definition_mismatch',
                            description=(
                                f"Program tag '{tname}' in '{target_prog}' "
                                f"exists with a different data type."
                            ),
                            source_detail=f"Import: {in_sum}",
                            target_detail=f"Project: {ex_sum}",
                        ))

    return conflicts


# ---------------------------------------------------------------------------
# Import sub-operations
# ---------------------------------------------------------------------------

def _import_context_udts(
    project,
    source_controller: etree._Element,
    conflict_resolution: str,
    result: ImportResult,
) -> None:
    """Import context UDTs from the source into the project."""
    src_dt = source_controller.find('DataTypes')
    if src_dt is None:
        return

    dt_container = find_or_create(project.controller, 'DataTypes')

    for dt in src_dt.findall('DataType'):
        name = dt.get('Name', '')
        if not name:
            continue

        existing = _find_existing_udt(project, name)
        if existing is not None:
            is_eq, ex_sum, in_sum = _compare_udt_definitions(existing, dt)
            if is_eq:
                continue  # Identical, skip silently
            if conflict_resolution == 'skip':
                result.skipped.append(f"UDT:{name}")
                continue
            if conflict_resolution == 'overwrite':
                parent = existing.getparent()
                if parent is not None:
                    idx = list(parent).index(existing)
                    parent.remove(existing)
                    cloned = deep_copy(dt)
                    parent.insert(idx, cloned)
                    result.imported['udts'] += 1
                continue
            if conflict_resolution == 'fail':
                result.success = False
                result.conflicts.append(ImportConflict(
                    category='udt', name=name,
                    conflict_type='definition_mismatch',
                    description=f"UDT '{name}' definition mismatch.",
                    source_detail=in_sum, target_detail=ex_sum,
                ))
                return
        else:
            cloned = deep_copy(dt)
            dt_container.append(cloned)
            result.imported['udts'] += 1


def _import_context_aois(
    project,
    source_controller: etree._Element,
    conflict_resolution: str,
    result: ImportResult,
) -> None:
    """Import context AOIs from the source into the project."""
    src_aoi = source_controller.find('AddOnInstructionDefinitions')
    if src_aoi is None:
        return

    controller = project.controller
    aoi_container = find_or_create(
        controller, 'AddOnInstructionDefinitions'
    )

    for aoi in src_aoi.findall('AddOnInstructionDefinition'):
        name = aoi.get('Name', '')
        if not name:
            continue

        existing = _find_existing_aoi(project, name)
        if existing is not None:
            is_eq, ex_sum, in_sum = _compare_aoi_definitions(existing, aoi)
            if is_eq:
                continue  # Identical, skip silently
            if conflict_resolution == 'skip':
                result.skipped.append(f"AOI:{name}")
                continue
            if conflict_resolution == 'overwrite':
                parent = existing.getparent()
                if parent is not None:
                    idx = list(parent).index(existing)
                    parent.remove(existing)
                    cloned = deep_copy(aoi)
                    _update_edited_date(cloned)
                    parent.insert(idx, cloned)
                    result.imported['aois'] += 1
                continue
            if conflict_resolution == 'fail':
                result.success = False
                result.conflicts.append(ImportConflict(
                    category='aoi', name=name,
                    conflict_type='definition_mismatch',
                    description=f"AOI '{name}' definition mismatch.",
                    source_detail=in_sum, target_detail=ex_sum,
                ))
                return
        else:
            cloned = deep_copy(aoi)
            _update_edited_date(cloned)
            aoi_container.append(cloned)
            result.imported['aois'] += 1


def _import_tags_to_scope(
    project,
    source_tags_el: etree._Element,
    target_tags_el: etree._Element,
    scope: str,
    program_name: str,
    conflict_resolution: str,
    result: ImportResult,
) -> None:
    """Import tags from a source Tags element into a target Tags element."""
    for tag in source_tags_el.findall('Tag'):
        name = tag.get('Name', '')
        if not name:
            continue

        existing = _find_existing_tag(project, name, scope, program_name)
        if existing is not None:
            is_eq, ex_sum, in_sum = _compare_tag_definitions(existing, tag)
            if is_eq:
                continue  # Identical type, skip silently
            if conflict_resolution == 'skip':
                result.skipped.append(
                    f"Tag:{program_name + '.' if program_name else ''}{name}"
                )
                continue
            if conflict_resolution == 'overwrite':
                parent = existing.getparent()
                if parent is not None:
                    idx = list(parent).index(existing)
                    parent.remove(existing)
                    cloned = deep_copy(tag)
                    parent.insert(idx, cloned)
                    result.imported['tags'] += 1
                continue
            if conflict_resolution == 'fail':
                result.success = False
                result.conflicts.append(ImportConflict(
                    category='tag', name=name,
                    conflict_type='definition_mismatch',
                    description=f"Tag '{name}' type mismatch.",
                    source_detail=in_sum, target_detail=ex_sum,
                ))
                return
        else:
            cloned = deep_copy(tag)
            target_tags_el.append(cloned)
            result.imported['tags'] += 1


# ---------------------------------------------------------------------------
# Type-specific import dispatchers
# ---------------------------------------------------------------------------

def _import_rungs(
    project,
    source_root: etree._Element,
    target_program: str,
    target_routine: str,
    rung_position: int,
    conflict_resolution: str,
    result: ImportResult,
) -> None:
    """Import rungs from a Rung-type export file."""
    src_ctrl = _get_source_controller(source_root)

    # Import dependencies first
    _import_context_udts(project, src_ctrl, conflict_resolution, result)
    if not result.success:
        return
    _import_context_aois(project, src_ctrl, conflict_resolution, result)
    if not result.success:
        return

    # Import controller-scope tags
    src_tags = src_ctrl.find('Tags')
    if src_tags is not None:
        ctrl_tags = find_or_create(project.controller, 'Tags')
        _import_tags_to_scope(
            project, src_tags, ctrl_tags,
            'controller', '', conflict_resolution, result,
        )
        if not result.success:
            return

    # Find source program/routine with rungs
    src_progs = src_ctrl.find('Programs')
    if src_progs is None:
        return

    source_rungs = []
    source_prog_name = ''
    for prog in src_progs.findall('Program'):
        source_prog_name = prog.get('Name', '')
        # Import program-scope tags
        src_ptags = prog.find('Tags')
        if src_ptags is not None:
            tp = target_program or source_prog_name
            try:
                target_prog_el = project.get_program_element(tp)
                tgt_ptags = find_or_create(target_prog_el, 'Tags')
                _import_tags_to_scope(
                    project, src_ptags, tgt_ptags,
                    'program', tp, conflict_resolution, result,
                )
                if not result.success:
                    return
            except (KeyError, ValueError):
                pass  # Target program doesn't exist yet

        routines = prog.find('Routines')
        if routines is None:
            continue
        for routine in routines.findall('Routine'):
            rll = routine.find('RLLContent')
            if rll is not None:
                source_rungs.extend(rll.findall('Rung'))

    if not source_rungs:
        return

    # Get target routine
    tp = target_program or source_prog_name
    tr = target_routine
    if not tr:
        # Try to find routine from source
        for prog in src_progs.findall('Program'):
            routines = prog.find('Routines')
            if routines is not None:
                for r in routines.findall('Routine'):
                    tr = r.get('Name', '')
                    break
            if tr:
                break
    if not tr:
        tr = 'MainRoutine'

    try:
        target_routine_el = project.get_routine_element(tp, tr)
    except (KeyError, ValueError):
        result.success = False
        result.conflicts.append(ImportConflict(
            category='routine', name=f"{tp}/{tr}",
            conflict_type='name_exists',
            description=f"Target routine '{tp}/{tr}' does not exist.",
            source_detail='', target_detail='',
        ))
        return

    target_rll = target_routine_el.find('RLLContent')
    if target_rll is None:
        # Auto-create RLLContent if the routine is RLL type
        if target_routine_el.get('Type', 'RLL') == 'RLL':
            target_rll = etree.SubElement(target_routine_el, 'RLLContent')
        else:
            result.success = False
            result.conflicts.append(ImportConflict(
                category='routine', name=f"{tp}/{tr}",
                conflict_type='definition_mismatch',
                description=f"Target routine '{tr}' is not an RLL routine.",
                source_detail='', target_detail='',
            ))
            return

    # Determine insert position
    existing_rungs = target_rll.findall('Rung')
    if rung_position < 0 or rung_position >= len(existing_rungs):
        insert_idx = len(existing_rungs)
    else:
        insert_idx = rung_position

    # Insert rungs
    for i, rung in enumerate(source_rungs):
        cloned = deep_copy(rung)
        # Position in the target RLLContent element
        target_rll.insert(insert_idx + i, cloned)
        result.imported['rungs'] += 1

    # Renumber all rungs
    for idx, rung in enumerate(target_rll.findall('Rung')):
        rung.set('Number', str(idx))


def _import_routine(
    project,
    source_root: etree._Element,
    target_program: str,
    conflict_resolution: str,
    result: ImportResult,
) -> None:
    """Import a routine from a Routine-type export file."""
    src_ctrl = _get_source_controller(source_root)

    # Import dependencies
    _import_context_udts(project, src_ctrl, conflict_resolution, result)
    if not result.success:
        return
    _import_context_aois(project, src_ctrl, conflict_resolution, result)
    if not result.success:
        return

    # Import controller-scope tags
    src_tags = src_ctrl.find('Tags')
    if src_tags is not None:
        ctrl_tags = find_or_create(project.controller, 'Tags')
        _import_tags_to_scope(
            project, src_tags, ctrl_tags,
            'controller', '', conflict_resolution, result,
        )
        if not result.success:
            return

    # Find the target routine element (Use="Target")
    src_progs = src_ctrl.find('Programs')
    if src_progs is None:
        result.success = False
        return

    target_routine_el = None
    source_prog_name = ''
    for prog in src_progs.findall('Program'):
        source_prog_name = prog.get('Name', '')
        # Import program-scope tags
        src_ptags = prog.find('Tags')
        if src_ptags is not None:
            tp = target_program or source_prog_name
            try:
                target_prog_el = project.get_program_element(tp)
                tgt_ptags = find_or_create(target_prog_el, 'Tags')
                _import_tags_to_scope(
                    project, src_ptags, tgt_ptags,
                    'program', tp, conflict_resolution, result,
                )
                if not result.success:
                    return
            except (KeyError, ValueError):
                pass

        routines = prog.find('Routines')
        if routines is None:
            continue
        for routine in routines.findall('Routine'):
            if routine.get('Use', '') == 'Target':
                target_routine_el = routine
                break
        if target_routine_el is not None:
            break

    if target_routine_el is None:
        # Fall back to first routine found
        for prog in src_progs.findall('Program'):
            routines = prog.find('Routines')
            if routines is not None:
                for routine in routines.findall('Routine'):
                    target_routine_el = routine
                    break
            if target_routine_el is not None:
                break

    if target_routine_el is None:
        result.success = False
        return

    routine_name = target_routine_el.get('Name', '')
    tp = target_program or source_prog_name

    # Check for existing routine
    existing = _find_existing_routine(project, tp, routine_name)
    if existing is not None:
        if conflict_resolution == 'skip':
            result.skipped.append(f"Routine:{routine_name}")
            return
        if conflict_resolution == 'overwrite':
            parent = existing.getparent()
            if parent is not None:
                parent.remove(existing)
        elif conflict_resolution == 'fail':
            result.success = False
            result.conflicts.append(ImportConflict(
                category='routine', name=routine_name,
                conflict_type='name_exists',
                description=(
                    f"Routine '{routine_name}' already exists in "
                    f"program '{tp}'."
                ),
                source_detail=f"Routine from import file",
                target_detail=f"Existing routine in '{tp}'",
            ))
            return
        elif conflict_resolution == 'report':
            result.conflicts.append(ImportConflict(
                category='routine', name=routine_name,
                conflict_type='name_exists',
                description=(
                    f"Routine '{routine_name}' already exists in "
                    f"program '{tp}'."
                ),
                source_detail=f"Routine from import file",
                target_detail=f"Existing routine in '{tp}'",
            ))
            return

    # Insert the routine
    try:
        target_prog_el = project.get_program_element(tp)
    except (KeyError, ValueError):
        result.success = False
        result.conflicts.append(ImportConflict(
            category='program', name=tp,
            conflict_type='name_exists',
            description=f"Target program '{tp}' does not exist.",
            source_detail='', target_detail='',
        ))
        return

    routines_container = find_or_create(target_prog_el, 'Routines')
    cloned = deep_copy(target_routine_el)
    # Remove Use attribute (it's "Target" in the export)
    if 'Use' in cloned.attrib:
        del cloned.attrib['Use']
    routines_container.append(cloned)
    result.imported['routines'] += 1


def _import_program(
    project,
    source_root: etree._Element,
    conflict_resolution: str,
    result: ImportResult,
) -> None:
    """Import a program from a Program-type export file."""
    src_ctrl = _get_source_controller(source_root)

    # Import dependencies
    _import_context_udts(project, src_ctrl, conflict_resolution, result)
    if not result.success:
        return
    _import_context_aois(project, src_ctrl, conflict_resolution, result)
    if not result.success:
        return

    # Import controller-scope tags
    src_tags = src_ctrl.find('Tags')
    if src_tags is not None:
        ctrl_tags = find_or_create(project.controller, 'Tags')
        _import_tags_to_scope(
            project, src_tags, ctrl_tags,
            'controller', '', conflict_resolution, result,
        )
        if not result.success:
            return

    # Find the target program element (Use="Target")
    src_progs = src_ctrl.find('Programs')
    if src_progs is None:
        result.success = False
        return

    target_prog_el = None
    for prog in src_progs.findall('Program'):
        if prog.get('Use', '') == 'Target':
            target_prog_el = prog
            break

    if target_prog_el is None:
        # Fall back to first program
        progs_list = src_progs.findall('Program')
        if progs_list:
            target_prog_el = progs_list[0]

    if target_prog_el is None:
        result.success = False
        return

    program_name = target_prog_el.get('Name', '')

    # Check for existing program
    existing = _find_existing_program(project, program_name)
    if existing is not None:
        if conflict_resolution == 'skip':
            result.skipped.append(f"Program:{program_name}")
            return
        if conflict_resolution == 'overwrite':
            parent = existing.getparent()
            if parent is not None:
                parent.remove(existing)
        elif conflict_resolution == 'fail':
            result.success = False
            result.conflicts.append(ImportConflict(
                category='program', name=program_name,
                conflict_type='name_exists',
                description=(
                    f"Program '{program_name}' already exists in "
                    f"the project."
                ),
                source_detail=f"Program from import file",
                target_detail=f"Existing program in project",
            ))
            return
        elif conflict_resolution == 'report':
            result.conflicts.append(ImportConflict(
                category='program', name=program_name,
                conflict_type='name_exists',
                description=(
                    f"Program '{program_name}' already exists in "
                    f"the project."
                ),
                source_detail=f"Program from import file",
                target_detail=f"Existing program in project",
            ))
            return

    # Insert the program
    programs_container = find_or_create(project.controller, 'Programs')
    cloned = deep_copy(target_prog_el)
    # Remove Use attribute
    if 'Use' in cloned.attrib:
        del cloned.attrib['Use']
    programs_container.append(cloned)
    result.imported['programs'] += 1


def _import_udt_component(
    project,
    source_root: etree._Element,
    conflict_resolution: str,
    result: ImportResult,
) -> None:
    """Import a UDT from a DataType-type export file."""
    src_ctrl = _get_source_controller(source_root)

    # Import all UDTs (context dependencies + target)
    src_dt = src_ctrl.find('DataTypes')
    if src_dt is None:
        result.success = False
        return

    dt_container = find_or_create(project.controller, 'DataTypes')

    # Import context UDTs first (non-Target), then the Target
    for dt in src_dt.findall('DataType'):
        name = dt.get('Name', '')
        if not name:
            continue
        is_target = dt.get('Use', '') == 'Target'

        existing = _find_existing_udt(project, name)
        if existing is not None:
            is_eq, ex_sum, in_sum = _compare_udt_definitions(existing, dt)
            if is_eq:
                if not is_target:
                    continue  # Skip identical context UDT
                else:
                    result.skipped.append(f"UDT:{name} (identical)")
                    return
            if conflict_resolution == 'skip':
                result.skipped.append(f"UDT:{name}")
                if is_target:
                    return
                continue
            if conflict_resolution == 'overwrite':
                parent = existing.getparent()
                if parent is not None:
                    idx = list(parent).index(existing)
                    parent.remove(existing)
                    cloned = deep_copy(dt)
                    if 'Use' in cloned.attrib:
                        del cloned.attrib['Use']
                    parent.insert(idx, cloned)
                    result.imported['udts'] += 1
                if is_target:
                    return
                continue
            if conflict_resolution == 'fail':
                result.success = False
                result.conflicts.append(ImportConflict(
                    category='udt', name=name,
                    conflict_type='definition_mismatch',
                    description=f"UDT '{name}' definition mismatch.",
                    source_detail=in_sum, target_detail=ex_sum,
                ))
                return
            if conflict_resolution == 'report':
                result.conflicts.append(ImportConflict(
                    category='udt', name=name,
                    conflict_type='definition_mismatch',
                    description=f"UDT '{name}' definition mismatch.",
                    source_detail=f"Import: {in_sum}",
                    target_detail=f"Project: {ex_sum}",
                ))
                if is_target:
                    return
                continue
        else:
            cloned = deep_copy(dt)
            if 'Use' in cloned.attrib:
                del cloned.attrib['Use']
            dt_container.append(cloned)
            result.imported['udts'] += 1


def _import_aoi_component(
    project,
    source_root: etree._Element,
    conflict_resolution: str,
    result: ImportResult,
) -> None:
    """Import an AOI from an AddOnInstructionDefinition-type export file."""
    src_ctrl = _get_source_controller(source_root)

    # Import context UDTs first
    _import_context_udts(project, src_ctrl, conflict_resolution, result)
    if not result.success:
        return

    # Import all AOIs (context + target)
    src_aoi = src_ctrl.find('AddOnInstructionDefinitions')
    if src_aoi is None:
        result.success = False
        return

    controller = project.controller
    aoi_container = find_or_create(
        controller, 'AddOnInstructionDefinitions'
    )

    for aoi in src_aoi.findall('AddOnInstructionDefinition'):
        name = aoi.get('Name', '')
        if not name:
            continue
        is_target = aoi.get('Use', '') == 'Target'

        existing = _find_existing_aoi(project, name)
        if existing is not None:
            is_eq, ex_sum, in_sum = _compare_aoi_definitions(existing, aoi)
            if is_eq:
                if not is_target:
                    continue
                else:
                    result.skipped.append(f"AOI:{name} (identical)")
                    return
            if conflict_resolution == 'skip':
                result.skipped.append(f"AOI:{name}")
                if is_target:
                    return
                continue
            if conflict_resolution == 'overwrite':
                parent = existing.getparent()
                if parent is not None:
                    idx = list(parent).index(existing)
                    parent.remove(existing)
                    cloned = deep_copy(aoi)
                    if 'Use' in cloned.attrib:
                        del cloned.attrib['Use']
                    _update_edited_date(cloned)
                    parent.insert(idx, cloned)
                    result.imported['aois'] += 1
                if is_target:
                    return
                continue
            if conflict_resolution == 'fail':
                result.success = False
                result.conflicts.append(ImportConflict(
                    category='aoi', name=name,
                    conflict_type='definition_mismatch',
                    description=f"AOI '{name}' definition mismatch.",
                    source_detail=in_sum, target_detail=ex_sum,
                ))
                return
            if conflict_resolution == 'report':
                result.conflicts.append(ImportConflict(
                    category='aoi', name=name,
                    conflict_type='definition_mismatch',
                    description=f"AOI '{name}' definition mismatch.",
                    source_detail=f"Import: {in_sum}",
                    target_detail=f"Project: {ex_sum}",
                ))
                if is_target:
                    return
                continue
        else:
            cloned = deep_copy(aoi)
            if 'Use' in cloned.attrib:
                del cloned.attrib['Use']
            _update_edited_date(cloned)
            aoi_container.append(cloned)
            result.imported['aois'] += 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_import(
    project,
    file_path: str,
) -> ImportResult:
    """Dry-run analysis of importing a component export file.

    Loads the export file, detects the TargetType, and checks all elements
    against the project for conflicts without making any changes.

    Args:
        project: The loaded L5XProject (target).
        file_path: Path to the component export ``.L5X`` file.

    Returns:
        An :class:`ImportResult` with conflicts populated but no changes made.

    Raises:
        FileNotFoundError: If *file_path* does not exist.
        ValueError: If the file is not a valid L5X file.
    """
    logger.info("Analyzing import from '%s'", file_path)
    source_root = parse_l5x(file_path)
    src_ctrl = _get_source_controller(source_root)
    target_type = _get_source_target_type(source_root)
    target_name = source_root.get('TargetName', '')

    result = ImportResult()

    # Check UDT conflicts
    result.conflicts.extend(_check_udt_conflicts(project, src_ctrl))

    # Check AOI conflicts
    result.conflicts.extend(_check_aoi_conflicts(project, src_ctrl))

    # Check tag conflicts
    result.conflicts.extend(_check_tag_conflicts(project, src_ctrl))

    # Check target-specific conflicts
    if target_type == 'Program':
        src_progs = src_ctrl.find('Programs')
        if src_progs is not None:
            for prog in src_progs.findall('Program'):
                if prog.get('Use', '') == 'Target':
                    pname = prog.get('Name', '')
                    existing = _find_existing_program(project, pname)
                    if existing is not None:
                        result.conflicts.append(ImportConflict(
                            category='program', name=pname,
                            conflict_type='name_exists',
                            description=(
                                f"Program '{pname}' already exists."
                            ),
                            source_detail='Program from import file',
                            target_detail='Existing program in project',
                        ))

    elif target_type == 'Routine':
        src_progs = src_ctrl.find('Programs')
        if src_progs is not None:
            for prog in src_progs.findall('Program'):
                prog_name = prog.get('Name', '')
                routines = prog.find('Routines')
                if routines is None:
                    continue
                for r in routines.findall('Routine'):
                    if r.get('Use', '') == 'Target':
                        rname = r.get('Name', '')
                        existing = _find_existing_routine(
                            project, prog_name, rname
                        )
                        if existing is not None:
                            result.conflicts.append(ImportConflict(
                                category='routine', name=rname,
                                conflict_type='name_exists',
                                description=(
                                    f"Routine '{rname}' already exists in "
                                    f"program '{prog_name}'."
                                ),
                                source_detail='Routine from import file',
                                target_detail=(
                                    f"Existing routine in '{prog_name}'"
                                ),
                            ))

    elif target_type == 'DataType':
        # Check target UDT specifically
        src_dt = src_ctrl.find('DataTypes')
        if src_dt is not None:
            for dt in src_dt.findall('DataType'):
                if dt.get('Use', '') == 'Target':
                    dname = dt.get('Name', '')
                    existing = _find_existing_udt(project, dname)
                    if existing is not None:
                        is_eq, ex_sum, in_sum = _compare_udt_definitions(
                            existing, dt
                        )
                        if not is_eq:
                            result.conflicts.append(ImportConflict(
                                category='udt', name=dname,
                                conflict_type='definition_mismatch',
                                description=(
                                    f"Target UDT '{dname}' has a different "
                                    f"definition."
                                ),
                                source_detail=f"Import: {in_sum}",
                                target_detail=f"Project: {ex_sum}",
                            ))

    elif target_type == 'AddOnInstructionDefinition':
        src_aoi = src_ctrl.find('AddOnInstructionDefinitions')
        if src_aoi is not None:
            for aoi in src_aoi.findall('AddOnInstructionDefinition'):
                if aoi.get('Use', '') == 'Target':
                    aname = aoi.get('Name', '')
                    existing = _find_existing_aoi(project, aname)
                    if existing is not None:
                        is_eq, ex_sum, in_sum = _compare_aoi_definitions(
                            existing, aoi
                        )
                        if not is_eq:
                            result.conflicts.append(ImportConflict(
                                category='aoi', name=aname,
                                conflict_type='definition_mismatch',
                                description=(
                                    f"Target AOI '{aname}' has a different "
                                    f"definition."
                                ),
                                source_detail=f"Import: {in_sum}",
                                target_detail=f"Project: {ex_sum}",
                            ))

    result.success = len(result.conflicts) == 0
    return result


def import_component(
    project,
    file_path: str,
    conflict_resolution: str = "report",
    target_program: str = "",
    target_routine: str = "",
    rung_position: int = -1,
) -> ImportResult:
    """Import a component export file into the loaded project.

    Automatically detects the TargetType of the export file and dispatches
    to the appropriate import handler.

    Args:
        project: The loaded L5XProject (target).
        file_path: Path to the component export ``.L5X`` file.
        conflict_resolution: How to handle conflicts:
            - ``'report'``: Dry run -- returns conflicts only (no changes).
            - ``'skip'``: Import non-conflicting items, skip conflicts.
            - ``'overwrite'``: Replace existing items with imported versions.
            - ``'fail'``: Abort on any conflict.
        target_program: Override the target program name (for Rung/Routine
            imports).  If empty, uses the program name from the export file.
        target_routine: Override the target routine name (for Rung imports).
            If empty, uses the routine name from the export file.
        rung_position: Insert position for rungs (0-based).  ``-1`` to
            append at end.

    Returns:
        An :class:`ImportResult` with the outcome.

    Raises:
        FileNotFoundError: If *file_path* does not exist.
        ValueError: If the file is not a valid L5X file or has an
            unsupported TargetType.
    """
    logger.info("Importing component from '%s' with conflict_resolution='%s'", file_path, conflict_resolution)
    valid_resolutions = {'report', 'skip', 'overwrite', 'fail'}
    if conflict_resolution not in valid_resolutions:
        raise ValueError(
            f"Invalid conflict_resolution '{conflict_resolution}'. "
            f"Must be one of: {sorted(valid_resolutions)}"
        )

    # For 'report' mode, delegate to analyze_import
    if conflict_resolution == 'report':
        return analyze_import(project, file_path)

    source_root = parse_l5x(file_path)
    target_type = _get_source_target_type(source_root)
    result = ImportResult()

    if target_type == 'Rung':
        _import_rungs(
            project, source_root,
            target_program, target_routine, rung_position,
            conflict_resolution, result,
        )

    elif target_type == 'Routine':
        _import_routine(
            project, source_root,
            target_program, conflict_resolution, result,
        )

    elif target_type == 'Program':
        _import_program(
            project, source_root,
            conflict_resolution, result,
        )

    elif target_type == 'DataType':
        _import_udt_component(
            project, source_root,
            conflict_resolution, result,
        )

    elif target_type == 'AddOnInstructionDefinition':
        _import_aoi_component(
            project, source_root,
            conflict_resolution, result,
        )

    else:
        raise ValueError(
            f"Unsupported TargetType '{target_type}'. "
            f"Expected one of: Rung, Routine, Program, DataType, "
            f"AddOnInstructionDefinition."
        )

    return result
