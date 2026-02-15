"""
User-Defined Type (UDT) import and query operations for L5X files.

Provides functions to import UDT definitions from standalone L5X export files,
query UDT metadata (members, descriptions), and analyze dependencies on other
user-defined types.

UDTs in L5X files live under::

    Controller / DataTypes / DataType

Each DataType element with ``Class="User"`` is a UDT.  Its ``Members`` child
contains ``Member`` elements that define the structure fields.

Special handling is required for BOOL members, which Rockwell encodes using
a bit-packed pattern: a hidden ``SINT`` backing member (named with the
``ZZZZZZZZZZ`` prefix) holds the actual data, and each visible ``BIT`` member
references the backing field via ``Target`` and ``BitNumber`` attributes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from lxml import etree

from .schema import (
    BASE_DATA_TYPES,
    BUILTIN_STRUCTURES,
    CONTROLLER_CHILD_ORDER,
)
from .utils import (
    deep_copy,
    find_or_create,
    get_description,
    insert_in_order,
    parse_l5x,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_controller(project) -> etree._Element:
    """Return the Controller element from a project.

    Args:
        project: An L5XProject instance (must have a ``root`` attribute).

    Returns:
        The ``<Controller>`` element.

    Raises:
        ValueError: If no Controller element is found.
    """
    controller = project.root.find("Controller")
    if controller is None:
        raise ValueError("No <Controller> element found in the project.")
    return controller


def _get_datatypes_container(project) -> etree._Element:
    """Return (or create) the DataTypes container element.

    Args:
        project: An L5XProject instance.

    Returns:
        The ``<DataTypes>`` element.
    """
    controller = _get_controller(project)
    container = controller.find("DataTypes")
    if container is None:
        container = etree.Element("DataTypes")
        insert_in_order(controller, container, CONTROLLER_CHILD_ORDER)
    return container


def _find_udt_element(project, name: str) -> Optional[etree._Element]:
    """Find a DataType element by name.

    Searches for DataType elements with ``Class="User"`` or no Class attribute
    (some exports omit the Class for UDTs).

    Args:
        project: An L5XProject instance.
        name: The UDT name to search for (case-insensitive).

    Returns:
        The matching ``<DataType>`` element, or ``None`` if not found.
    """
    container = _get_controller(project).find("DataTypes")
    if container is None:
        return None
    for dt_elem in container.findall("DataType"):
        if dt_elem.get("Name", "").lower() == name.lower():
            return dt_elem
    return None


def _is_base_or_builtin_type(data_type: str) -> bool:
    """Return True if *data_type* is a base atomic type, a built-in structure,
    or a recognized system type that does not need a UDT definition."""
    return (
        data_type.upper() in BASE_DATA_TYPES
        or data_type.upper() in {k.upper() for k in BUILTIN_STRUCTURES}
        or data_type.upper() in {"BIT", "STRING"}
    )


def _parse_member_element(member: etree._Element) -> dict:
    """Parse a single Member element into a dictionary.

    Args:
        member: The ``<Member>`` element from a DataType definition.

    Returns:
        A dictionary with member metadata:
            - ``name`` (str): Member name.
            - ``data_type`` (str): The data type (e.g., ``"DINT"``, ``"BIT"``).
            - ``dimension`` (str): Array dimension (``"0"`` for scalar).
            - ``radix`` (str): Display radix (e.g., ``"Decimal"``).
            - ``hidden`` (bool): Whether the member is hidden (backing fields).
            - ``external_access`` (str): Access level string.
            - ``description`` (str or None): Member description text.
            - ``target`` (str or None): Backing member name for BIT members.
            - ``bit_number`` (int or None): Bit position for BIT members.
    """
    info: dict[str, Any] = {
        "name": member.get("Name", ""),
        "data_type": member.get("DataType", ""),
        "dimension": member.get("Dimension", "0"),
        "radix": member.get("Radix", ""),
        "hidden": member.get("Hidden", "false").lower() == "true",
        "external_access": member.get("ExternalAccess", "Read/Write"),
        "description": get_description(member),
        "target": member.get("Target"),
        "bit_number": None,
    }

    bit_num_str = member.get("BitNumber")
    if bit_num_str is not None:
        try:
            info["bit_number"] = int(bit_num_str)
        except (ValueError, TypeError):
            info["bit_number"] = None

    return info


def _import_dependent_udts_from_source(
    project,
    source_root: etree._Element,
    exclude_name: str,
) -> list[str]:
    """Import any additional UDT definitions from the source file that the
    primary UDT depends on.

    Scans the members of the primary UDT for DataType references that are
    not base types, then looks for matching DataType definitions in the
    source file and imports them recursively.

    Args:
        project: The target L5XProject instance.
        source_root: The root element of the source L5X file.
        exclude_name: Name of the primary UDT (already being imported).

    Returns:
        List of UDT names that were imported as dependencies.
    """
    imported: list[str] = []
    target_container = _get_datatypes_container(project)

    source_controller = source_root.find("Controller")
    if source_controller is None:
        return imported

    source_datatypes = source_controller.find("DataTypes")
    if source_datatypes is None:
        return imported

    # Build a map of source UDTs by name (case-insensitive)
    source_map: dict[str, etree._Element] = {}
    for dt_elem in source_datatypes.findall("DataType"):
        dt_name = dt_elem.get("Name", "")
        source_map[dt_name.lower()] = dt_elem

    # Track existing UDTs in the target project
    existing_names: set[str] = {
        dt.get("Name", "").lower()
        for dt in target_container.findall("DataType")
    }

    # Use a work queue to handle transitive dependencies
    to_process: list[str] = []

    # Find direct dependencies of the primary UDT
    primary_source = source_map.get(exclude_name.lower())
    if primary_source is not None:
        members_container = primary_source.find("Members")
        if members_container is not None:
            for member in members_container.findall("Member"):
                dt = member.get("DataType", "")
                if dt and not _is_base_or_builtin_type(dt):
                    if dt.lower() != exclude_name.lower():
                        to_process.append(dt)

    processed: set[str] = {exclude_name.lower()}

    while to_process:
        dep_name = to_process.pop(0)
        dep_key = dep_name.lower()

        if dep_key in processed:
            continue
        processed.add(dep_key)

        # Skip if already in the target project
        if dep_key in existing_names:
            continue

        # Find in source
        source_elem = source_map.get(dep_key)
        if source_elem is None:
            continue

        # Import it
        cloned = deep_copy(source_elem)
        target_container.append(cloned)
        existing_names.add(dep_key)
        imported.append(source_elem.get("Name", ""))

        # Queue transitive dependencies
        members_container = source_elem.find("Members")
        if members_container is not None:
            for member in members_container.findall("Member"):
                dt = member.get("DataType", "")
                if dt and not _is_base_or_builtin_type(dt):
                    if dt.lower() not in processed:
                        to_process.append(dt)

    return imported


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def import_udt(
    project,
    file_path: str,
    overwrite: bool = False,
) -> etree._Element:
    """Import a UDT definition from an L5X export file.

    The source file should have ``TargetType="DataType"``.  Any UDTs that the
    imported type depends on (i.e., UDTs used as member data types) are also
    imported from the source file if they do not already exist in the project.

    Args:
        project: The target L5XProject instance.
        file_path: Path to the L5X file containing the UDT definition.
        overwrite: If ``False`` (default) and a UDT with the same name
            already exists in the project, a ``ValueError`` is raised.
            If ``True``, the existing definition is replaced.

    Returns:
        The imported ``<DataType>`` element (as inserted into the project tree).

    Raises:
        FileNotFoundError: If *file_path* does not exist.
        ValueError: If the source file does not contain a UDT definition,
            or if ``overwrite`` is False and the UDT already exists.
        etree.XMLSyntaxError: If the source file contains malformed XML.
    """
    logger.info("Importing UDT from %r", file_path)
    source_root = parse_l5x(file_path)

    # Locate the DataType definition in the source file.
    udt_elem = None
    source_controller = source_root.find("Controller")
    if source_controller is not None:
        datatypes_container = source_controller.find("DataTypes")
        if datatypes_container is not None:
            udt_elems = datatypes_container.findall("DataType")
            # Prefer user-class types; fall back to the first one found
            for dt in udt_elems:
                dt_class = dt.get("Class", "")
                if dt_class == "User" or not dt_class:
                    udt_elem = dt
                    break
            if udt_elem is None and udt_elems:
                udt_elem = udt_elems[0]

    # Some export formats place the definition more directly
    if udt_elem is None:
        udt_elem = source_root.find(".//DataType")

    if udt_elem is None:
        raise ValueError(
            f"No DataType definition found in '{file_path}'. "
            "Ensure the file was exported with TargetType='DataType'."
        )

    udt_name = udt_elem.get("Name", "")
    if not udt_name:
        raise ValueError(
            f"DataType definition in '{file_path}' has no Name attribute."
        )

    # Check for existing definition
    existing = _find_udt_element(project, udt_name)
    if existing is not None and not overwrite:
        raise ValueError(
            f"UDT '{udt_name}' already exists in the project. "
            "Use overwrite=True to replace it."
        )

    # Import dependent UDTs first (transitive)
    _import_dependent_udts_from_source(project, source_root, udt_name)

    # Clone the primary UDT
    cloned_udt = deep_copy(udt_elem)

    # Insert or replace in the project
    container = _get_datatypes_container(project)
    if existing is not None and overwrite:
        parent = existing.getparent()
        if parent is not None:
            idx = list(parent).index(existing)
            parent.remove(existing)
            parent.insert(idx, cloned_udt)
        else:
            container.append(cloned_udt)
    else:
        container.append(cloned_udt)

    return cloned_udt


def get_udt_info(project, name: str) -> dict:
    """Get detailed information about a UDT.

    Args:
        project: The L5XProject instance.
        name: The UDT name.

    Returns:
        A dictionary containing:
            - ``name`` (str): The UDT name.
            - ``family`` (str): The family (typically ``"NoFamily"``).
            - ``class_`` (str): The class (``"User"`` for UDTs).
            - ``description`` (str or None): The UDT description text.
            - ``members`` (list[dict]): All member definitions, including
              hidden backing fields for BIT members.  Each member dict
              contains: ``name``, ``data_type``, ``dimension``, ``radix``,
              ``hidden``, ``external_access``, ``description``, ``target``,
              ``bit_number``.

    Raises:
        ValueError: If the UDT is not found.
    """
    logger.info("Querying UDT info for %r", name)
    udt_elem = _find_udt_element(project, name)
    if udt_elem is None:
        raise ValueError(f"UDT '{name}' not found in the project.")

    info: dict[str, Any] = {
        "name": udt_elem.get("Name", ""),
        "family": udt_elem.get("Family", "NoFamily"),
        "class_": udt_elem.get("Class", "User"),
        "description": get_description(udt_elem),
        "members": [],
    }

    members_container = udt_elem.find("Members")
    if members_container is not None:
        for member in members_container.findall("Member"):
            info["members"].append(_parse_member_element(member))

    return info


def get_udt_members(project, name: str) -> list[dict]:
    """Get the visible (non-hidden) members of a UDT.

    This returns only the members that are visible in the Studio 5000
    interface.  Hidden backing fields for BIT-packed BOOL members
    (the ``ZZZZZZZZZZ``-prefixed SINT members) are excluded.

    Args:
        project: The L5XProject instance.
        name: The UDT name.

    Returns:
        A list of member dictionaries (same format as ``get_udt_info``
        members), filtered to exclude hidden members.

    Raises:
        ValueError: If the UDT is not found.
    """
    udt_elem = _find_udt_element(project, name)
    if udt_elem is None:
        raise ValueError(f"UDT '{name}' not found in the project.")

    members: list[dict] = []
    members_container = udt_elem.find("Members")
    if members_container is not None:
        for member in members_container.findall("Member"):
            parsed = _parse_member_element(member)
            if not parsed["hidden"]:
                members.append(parsed)

    return members


def get_udt_all_members(project, name: str) -> list[dict]:
    """Get ALL members of a UDT, including hidden backing fields.

    This includes the hidden ``SINT`` backing members that Rockwell uses
    for bit-packed BOOL storage (with the ``ZZZZZZZZZZ`` prefix).

    Args:
        project: The L5XProject instance.
        name: The UDT name.

    Returns:
        A list of all member dictionaries, including hidden members.

    Raises:
        ValueError: If the UDT is not found.
    """
    udt_elem = _find_udt_element(project, name)
    if udt_elem is None:
        raise ValueError(f"UDT '{name}' not found in the project.")

    members: list[dict] = []
    members_container = udt_elem.find("Members")
    if members_container is not None:
        for member in members_container.findall("Member"):
            members.append(_parse_member_element(member))

    return members


def list_udt_dependencies(project, name: str) -> list[str]:
    """List other UDTs that this UDT references in its member definitions.

    Scans all member DataType attributes and returns those that are not
    base types, built-in structures, or the UDT itself.

    Args:
        project: The L5XProject instance.
        name: The UDT name.

    Returns:
        A sorted list of UDT names that this UDT depends on.

    Raises:
        ValueError: If the UDT is not found.
    """
    udt_elem = _find_udt_element(project, name)
    if udt_elem is None:
        raise ValueError(f"UDT '{name}' not found in the project.")

    dependencies: set[str] = set()
    members_container = udt_elem.find("Members")
    if members_container is not None:
        for member in members_container.findall("Member"):
            dt = member.get("DataType", "")
            if not dt:
                continue
            if _is_base_or_builtin_type(dt):
                continue
            if dt.lower() == name.lower():
                # Self-reference (unusual but possible in some contexts)
                continue
            dependencies.add(dt)

    return sorted(dependencies)
