"""
Pre-flight validation engine for L5X projects.

Performs structural, semantic, and referential integrity checks on an L5X
project tree before writing to disk.  Catching errors here prevents Studio
5000 from rejecting an import or, worse, silently corrupting data.

Validation is organized into focused check functions that each return a
:class:`ValidationResult`.  The top-level :func:`validate_project` aggregates
all checks into a single result.

Error severity:
    - **errors**: Fatal issues that will almost certainly cause Studio 5000
      to reject the file or produce incorrect behavior.
    - **warnings**: Non-fatal issues that may cause unexpected behavior
      or indicate sloppy construction but will not prevent import.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from lxml import etree

from .schema import (
    BASE_DATA_TYPES,
    BUILTIN_STRUCTURES,
    CONTROLLER_CHILD_ORDER,
    INSTRUCTION_CATALOG,
    MAX_TAG_NAME_LENGTH,
    TAG_NAME_PATTERN,
    VALID_EXTERNAL_ACCESS,
    VALID_PARAMETER_USAGE,
    VALID_ROUTINE_TYPES,
    VALID_RUNG_TYPES,
    VALID_TASK_TYPES,
)
from .utils import get_description
from .rungs import validate_rung_syntax, extract_tag_references


# ---------------------------------------------------------------------------
# Validation result container
# ---------------------------------------------------------------------------

class ValidationResult:
    """Container for validation results.

    Collects errors (fatal) and warnings (non-fatal) discovered during
    validation.  The :attr:`is_valid` property indicates whether the project
    passes validation (no errors, though warnings may be present).

    Usage::

        result = validate_project(project)
        if not result.is_valid:
            for err in result.errors:
                print(f"ERROR: {err}")
        for warn in result.warnings:
            print(f"WARNING: {warn}")
    """

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    @property
    def is_valid(self) -> bool:
        """Return True if no errors were recorded."""
        return len(self.errors) == 0

    def add_error(self, message: str) -> None:
        """Record a fatal validation error.

        Args:
            message: A human-readable description of the error.
        """
        self.errors.append(message)

    def add_warning(self, message: str) -> None:
        """Record a non-fatal validation warning.

        Args:
            message: A human-readable description of the warning.
        """
        self.warnings.append(message)

    def merge(self, other: "ValidationResult") -> None:
        """Merge another ValidationResult into this one.

        All errors and warnings from *other* are appended to this result.

        Args:
            other: The ValidationResult to merge in.
        """
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)

    def __str__(self) -> str:
        lines: list[str] = []
        if self.errors:
            lines.append(f"=== ERRORS ({len(self.errors)}) ===")
            for i, err in enumerate(self.errors, 1):
                lines.append(f"  {i}. {err}")
        if self.warnings:
            lines.append(f"=== WARNINGS ({len(self.warnings)}) ===")
            for i, warn in enumerate(self.warnings, 1):
                lines.append(f"  {i}. {warn}")
        if not self.errors and not self.warnings:
            lines.append("Validation passed: no errors or warnings.")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"ValidationResult(errors={len(self.errors)}, "
            f"warnings={len(self.warnings)})"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_TAG_NAME_RE = re.compile(TAG_NAME_PATTERN)


def _get_controller(project) -> Optional[etree._Element]:
    """Return the Controller element, or None if missing."""
    return project.root.find("Controller")


def _collect_all_defined_tag_names(project) -> set[str]:
    """Collect all tag names from controller scope and all program scopes.

    Returns:
        A set of all tag base names defined anywhere in the project.
    """
    names: set[str] = set()
    controller = _get_controller(project)
    if controller is None:
        return names

    # Controller tags
    tags_elem = controller.find("Tags")
    if tags_elem is not None:
        for tag in tags_elem.findall("Tag"):
            tag_name = tag.get("Name", "")
            if tag_name:
                names.add(tag_name)

    # Program tags
    programs_elem = controller.find("Programs")
    if programs_elem is not None:
        for program in programs_elem.findall("Program"):
            prog_tags = program.find("Tags")
            if prog_tags is not None:
                for tag in prog_tags.findall("Tag"):
                    tag_name = tag.get("Name", "")
                    if tag_name:
                        names.add(tag_name)

    return names


def _collect_all_defined_type_names(project) -> set[str]:
    """Collect all data type names: base types, built-in structures, and UDTs.

    Returns:
        A set of all known data type names (upper-cased for comparison).
    """
    types: set[str] = set()

    # Base types
    for dt in BASE_DATA_TYPES:
        types.add(dt.upper())

    # Built-in structures
    for name in BUILTIN_STRUCTURES:
        types.add(name.upper())

    # Additional system types
    types.add("BIT")
    types.add("STRING")

    # User-defined types from the project
    controller = _get_controller(project)
    if controller is not None:
        datatypes = controller.find("DataTypes")
        if datatypes is not None:
            for dt in datatypes.findall("DataType"):
                dt_name = dt.get("Name", "")
                if dt_name:
                    types.add(dt_name.upper())

    # AOI names also function as data types (for AOI instance tags)
    if controller is not None:
        aoi_container = controller.find("AddOnInstructionDefinitions")
        if aoi_container is not None:
            for aoi in aoi_container.findall("AddOnInstructionDefinition"):
                aoi_name = aoi.get("Name", "")
                if aoi_name:
                    types.add(aoi_name.upper())

    return types


def _collect_all_aoi_names(project) -> set[str]:
    """Collect all AOI names defined in the project.

    Returns:
        A set of AOI names.
    """
    names: set[str] = set()
    controller = _get_controller(project)
    if controller is None:
        return names
    aoi_container = controller.find("AddOnInstructionDefinitions")
    if aoi_container is None:
        return names
    for aoi in aoi_container.findall("AddOnInstructionDefinition"):
        aoi_name = aoi.get("Name", "")
        if aoi_name:
            names.add(aoi_name)
    return names


def _collect_all_module_names(project) -> set[str]:
    """Collect all module names defined in the project.

    Returns:
        A set of module names.
    """
    names: set[str] = set()
    controller = _get_controller(project)
    if controller is None:
        return names
    modules = controller.find("Modules")
    if modules is None:
        return names
    for module in modules.findall("Module"):
        mod_name = module.get("Name", "")
        if mod_name:
            names.add(mod_name)
    return names


def _collect_program_names(project) -> set[str]:
    """Collect all program names defined in the project."""
    names: set[str] = set()
    controller = _get_controller(project)
    if controller is None:
        return names
    programs = controller.find("Programs")
    if programs is None:
        return names
    for prog in programs.findall("Program"):
        prog_name = prog.get("Name", "")
        if prog_name:
            names.add(prog_name)
    return names


# ---------------------------------------------------------------------------
# Individual validation checks
# ---------------------------------------------------------------------------

def validate_structure(project) -> ValidationResult:
    """Check structural correctness of the project.

    Validates:
        1. Root element is ``RSLogix5000Content``.
        2. A ``Controller`` element exists.
        3. Required child elements of Controller are present.
        4. Child element ordering matches the canonical L5X sequence.

    Args:
        project: The L5XProject instance.

    Returns:
        A ValidationResult with any structural issues found.
    """
    result = ValidationResult()

    # Check root element
    if project.root.tag != "RSLogix5000Content":
        result.add_error(
            f"Root element is '{project.root.tag}', "
            "expected 'RSLogix5000Content'."
        )
        return result

    # Check Controller exists
    controller = _get_controller(project)
    if controller is None:
        result.add_error("No <Controller> element found.")
        return result

    # Check for required containers (at minimum, Tags and Programs should exist
    # for a functional project)
    required_containers = ["Tags", "Programs", "Tasks"]
    for container_name in required_containers:
        if controller.find(container_name) is None:
            result.add_warning(
                f"Missing <{container_name}> container element under Controller. "
                "This may indicate an incomplete project."
            )

    # Check child element ordering
    child_tags = [child.tag for child in controller]
    order_map = {name: idx for idx, name in enumerate(CONTROLLER_CHILD_ORDER)}

    last_pos = -1
    last_tag = None
    for tag in child_tags:
        pos = order_map.get(tag)
        if pos is not None:
            if pos < last_pos:
                result.add_error(
                    f"Controller child element <{tag}> appears after "
                    f"<{last_tag}>, but the canonical L5X order requires "
                    f"<{tag}> to come first. Studio 5000 may reject this file."
                )
            if pos >= last_pos:
                last_pos = pos
                last_tag = tag

    # Check that DataTypes element (if present) contains no duplicate names
    datatypes = controller.find("DataTypes")
    if datatypes is not None:
        dt_names: dict[str, int] = {}
        for dt in datatypes.findall("DataType"):
            dt_name = dt.get("Name", "").lower()
            if dt_name:
                dt_names[dt_name] = dt_names.get(dt_name, 0) + 1
        for dt_name, count in dt_names.items():
            if count > 1:
                result.add_error(
                    f"Duplicate DataType definition: '{dt_name}' "
                    f"appears {count} times."
                )

    return result


def validate_references(project) -> ValidationResult:
    """Check all cross-references in the project.

    Validates:
        1. Tags referenced in rung text exist in the appropriate scope.
        2. Data types used by tags are defined (UDTs, AOIs, or base types).
        3. AOI data types used by tags reference existing AOI definitions.

    Args:
        project: The L5XProject instance.

    Returns:
        A ValidationResult with any reference issues found.
    """
    result = ValidationResult()
    controller = _get_controller(project)
    if controller is None:
        result.add_error("No <Controller> element found.")
        return result

    all_types = _collect_all_defined_type_names(project)
    controller_tags = set()
    aoi_names = _collect_all_aoi_names(project)

    # Collect controller-scoped tags
    tags_elem = controller.find("Tags")
    if tags_elem is not None:
        for tag in tags_elem.findall("Tag"):
            tag_name = tag.get("Name", "")
            if tag_name:
                controller_tags.add(tag_name)

    # Validate data types on controller tags
    if tags_elem is not None:
        for tag in tags_elem.findall("Tag"):
            tag_name = tag.get("Name", "")
            dt = tag.get("DataType", "")
            if dt and dt.upper() not in all_types:
                result.add_error(
                    f"Controller tag '{tag_name}' uses undefined data type "
                    f"'{dt}'."
                )

    # Validate programs and their tags/rungs
    programs = controller.find("Programs")
    if programs is not None:
        for program in programs.findall("Program"):
            prog_name = program.get("Name", "")
            prog_tags: set[str] = set()

            # Collect program-scoped tags
            prog_tags_elem = program.find("Tags")
            if prog_tags_elem is not None:
                for tag in prog_tags_elem.findall("Tag"):
                    tag_name = tag.get("Name", "")
                    if tag_name:
                        prog_tags.add(tag_name)

                # Validate data types on program tags
                for tag in prog_tags_elem.findall("Tag"):
                    tag_name = tag.get("Name", "")
                    dt = tag.get("DataType", "")
                    if dt and dt.upper() not in all_types:
                        result.add_error(
                            f"Program '{prog_name}' tag '{tag_name}' uses "
                            f"undefined data type '{dt}'."
                        )

            # Combined tag scope for rung validation: program + controller
            available_tags = prog_tags | controller_tags

            # Also add AOI names as valid instruction targets
            # (not tag references, but they appear as instruction names)
            available_instructions = {k.upper() for k in INSTRUCTION_CATALOG}
            available_instructions.update(n.upper() for n in aoi_names)

            # Validate rungs
            routines = program.find("Routines")
            if routines is not None:
                for routine in routines.findall("Routine"):
                    routine_name = routine.get("Name", "")
                    rll_content = routine.find("RLLContent")
                    if rll_content is not None:
                        for rung in rll_content.findall("Rung"):
                            text_elem = rung.find("Text")
                            if text_elem is None:
                                continue
                            rung_text = text_elem.text
                            if rung_text is None:
                                continue
                            rung_text = rung_text.strip()
                            if not rung_text or rung_text == ";":
                                continue

                            # Extract tag references and check availability
                            referenced = extract_tag_references(rung_text)
                            for ref_name in referenced:
                                if ref_name not in available_tags:
                                    result.add_warning(
                                        f"Program '{prog_name}', routine "
                                        f"'{routine_name}': rung references "
                                        f"tag '{ref_name}' which is not "
                                        f"defined in this scope."
                                    )

    return result


def validate_tag(project, tag_element: etree._Element) -> ValidationResult:
    """Validate a single tag element.

    Checks:
        1. Name is present and valid.
        2. DataType is specified and known.
        3. Value matches the data type (basic type checking).
        4. Both L5K and Decorated data formats are present (if tag has data).
        5. ExternalAccess is a valid value.

    Args:
        project: The L5XProject instance (for type resolution).
        tag_element: The ``<Tag>`` element to validate.

    Returns:
        A ValidationResult for this tag.
    """
    result = ValidationResult()
    all_types = _collect_all_defined_type_names(project)

    tag_name = tag_element.get("Name", "")
    tag_type = tag_element.get("TagType", "Base")
    data_type = tag_element.get("DataType", "")

    # Name validation
    if not tag_name:
        result.add_error("Tag element has no Name attribute.")
    else:
        if len(tag_name) > MAX_TAG_NAME_LENGTH:
            result.add_error(
                f"Tag '{tag_name}' name exceeds {MAX_TAG_NAME_LENGTH} "
                f"characters (length: {len(tag_name)})."
            )
        if not _TAG_NAME_RE.match(tag_name):
            result.add_error(
                f"Tag '{tag_name}' contains invalid characters. "
                "Names must start with a letter or underscore and contain "
                "only letters, digits, and underscores."
            )

    # DataType validation (not required for alias tags)
    if tag_type != "Alias":
        if not data_type:
            result.add_error(
                f"Tag '{tag_name}' has no DataType attribute."
            )
        elif data_type.upper() not in all_types:
            result.add_error(
                f"Tag '{tag_name}' uses undefined data type '{data_type}'."
            )

    # ExternalAccess validation
    ext_access = tag_element.get("ExternalAccess", "")
    if ext_access and ext_access not in VALID_EXTERNAL_ACCESS:
        result.add_warning(
            f"Tag '{tag_name}' has invalid ExternalAccess value "
            f"'{ext_access}'. Valid values: {VALID_EXTERNAL_ACCESS}"
        )

    # Check for data format completeness
    data_elem = tag_element.find("Data")
    if data_elem is not None:
        formats_found: set[str] = set()
        for child in data_elem:
            fmt = child.get("Format", child.tag)
            formats_found.add(fmt)

        # For non-alias tags with data, check both formats are present
        if tag_type != "Alias" and formats_found:
            if "L5K" not in formats_found:
                result.add_warning(
                    f"Tag '{tag_name}' is missing L5K data format. "
                    "Studio 5000 expects both L5K and Decorated formats."
                )
            if "Decorated" not in formats_found:
                result.add_warning(
                    f"Tag '{tag_name}' is missing Decorated data format. "
                    "Studio 5000 expects both L5K and Decorated formats."
                )

    # Basic type-value checks for atomic types
    if data_type.upper() in BASE_DATA_TYPES and tag_type == "Base":
        type_info = BASE_DATA_TYPES[data_type.upper()]
        data_elem = tag_element.find("Data")
        if data_elem is not None:
            # Check Decorated DataValue
            for child in data_elem:
                if child.get("Format") == "Decorated":
                    data_value = child.find("DataValue")
                    if data_value is not None:
                        val_str = data_value.get("Value", "")
                        if val_str and data_type.upper() in (
                            "SINT", "USINT", "INT", "UINT",
                            "DINT", "UDINT", "LINT"
                        ):
                            try:
                                int(val_str)
                            except ValueError:
                                result.add_error(
                                    f"Tag '{tag_name}' has value '{val_str}' "
                                    f"which is not a valid integer for type "
                                    f"'{data_type}'."
                                )
                        elif val_str and data_type.upper() in ("REAL", "LREAL"):
                            try:
                                float(val_str)
                            except ValueError:
                                result.add_error(
                                    f"Tag '{tag_name}' has value '{val_str}' "
                                    f"which is not a valid float for type "
                                    f"'{data_type}'."
                                )

    return result


def validate_rung(rung_text: str) -> ValidationResult:
    """Validate rung instruction syntax.

    Checks:
        1. Semicolon termination.
        2. Bracket matching (``[`` / ``]``).
        3. Parenthesis matching (``(`` / ``)``).

    This function validates syntax only -- it does not check whether
    tag references exist or whether instruction names are valid.

    Args:
        rung_text: The raw rung text string to validate.

    Returns:
        A ValidationResult for this rung.
    """
    result = ValidationResult()

    syntax_errors = validate_rung_syntax(rung_text)
    for err in syntax_errors:
        result.add_error(f"Rung syntax: {err}")

    return result


def validate_naming(project) -> ValidationResult:
    """Check for duplicate names and invalid characters across all scopes.

    Validates:
        1. No duplicate tag names within the same scope (controller or
           individual programs).
        2. No duplicate program names.
        3. No duplicate AOI names.
        4. No duplicate UDT names.
        5. All names conform to L5X naming rules (characters, length).

    Args:
        project: The L5XProject instance.

    Returns:
        A ValidationResult with any naming issues found.
    """
    result = ValidationResult()
    controller = _get_controller(project)
    if controller is None:
        result.add_error("No <Controller> element found.")
        return result

    def _check_name(name: str, context: str) -> None:
        """Validate a single name against L5X rules."""
        if not name:
            result.add_error(f"{context}: empty name.")
            return
        if len(name) > MAX_TAG_NAME_LENGTH:
            result.add_error(
                f"{context}: name '{name}' exceeds {MAX_TAG_NAME_LENGTH} "
                f"characters (length: {len(name)})."
            )
        if not _TAG_NAME_RE.match(name):
            result.add_error(
                f"{context}: name '{name}' contains invalid characters."
            )

    def _check_duplicates(
        elements: list[etree._Element], attr: str, context: str
    ) -> None:
        """Check for duplicate values of a given attribute."""
        seen: dict[str, int] = {}
        for elem in elements:
            name = elem.get(attr, "").lower()
            if name:
                seen[name] = seen.get(name, 0) + 1
        for name, count in seen.items():
            if count > 1:
                result.add_error(
                    f"{context}: duplicate name '{name}' "
                    f"({count} occurrences)."
                )

    # Controller tags
    tags_elem = controller.find("Tags")
    if tags_elem is not None:
        tag_elems = tags_elem.findall("Tag")
        _check_duplicates(tag_elems, "Name", "Controller tags")
        for tag in tag_elems:
            _check_name(tag.get("Name", ""), "Controller tag")

    # Programs
    programs = controller.find("Programs")
    if programs is not None:
        prog_elems = programs.findall("Program")
        _check_duplicates(prog_elems, "Name", "Programs")
        for program in prog_elems:
            prog_name = program.get("Name", "")
            _check_name(prog_name, "Program")

            # Program tags
            prog_tags = program.find("Tags")
            if prog_tags is not None:
                prog_tag_elems = prog_tags.findall("Tag")
                _check_duplicates(
                    prog_tag_elems, "Name", f"Program '{prog_name}' tags"
                )
                for tag in prog_tag_elems:
                    _check_name(
                        tag.get("Name", ""),
                        f"Program '{prog_name}' tag",
                    )

            # Routine names within program
            routines = program.find("Routines")
            if routines is not None:
                routine_elems = routines.findall("Routine")
                _check_duplicates(
                    routine_elems, "Name",
                    f"Program '{prog_name}' routines",
                )
                for routine in routine_elems:
                    _check_name(
                        routine.get("Name", ""),
                        f"Program '{prog_name}' routine",
                    )

    # AOI names
    aoi_container = controller.find("AddOnInstructionDefinitions")
    if aoi_container is not None:
        aoi_elems = aoi_container.findall("AddOnInstructionDefinition")
        _check_duplicates(aoi_elems, "Name", "AOIs")
        for aoi in aoi_elems:
            _check_name(aoi.get("Name", ""), "AOI")

    # UDT names
    datatypes = controller.find("DataTypes")
    if datatypes is not None:
        dt_elems = datatypes.findall("DataType")
        _check_duplicates(dt_elems, "Name", "DataTypes")
        for dt in dt_elems:
            _check_name(dt.get("Name", ""), "DataType")

    # Module names
    modules = controller.find("Modules")
    if modules is not None:
        mod_elems = modules.findall("Module")
        _check_duplicates(mod_elems, "Name", "Modules")
        for mod in mod_elems:
            mod_name = mod.get("Name", "")
            if mod_name:
                # Module names follow slightly different rules (can be longer)
                # but still must not be empty
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", mod_name):
                    result.add_warning(
                        f"Module name '{mod_name}' may contain "
                        "invalid characters."
                    )

    return result


def _validate_dependencies(project) -> ValidationResult:
    """Check that all dependency references resolve correctly.

    Validates:
        1. AOIs used by tags have definitions in the project.
        2. UDTs used by tags and AOIs have definitions in the project.
        3. Parent module references in the Modules section are valid.

    Args:
        project: The L5XProject instance.

    Returns:
        A ValidationResult with dependency issues.
    """
    result = ValidationResult()
    controller = _get_controller(project)
    if controller is None:
        return result

    all_types = _collect_all_defined_type_names(project)
    module_names = _collect_all_module_names(project)

    # Check module parent references
    modules = controller.find("Modules")
    if modules is not None:
        for module in modules.findall("Module"):
            mod_name = module.get("Name", "")
            parent_module = module.get("ParentModule", "")
            if parent_module and parent_module not in module_names:
                result.add_error(
                    f"Module '{mod_name}' references parent module "
                    f"'{parent_module}' which is not defined."
                )

    # Check AOI parameter and local tag types
    aoi_container = controller.find("AddOnInstructionDefinitions")
    if aoi_container is not None:
        for aoi in aoi_container.findall("AddOnInstructionDefinition"):
            aoi_name = aoi.get("Name", "")

            params = aoi.find("Parameters")
            if params is not None:
                for param in params.findall("Parameter"):
                    dt = param.get("DataType", "")
                    if dt and dt.upper() not in all_types:
                        result.add_error(
                            f"AOI '{aoi_name}' parameter "
                            f"'{param.get('Name', '')}' uses undefined "
                            f"data type '{dt}'."
                        )

            local_tags = aoi.find("LocalTags")
            if local_tags is not None:
                for lt in local_tags.findall("LocalTag"):
                    dt = lt.get("DataType", "")
                    if dt and dt.upper() not in all_types:
                        result.add_error(
                            f"AOI '{aoi_name}' local tag "
                            f"'{lt.get('Name', '')}' uses undefined "
                            f"data type '{dt}'."
                        )

    return result


def _validate_modules(project) -> ValidationResult:
    """Check module configuration.

    Validates:
        1. A Local module exists (required for all ControlLogix projects).
        2. Parent module references are valid.
        3. No slot conflicts (multiple modules in the same slot of the
           same parent).

    Args:
        project: The L5XProject instance.

    Returns:
        A ValidationResult with module issues.
    """
    result = ValidationResult()
    controller = _get_controller(project)
    if controller is None:
        return result

    modules_container = controller.find("Modules")
    if modules_container is None:
        result.add_warning("No <Modules> section found in the project.")
        return result

    module_elems = modules_container.findall("Module")
    if not module_elems:
        result.add_warning("No modules defined in the project.")
        return result

    module_names = {m.get("Name", "") for m in module_elems}

    # Check for Local module
    has_local = any(
        m.get("Name", "").lower() == "local" for m in module_elems
    )
    if not has_local:
        result.add_warning(
            "No 'Local' module found. ControlLogix projects typically "
            "require a Local module representing the chassis backplane."
        )

    # Check parent references and slot conflicts
    # Track: (parent_name, slot_number) -> list of module names
    slot_usage: dict[tuple[str, str], list[str]] = {}

    for module in module_elems:
        mod_name = module.get("Name", "")
        parent = module.get("ParentModule", "")
        parent_port_id = module.get("ParentModPortId", "")

        if parent:
            if parent not in module_names:
                result.add_error(
                    f"Module '{mod_name}' references parent module "
                    f"'{parent}' which does not exist."
                )

            # Check for slot number in the Ports section
            ports = module.find("Ports")
            if ports is not None:
                for port in ports.findall("Port"):
                    port_address = port.get("Address", "")
                    if port.get("Upstream", "false").lower() == "true":
                        # Upstream port -- this is the slot address
                        slot_key = (parent, port_address)
                        if slot_key not in slot_usage:
                            slot_usage[slot_key] = []
                        slot_usage[slot_key].append(mod_name)

    # Report slot conflicts
    for (parent, slot), names in slot_usage.items():
        if len(names) > 1 and slot:
            result.add_error(
                f"Slot conflict: modules {names} all occupy slot {slot} "
                f"under parent '{parent}'."
            )

    return result


def _validate_tasks(project) -> ValidationResult:
    """Check task configuration.

    Validates:
        1. At least one task is defined.
        2. All scheduled programs exist.
        3. At most one continuous task.
        4. Task types are valid.

    Args:
        project: The L5XProject instance.

    Returns:
        A ValidationResult with task issues.
    """
    result = ValidationResult()
    controller = _get_controller(project)
    if controller is None:
        return result

    tasks_container = controller.find("Tasks")
    if tasks_container is None:
        result.add_warning("No <Tasks> section found in the project.")
        return result

    task_elems = tasks_container.findall("Task")
    if not task_elems:
        result.add_error("No tasks defined. At least one task is required.")
        return result

    program_names = _collect_program_names(project)
    continuous_count = 0

    for task in task_elems:
        task_name = task.get("Name", "")
        task_type = task.get("Type", "")

        # Validate task type
        if task_type and task_type.upper() not in VALID_TASK_TYPES:
            result.add_error(
                f"Task '{task_name}' has invalid type '{task_type}'. "
                f"Valid types: {VALID_TASK_TYPES}"
            )

        if task_type.upper() == "CONTINUOUS":
            continuous_count += 1

        # Check scheduled programs
        sched_programs = task.find("ScheduledPrograms")
        if sched_programs is not None:
            for sp in sched_programs.findall("ScheduledProgram"):
                prog_ref = sp.get("Name", "")
                if prog_ref and prog_ref not in program_names:
                    result.add_error(
                        f"Task '{task_name}' schedules program "
                        f"'{prog_ref}' which does not exist."
                    )

    if continuous_count > 1:
        result.add_error(
            f"Found {continuous_count} continuous tasks. "
            "Only one continuous task is allowed."
        )

    return result


def _validate_rungs(project) -> ValidationResult:
    """Check all rung instruction text in the project.

    Validates:
        1. Every rung's text is semicolon-terminated.
        2. Brackets are matched.
        3. Parentheses are matched.

    Args:
        project: The L5XProject instance.

    Returns:
        A ValidationResult with rung syntax issues.
    """
    result = ValidationResult()
    controller = _get_controller(project)
    if controller is None:
        return result

    programs = controller.find("Programs")
    if programs is None:
        return result

    for program in programs.findall("Program"):
        prog_name = program.get("Name", "")
        routines = program.find("Routines")
        if routines is None:
            continue

        for routine in routines.findall("Routine"):
            routine_name = routine.get("Name", "")
            rll_content = routine.find("RLLContent")
            if rll_content is None:
                continue

            for rung_idx, rung in enumerate(rll_content.findall("Rung")):
                text_elem = rung.find("Text")
                if text_elem is None:
                    continue
                rung_text = text_elem.text
                if rung_text is None:
                    continue
                rung_text = rung_text.strip()
                if not rung_text:
                    continue

                rung_result = validate_rung(rung_text)
                for err in rung_result.errors:
                    result.add_error(
                        f"Program '{prog_name}', routine '{routine_name}', "
                        f"rung {rung_idx}: {err}"
                    )
                for warn in rung_result.warnings:
                    result.add_warning(
                        f"Program '{prog_name}', routine '{routine_name}', "
                        f"rung {rung_idx}: {warn}"
                    )

    # Also check AOI routines
    aoi_container = controller.find("AddOnInstructionDefinitions")
    if aoi_container is not None:
        for aoi in aoi_container.findall("AddOnInstructionDefinition"):
            aoi_name = aoi.get("Name", "")
            routines = aoi.find("Routines")
            if routines is None:
                continue
            for routine in routines.findall("Routine"):
                routine_name = routine.get("Name", "")
                rll_content = routine.find("RLLContent")
                if rll_content is None:
                    continue
                for rung_idx, rung in enumerate(rll_content.findall("Rung")):
                    text_elem = rung.find("Text")
                    if text_elem is None:
                        continue
                    rung_text = text_elem.text
                    if rung_text is None:
                        continue
                    rung_text = rung_text.strip()
                    if not rung_text:
                        continue

                    rung_result = validate_rung(rung_text)
                    for err in rung_result.errors:
                        result.add_error(
                            f"AOI '{aoi_name}', routine '{routine_name}', "
                            f"rung {rung_idx}: {err}"
                        )
                    for warn in rung_result.warnings:
                        result.add_warning(
                            f"AOI '{aoi_name}', routine '{routine_name}', "
                            f"rung {rung_idx}: {warn}"
                        )

    return result


def _validate_aoi_timestamps(project) -> ValidationResult:
    """Check that all AOI definitions have an EditedDate attribute.

    Studio 5000 may reject or ignore AOI definitions that lack a
    valid EditedDate timestamp.

    Args:
        project: The L5XProject instance.

    Returns:
        A ValidationResult with timestamp issues.
    """
    result = ValidationResult()
    controller = _get_controller(project)
    if controller is None:
        return result

    aoi_container = controller.find("AddOnInstructionDefinitions")
    if aoi_container is None:
        return result

    for aoi in aoi_container.findall("AddOnInstructionDefinition"):
        aoi_name = aoi.get("Name", "")
        edited_date = aoi.get("EditedDate", "")
        if not edited_date:
            result.add_warning(
                f"AOI '{aoi_name}' has no EditedDate attribute. "
                "Studio 5000 may reject this definition."
            )
        created_date = aoi.get("CreatedDate", "")
        if not created_date:
            result.add_warning(
                f"AOI '{aoi_name}' has no CreatedDate attribute."
            )

    return result


def _validate_data_formats(project) -> ValidationResult:
    """Check that tags with data have both L5K and Decorated formats.

    Studio 5000 expects both ``<Data Format="L5K">`` and
    ``<Data Format="Decorated">`` sections on tags that carry data.  Missing
    either format may cause import issues.

    Args:
        project: The L5XProject instance.

    Returns:
        A ValidationResult with data format issues.
    """
    result = ValidationResult()
    controller = _get_controller(project)
    if controller is None:
        return result

    def _check_tags_in_container(
        tags_container: etree._Element, scope_label: str
    ) -> None:
        """Check all tags in a container for data format completeness."""
        for tag in tags_container.findall("Tag"):
            tag_name = tag.get("Name", "")
            tag_type = tag.get("TagType", "Base")

            # Alias tags do not carry Data elements
            if tag_type == "Alias":
                continue

            data_elem = tag.find("Data")
            if data_elem is None:
                continue

            formats_found: set[str] = set()
            for child in data_elem:
                fmt = child.get("Format", "")
                if fmt:
                    formats_found.add(fmt)

            if formats_found and "L5K" not in formats_found:
                result.add_warning(
                    f"{scope_label} tag '{tag_name}' is missing "
                    "L5K data format."
                )
            if formats_found and "Decorated" not in formats_found:
                result.add_warning(
                    f"{scope_label} tag '{tag_name}' is missing "
                    "Decorated data format."
                )

    # Controller tags
    tags_elem = controller.find("Tags")
    if tags_elem is not None:
        _check_tags_in_container(tags_elem, "Controller")

    # Program tags
    programs = controller.find("Programs")
    if programs is not None:
        for program in programs.findall("Program"):
            prog_name = program.get("Name", "")
            prog_tags = program.find("Tags")
            if prog_tags is not None:
                _check_tags_in_container(
                    prog_tags, f"Program '{prog_name}'"
                )

    # AOI parameter default data
    aoi_container = controller.find("AddOnInstructionDefinitions")
    if aoi_container is not None:
        for aoi in aoi_container.findall("AddOnInstructionDefinition"):
            aoi_name = aoi.get("Name", "")
            params = aoi.find("Parameters")
            if params is None:
                continue
            for param in params.findall("Parameter"):
                param_name = param.get("Name", "")
                default_datas = param.findall("DefaultData")
                if not default_datas:
                    continue
                formats_found = {
                    dd.get("Format", "") for dd in default_datas
                }
                if "L5K" not in formats_found:
                    result.add_warning(
                        f"AOI '{aoi_name}' parameter '{param_name}' "
                        "is missing L5K DefaultData format."
                    )
                if "Decorated" not in formats_found:
                    result.add_warning(
                        f"AOI '{aoi_name}' parameter '{param_name}' "
                        "is missing Decorated DefaultData format."
                    )

    return result


# ---------------------------------------------------------------------------
# Top-level validation
# ---------------------------------------------------------------------------

def validate_project(project) -> ValidationResult:
    """Run all validation checks on a project.

    Aggregates the results of all individual validators into a single
    :class:`ValidationResult`.  The checks performed are:

    1. **Structural**: Required elements present, correct ordering.
    2. **References**: All tags in rungs exist; all data types used by tags
       are defined.
    3. **Names**: No duplicates in the same scope; valid characters.
    4. **Dependencies**: AOIs/UDTs used by tags are defined; parent modules
       exist.
    5. **Modules**: Local module exists; parent references valid; no slot
       conflicts.
    6. **Tasks**: At least one task; all scheduled programs exist; max 1
       continuous task.
    7. **Rungs**: All instruction text parses correctly (semicolon terminated,
       brackets matched).
    8. **AOI timestamps**: EditedDate is set on all AOIs.
    9. **Data formats**: Both L5K and Decorated present on all tags.

    Args:
        project: The L5XProject instance.

    Returns:
        A :class:`ValidationResult` containing all discovered errors and
        warnings.
    """
    result = ValidationResult()

    # 1. Structural checks
    result.merge(validate_structure(project))

    # If the structure is fundamentally broken, further checks may fail
    if not result.is_valid:
        return result

    # 2. Reference checks
    result.merge(validate_references(project))

    # 3. Naming checks
    result.merge(validate_naming(project))

    # 4. Dependency checks
    result.merge(_validate_dependencies(project))

    # 5. Module checks
    result.merge(_validate_modules(project))

    # 6. Task checks
    result.merge(_validate_tasks(project))

    # 7. Rung syntax checks
    result.merge(_validate_rungs(project))

    # 8. AOI timestamp checks
    result.merge(_validate_aoi_timestamps(project))

    # 9. Data format checks
    result.merge(_validate_data_formats(project))

    # Additionally, validate individual controller tags
    controller = _get_controller(project)
    if controller is not None:
        tags_elem = controller.find("Tags")
        if tags_elem is not None:
            for tag in tags_elem.findall("Tag"):
                result.merge(validate_tag(project, tag))

        # Validate program tags too
        programs = controller.find("Programs")
        if programs is not None:
            for program in programs.findall("Program"):
                prog_tags = program.find("Tags")
                if prog_tags is not None:
                    for tag in prog_tags.findall("Tag"):
                        result.merge(validate_tag(project, tag))

    return result
