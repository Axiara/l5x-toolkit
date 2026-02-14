"""
Add-On Instruction (AOI) import and query operations for L5X files.

Provides functions to import AOI definitions from standalone L5X export files,
query AOI metadata (parameters, local tags, routines), analyze dependencies,
and generate instruction call text for embedding AOIs in rung logic.

AOIs in L5X files live under::

    Controller / AddOnInstructionDefinitions / AddOnInstructionDefinition

Each AOI definition contains Parameters, LocalTags, and Routines sub-elements.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from lxml import etree

from .schema import (
    BASE_DATA_TYPES,
    BUILTIN_STRUCTURES,
    CONTROLLER_CHILD_ORDER,
    VALID_PARAMETER_USAGE,
)
from .utils import (
    deep_copy,
    find_or_create,
    get_description,
    insert_in_order,
    parse_l5x,
    set_cdata_text,
)


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


def _get_aoi_definitions_container(project) -> etree._Element:
    """Return (or create) the AddOnInstructionDefinitions container element.

    Args:
        project: An L5XProject instance.

    Returns:
        The ``<AddOnInstructionDefinitions>`` element.
    """
    controller = _get_controller(project)
    container = controller.find("AddOnInstructionDefinitions")
    if container is None:
        container = etree.Element("AddOnInstructionDefinitions")
        insert_in_order(controller, container, CONTROLLER_CHILD_ORDER)
    return container


def _find_aoi_element(project, name: str) -> Optional[etree._Element]:
    """Find an AddOnInstructionDefinition element by name.

    Args:
        project: An L5XProject instance.
        name: The AOI name to search for (case-insensitive).

    Returns:
        The matching element, or ``None`` if not found.
    """
    container = _get_controller(project).find("AddOnInstructionDefinitions")
    if container is None:
        return None
    for aoi_elem in container.findall("AddOnInstructionDefinition"):
        if aoi_elem.get("Name", "").lower() == name.lower():
            return aoi_elem
    return None


def _update_edited_date(aoi_element: etree._Element) -> None:
    """Set the EditedDate attribute to the current UTC time.

    Studio 5000 uses this timestamp to determine whether an imported AOI
    should be accepted.  Without a recent timestamp the import may be
    silently rejected.

    Args:
        aoi_element: The ``<AddOnInstructionDefinition>`` element to update.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    aoi_element.set("EditedDate", now)


def _is_base_or_builtin_type(data_type: str) -> bool:
    """Return True if *data_type* is a base atomic type or a built-in
    structure (TIMER, COUNTER, etc.)."""
    return (
        data_type.upper() in BASE_DATA_TYPES
        or data_type.upper() in {k.upper() for k in BUILTIN_STRUCTURES}
        or data_type.upper() in {"BIT", "STRING"}
    )


def _extract_referenced_types(element: etree._Element) -> set[str]:
    """Scan an element tree for DataType attribute references and return
    the set of non-base type names found.

    Looks at Parameters, LocalTags, and Member elements to find DataType
    attributes that reference user-defined types.

    Args:
        element: The root element to scan (typically an AOI definition).

    Returns:
        Set of user-defined type names referenced by the element.
    """
    types_found: set[str] = set()
    # Check Parameter elements
    for param in element.iter("Parameter"):
        dt = param.get("DataType", "")
        if dt and not _is_base_or_builtin_type(dt):
            types_found.add(dt)
    # Check LocalTag elements
    for local_tag in element.iter("LocalTag"):
        dt = local_tag.get("DataType", "")
        if dt and not _is_base_or_builtin_type(dt):
            types_found.add(dt)
    # Check Member elements (within UDTs embedded in AOI)
    for member in element.iter("Member"):
        dt = member.get("DataType", "")
        if dt and not _is_base_or_builtin_type(dt):
            types_found.add(dt)
    return types_found


def _extract_referenced_aois(element: etree._Element) -> set[str]:
    """Scan routine rung text in an element for references to other AOI names.

    This is a heuristic: it looks at rung instruction text for instruction
    names that are NOT standard instructions (i.e., potential AOI calls).

    Args:
        element: The root element to scan.

    Returns:
        Set of potential AOI names referenced in rung text.
    """
    from .schema import INSTRUCTION_CATALOG
    from .rungs import tokenize, TokenType

    aoi_refs: set[str] = set()
    standard_instructions = {k.upper() for k in INSTRUCTION_CATALOG}

    for text_elem in element.iter("Text"):
        text = text_elem.text
        if text is None:
            continue
        # Strip CDATA wrapper if present in raw text
        if hasattr(text, 'strip'):
            text = text.strip()
        if not text:
            continue
        try:
            tokens = tokenize(text)
            for token in tokens:
                if token.type == TokenType.INSTRUCTION:
                    if token.value.upper() not in standard_instructions:
                        aoi_refs.add(token.value)
        except Exception:
            # Tokenization failures should not prevent dependency analysis
            pass

    return aoi_refs


def _import_dependent_udts(project, source_root: etree._Element) -> list[str]:
    """Import any UDT definitions found in the source file into the project.

    Args:
        project: The target L5XProject instance.
        source_root: The root element of the source L5X file.

    Returns:
        List of UDT names that were imported.
    """
    imported: list[str] = []
    controller = _get_controller(project)
    datatypes_container = find_or_create(controller, "DataTypes")

    source_controller = source_root.find("Controller")
    if source_controller is None:
        return imported

    source_datatypes = source_controller.find("DataTypes")
    if source_datatypes is None:
        return imported

    existing_names = {
        dt.get("Name", "").lower()
        for dt in datatypes_container.findall("DataType")
    }

    for dt_elem in source_datatypes.findall("DataType"):
        dt_name = dt_elem.get("Name", "")
        if dt_name.lower() not in existing_names:
            cloned = deep_copy(dt_elem)
            datatypes_container.append(cloned)
            existing_names.add(dt_name.lower())
            imported.append(dt_name)

    return imported


def _import_dependent_aois(
    project, source_root: etree._Element, exclude_name: str
) -> list[str]:
    """Import any dependent AOI definitions from the source file, excluding
    the primary AOI being imported.

    Args:
        project: The target L5XProject instance.
        source_root: The root element of the source L5X file.
        exclude_name: Name of the primary AOI (will not be imported here).

    Returns:
        List of AOI names that were imported as dependencies.
    """
    imported: list[str] = []
    container = _get_aoi_definitions_container(project)

    source_controller = source_root.find("Controller")
    if source_controller is None:
        return imported

    source_aoi_container = source_controller.find("AddOnInstructionDefinitions")
    if source_aoi_container is None:
        return imported

    existing_names = {
        aoi_el.get("Name", "").lower()
        for aoi_el in container.findall("AddOnInstructionDefinition")
    }

    for aoi_elem in source_aoi_container.findall("AddOnInstructionDefinition"):
        aoi_name = aoi_elem.get("Name", "")
        if aoi_name.lower() == exclude_name.lower():
            continue
        if aoi_name.lower() not in existing_names:
            cloned = deep_copy(aoi_elem)
            _update_edited_date(cloned)
            container.append(cloned)
            existing_names.add(aoi_name.lower())
            imported.append(aoi_name)

    return imported


def _parse_parameter_element(param: etree._Element) -> dict:
    """Parse a single Parameter element into a dictionary.

    Args:
        param: The ``<Parameter>`` element.

    Returns:
        A dictionary with parameter metadata.
    """
    info: dict[str, Any] = {
        "name": param.get("Name", ""),
        "data_type": param.get("DataType", ""),
        "usage": param.get("Usage", ""),
        "required": param.get("Required", "false").lower() == "true",
        "visible": param.get("Visible", "true").lower() == "true",
        "description": None,
        "default_value": None,
    }

    # Extract description
    desc_elem = param.find("Description")
    if desc_elem is not None:
        info["description"] = get_description(param)

    # Extract default value from Decorated format
    decorated = None
    for dd in param.findall("DefaultData"):
        if dd.get("Format") == "Decorated":
            decorated = dd
            break

    if decorated is not None:
        data_value = decorated.find("DataValue")
        if data_value is not None:
            info["default_value"] = data_value.get("Value")
        # For array or structure defaults, capture the raw XML text
        if info["default_value"] is None:
            array_elem = decorated.find("Array")
            if array_elem is not None:
                info["default_value"] = etree.tostring(
                    array_elem, encoding="unicode"
                ).strip()
            structure_elem = decorated.find("Structure")
            if structure_elem is not None:
                info["default_value"] = etree.tostring(
                    structure_elem, encoding="unicode"
                ).strip()
    else:
        # Fall back to L5K format
        for dd in param.findall("DefaultData"):
            if dd.get("Format") == "L5K":
                raw_text = dd.text
                if raw_text is not None:
                    info["default_value"] = raw_text.strip()
                break

    return info


def _parse_local_tag_element(local_tag: etree._Element) -> dict:
    """Parse a single LocalTag element into a dictionary.

    Args:
        local_tag: The ``<LocalTag>`` element.

    Returns:
        A dictionary with local tag metadata.
    """
    info: dict[str, Any] = {
        "name": local_tag.get("Name", ""),
        "data_type": local_tag.get("DataType", ""),
        "dimension": local_tag.get("Dimensions", "0"),
        "radix": local_tag.get("Radix", ""),
        "external_access": local_tag.get("ExternalAccess", "Read/Write"),
        "description": get_description(local_tag),
    }
    return info


def _parse_routine_element(routine: etree._Element) -> dict:
    """Parse a single Routine element into a summary dictionary.

    Args:
        routine: The ``<Routine>`` element.

    Returns:
        A dictionary with routine metadata.
    """
    rung_count = 0
    rll_content = routine.find("RLLContent")
    if rll_content is not None:
        rung_count = len(rll_content.findall("Rung"))

    st_content = routine.find("STContent")
    line_count = 0
    if st_content is not None:
        for line in st_content.findall("Line"):
            line_count += 1

    info: dict[str, Any] = {
        "name": routine.get("Name", ""),
        "type": routine.get("Type", ""),
        "description": get_description(routine),
        "rung_count": rung_count,
        "st_line_count": line_count,
    }
    return info


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def import_aoi(
    project,
    file_path: str,
    overwrite: bool = False,
) -> etree._Element:
    """Import an AOI definition from an L5X export file.

    The source file should have ``TargetType="AddOnInstructionDefinition"``.
    Any dependent UDTs and AOIs found in the source file are also imported
    into the project if they do not already exist.

    The ``EditedDate`` attribute on the imported AOI is updated to the
    current UTC time to ensure Studio 5000 accepts the import without
    complaining about stale timestamps.

    Args:
        project: The target L5XProject instance.
        file_path: Path to the L5X file containing the AOI definition.
        overwrite: If ``False`` (default) and an AOI with the same name
            already exists in the project, a ``ValueError`` is raised.
            If ``True``, the existing definition is replaced.

    Returns:
        The imported ``<AddOnInstructionDefinition>`` element (as inserted
        into the project tree).

    Raises:
        FileNotFoundError: If *file_path* does not exist.
        ValueError: If the source file does not contain an AOI definition,
            or if ``overwrite`` is False and the AOI already exists.
        etree.XMLSyntaxError: If the source file contains malformed XML.
    """
    source_root = parse_l5x(file_path)

    # Locate the AOI definition in the source file.
    # It may be directly under Controller/AddOnInstructionDefinitions,
    # or at the top level for single-definition exports.
    aoi_elem = None
    source_controller = source_root.find("Controller")
    if source_controller is not None:
        aoi_container = source_controller.find("AddOnInstructionDefinitions")
        if aoi_container is not None:
            aoi_elems = aoi_container.findall("AddOnInstructionDefinition")
            if aoi_elems:
                aoi_elem = aoi_elems[0]

    # Some export formats place the definition at the root level
    if aoi_elem is None:
        aoi_elem = source_root.find(".//AddOnInstructionDefinition")

    if aoi_elem is None:
        raise ValueError(
            f"No AddOnInstructionDefinition found in '{file_path}'. "
            "Ensure the file was exported with "
            "TargetType='AddOnInstructionDefinition'."
        )

    aoi_name = aoi_elem.get("Name", "")
    if not aoi_name:
        raise ValueError(
            f"AddOnInstructionDefinition in '{file_path}' has no Name attribute."
        )

    # Check for existing definition
    existing = _find_aoi_element(project, aoi_name)
    if existing is not None and not overwrite:
        raise ValueError(
            f"AOI '{aoi_name}' already exists in the project. "
            "Use overwrite=True to replace it."
        )

    # Import dependent UDTs first
    _import_dependent_udts(project, source_root)

    # Import dependent AOIs (excluding the primary one)
    _import_dependent_aois(project, source_root, aoi_name)

    # Clone and prepare the AOI element
    cloned_aoi = deep_copy(aoi_elem)
    _update_edited_date(cloned_aoi)

    # Insert or replace in the project
    container = _get_aoi_definitions_container(project)
    if existing is not None and overwrite:
        parent = existing.getparent()
        if parent is not None:
            idx = list(parent).index(existing)
            parent.remove(existing)
            parent.insert(idx, cloned_aoi)
        else:
            container.append(cloned_aoi)
    else:
        container.append(cloned_aoi)

    return cloned_aoi


def get_aoi_info(project, name: str) -> dict:
    """Get detailed information about an AOI.

    Args:
        project: The L5XProject instance.
        name: The AOI name.

    Returns:
        A dictionary containing:
            - ``name`` (str): The AOI name.
            - ``revision`` (str): The revision string (e.g., ``"1.0"``).
            - ``class_`` (str): The AOI class (``"Standard"`` or ``"Safety"``).
            - ``description`` (str or None): The AOI description text.
            - ``created_date`` (str): ISO timestamp of creation.
            - ``edited_date`` (str): ISO timestamp of last edit.
            - ``created_by`` (str): Author who created the AOI.
            - ``edited_by`` (str): Author who last edited the AOI.
            - ``software_revision`` (str): The Studio 5000 version.
            - ``execute_prescan`` (bool): Whether the Prescan routine runs.
            - ``execute_postscan`` (bool): Whether the Postscan routine runs.
            - ``execute_enable_in_false`` (bool): Whether logic runs on
              EnableIn false.
            - ``parameters`` (list[dict]): Parameter definitions.
            - ``local_tags`` (list[dict]): Local tag definitions.
            - ``routines`` (list[dict]): Routine summaries.

    Raises:
        ValueError: If the AOI is not found.
    """
    aoi_elem = _find_aoi_element(project, name)
    if aoi_elem is None:
        raise ValueError(f"AOI '{name}' not found in the project.")

    info: dict[str, Any] = {
        "name": aoi_elem.get("Name", ""),
        "revision": aoi_elem.get("Revision", ""),
        "class_": aoi_elem.get("Class", "Standard"),
        "description": get_description(aoi_elem),
        "created_date": aoi_elem.get("CreatedDate", ""),
        "edited_date": aoi_elem.get("EditedDate", ""),
        "created_by": aoi_elem.get("CreatedBy", ""),
        "edited_by": aoi_elem.get("EditedBy", ""),
        "software_revision": aoi_elem.get("SoftwareRevision", ""),
        "execute_prescan": (
            aoi_elem.get("ExecutePrescan", "false").lower() == "true"
        ),
        "execute_postscan": (
            aoi_elem.get("ExecutePostscan", "false").lower() == "true"
        ),
        "execute_enable_in_false": (
            aoi_elem.get("ExecuteEnableInFalse", "false").lower() == "true"
        ),
        "parameters": [],
        "local_tags": [],
        "routines": [],
    }

    # Parameters
    params_container = aoi_elem.find("Parameters")
    if params_container is not None:
        for param in params_container.findall("Parameter"):
            info["parameters"].append(_parse_parameter_element(param))

    # Local tags
    local_tags_container = aoi_elem.find("LocalTags")
    if local_tags_container is not None:
        for local_tag in local_tags_container.findall("LocalTag"):
            info["local_tags"].append(_parse_local_tag_element(local_tag))

    # Routines
    routines_container = aoi_elem.find("Routines")
    if routines_container is not None:
        for routine in routines_container.findall("Routine"):
            info["routines"].append(_parse_routine_element(routine))

    return info


def get_aoi_parameters(project, name: str) -> list[dict]:
    """Get the parameter list for an AOI.

    Returns a list of dictionaries, one per parameter, each containing:

    - ``name`` (str): Parameter name (e.g., ``"EnableIn"``, ``"MyInput"``).
    - ``data_type`` (str): The data type (e.g., ``"BOOL"``, ``"DINT"``).
    - ``usage`` (str): ``"Input"``, ``"Output"``, or ``"InOut"``.
    - ``required`` (bool): Whether the parameter must be wired.
    - ``visible`` (bool): Whether the parameter appears on the instruction.
    - ``description`` (str or None): Parameter description text.
    - ``default_value`` (str or None): The default value string.

    Args:
        project: The L5XProject instance.
        name: The AOI name.

    Returns:
        List of parameter dictionaries.

    Raises:
        ValueError: If the AOI is not found.
    """
    aoi_elem = _find_aoi_element(project, name)
    if aoi_elem is None:
        raise ValueError(f"AOI '{name}' not found in the project.")

    params: list[dict] = []
    params_container = aoi_elem.find("Parameters")
    if params_container is not None:
        for param in params_container.findall("Parameter"):
            params.append(_parse_parameter_element(param))

    return params


def list_aoi_dependencies(project, name: str) -> dict:
    """List UDTs and other AOIs that this AOI depends on.

    Scans the AOI's parameters, local tags, and routine rung text to identify
    references to user-defined data types and other Add-On Instructions.

    Args:
        project: The L5XProject instance.
        name: The AOI name.

    Returns:
        A dictionary with two keys:
            - ``data_types`` (list[str]): Names of UDTs referenced by this AOI.
            - ``aois`` (list[str]): Names of other AOIs called in this AOI's
              routines.

    Raises:
        ValueError: If the AOI is not found.
    """
    aoi_elem = _find_aoi_element(project, name)
    if aoi_elem is None:
        raise ValueError(f"AOI '{name}' not found in the project.")

    # Find referenced data types (UDTs)
    referenced_types = _extract_referenced_types(aoi_elem)

    # Separate into UDTs defined in the project vs. AOI types
    # A type name that matches an existing AOI is an AOI reference in the
    # type system; otherwise it is a UDT.
    controller = _get_controller(project)
    existing_aois: set[str] = set()
    aoi_container = controller.find("AddOnInstructionDefinitions")
    if aoi_container is not None:
        for aoi_def in aoi_container.findall("AddOnInstructionDefinition"):
            existing_aois.add(aoi_def.get("Name", ""))

    # Data types that are actually AOIs used as parameter types
    aoi_type_refs = referenced_types & existing_aois
    udt_refs = referenced_types - existing_aois - {name}

    # Find AOI references in rung text
    rung_aoi_refs = _extract_referenced_aois(aoi_elem)
    # Remove self-references
    rung_aoi_refs.discard(name)

    # Combine AOI references
    all_aoi_refs = (aoi_type_refs | rung_aoi_refs) - {name}

    return {
        "data_types": sorted(udt_refs),
        "aois": sorted(all_aoi_refs),
    }


def generate_aoi_call_text(
    project,
    aoi_name: str,
    instance_tag: str,
    param_map: Optional[dict] = None,
) -> str:
    """Generate the rung instruction text for calling an AOI.

    Produces a string suitable for embedding in a rung's ``<Text>`` element.
    The call format follows Rockwell conventions::

        AOIName(InstanceTag,Param1Value,Param2Value,...);

    Parameters are emitted in the order they appear in the AOI definition.
    The ``EnableIn`` and ``EnableOut`` system parameters are excluded from the
    argument list (they are handled implicitly by the runtime).

    For parameters not provided in *param_map*:

    - Required or InOut parameters raise a ``ValueError`` (they must be wired).
    - Optional input/output parameters are filled with ``?`` (Studio 5000
      placeholder for "use default").

    Args:
        project: The L5XProject instance.
        aoi_name: Name of the AOI to call.
        instance_tag: The tag that holds the AOI instance data
            (e.g., ``"MyConveyor_Controller"``).
        param_map: Optional dictionary mapping parameter names to tag
            references or literal values.  Keys are parameter names; values
            are the tag/literal strings to wire to each parameter.

    Returns:
        A semicolon-terminated instruction string, e.g.::

            MyAOI(Instance,Param1,Param2,?,?);

    Raises:
        ValueError: If the AOI is not found, or if a required/InOut parameter
            is missing from *param_map*.
    """
    if param_map is None:
        param_map = {}

    aoi_elem = _find_aoi_element(project, aoi_name)
    if aoi_elem is None:
        raise ValueError(f"AOI '{aoi_name}' not found in the project.")

    # Gather parameters in definition order, excluding EnableIn/EnableOut
    params_container = aoi_elem.find("Parameters")
    ordered_params: list[dict] = []
    if params_container is not None:
        for param in params_container.findall("Parameter"):
            p_name = param.get("Name", "")
            if p_name.lower() in ("enablein", "enableout"):
                continue
            ordered_params.append({
                "name": p_name,
                "usage": param.get("Usage", "Input"),
                "required": param.get("Required", "false").lower() == "true",
                "data_type": param.get("DataType", ""),
            })

    # Build the argument list
    arg_values: list[str] = [instance_tag]
    missing_required: list[str] = []

    for p in ordered_params:
        p_name = p["name"]
        usage = p["usage"]
        is_required = p["required"]

        if p_name in param_map:
            arg_values.append(str(param_map[p_name]))
        elif usage == "InOut":
            # InOut parameters must always be wired
            missing_required.append(f"{p_name} (InOut)")
        elif is_required:
            missing_required.append(f"{p_name} (Required {usage})")
        else:
            # Optional parameter -- use placeholder
            arg_values.append("?")

    if missing_required:
        raise ValueError(
            f"AOI '{aoi_name}' call is missing required parameters: "
            + ", ".join(missing_required)
        )

    # Format: AOIName(instance,arg1,arg2,...);
    args_str = ",".join(arg_values)
    return f"{aoi_name}({args_str});"
