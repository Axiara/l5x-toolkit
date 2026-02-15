"""
I/O Module operations for L5X files.

Provides functions to list, inspect, modify, import, and delete I/O
modules within a Rockwell Automation L5X project.  Modules represent
physical hardware (Ethernet adapters, I/O cards, drives, etc.) in the
controller's I/O tree.

Module XML structure::

    <Module Name="Local" CatalogNumber="5069-L320ER" Vendor="1"
            ProductType="14" ProductCode="217" Major="37" Minor="11"
            ParentModule="Local" ParentModPortId="1"
            Inhibited="false" MajorFault="true">
        <EKey State="Disabled"/>
        <Ports>
            <Port Id="1" Address="0" Type="5069" Upstream="false">
                <Bus Size="17"/>
            </Port>
        </Ports>
    </Module>
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from lxml import etree

from .utils import (
    deep_copy,
    find_or_create,
    get_description,
    parse_l5x,
    set_description,
    validate_tag_name,
)

logger = logging.getLogger(__name__)


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


def _find_modules_container(project: etree._Element) -> etree._Element:
    """Return the Modules container element, creating it if absent.

    Args:
        project: The root ``RSLogix5000Content`` element.

    Returns:
        The ``Modules`` element under the Controller.
    """
    controller = _find_controller(project)
    return find_or_create(controller, "Modules")


def _find_module(project: etree._Element, name: str) -> etree._Element:
    """Find a Module element by name.

    Args:
        project: The root ``RSLogix5000Content`` element.
        name: The module name (case-insensitive match against the
            ``Name`` attribute).

    Returns:
        The matching ``Module`` element.

    Raises:
        KeyError: If no module with the given name exists.
    """
    modules = _find_modules_container(project)
    for mod in modules.findall("Module"):
        if mod.get("Name", "").lower() == name.lower():
            return mod
    raise KeyError(f"Module '{name}' not found")


def _port_to_dict(port: etree._Element) -> dict:
    """Convert a Port element to an informational dictionary.

    Args:
        port: A ``Port`` element.

    Returns:
        A dictionary with keys ``id``, ``address``, ``type``, ``upstream``,
        and optionally ``bus_size``.
    """
    info: dict = {
        "id": port.get("Id", ""),
        "address": port.get("Address", ""),
        "type": port.get("Type", ""),
        "upstream": port.get("Upstream", "false").lower() == "true",
    }

    bus = port.find("Bus")
    if bus is not None:
        info["bus_size"] = bus.get("Size", "")

    return info


def _module_to_dict(module: etree._Element, detailed: bool = False) -> dict:
    """Convert a Module element to an informational dictionary.

    Args:
        module: A ``Module`` element.
        detailed: If ``True``, include port details and description.

    Returns:
        A dictionary with module information.
    """
    info: dict = {
        "name": module.get("Name", ""),
        "catalog_number": module.get("CatalogNumber", ""),
        "vendor": module.get("Vendor", ""),
        "product_type": module.get("ProductType", ""),
        "product_code": module.get("ProductCode", ""),
        "major": module.get("Major", ""),
        "minor": module.get("Minor", ""),
        "parent_module": module.get("ParentModule", ""),
        "parent_mod_port_id": module.get("ParentModPortId", ""),
        "inhibited": module.get("Inhibited", "false").lower() == "true",
        "major_fault": module.get("MajorFault", "false").lower() == "true",
    }

    if detailed:
        # Description.
        info["description"] = get_description(module)

        # EKey state.
        ekey = module.find("EKey")
        if ekey is not None:
            info["ekey_state"] = ekey.get("State", "")
        else:
            info["ekey_state"] = None

        # Ports.
        ports_elem = module.find("Ports")
        if ports_elem is not None:
            info["ports"] = [
                _port_to_dict(p) for p in ports_elem.findall("Port")
            ]
        else:
            info["ports"] = []

    return info


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_modules(project: etree._Element) -> List[dict]:
    """List all modules with key identifying information.

    Returns a list of dictionaries, each containing the module's name,
    catalog number, parent module, inhibit state, and other summary
    attributes.

    Args:
        project: The root ``RSLogix5000Content`` element.

    Returns:
        A list of dictionaries, one per module.  Each dictionary has keys:
        ``name``, ``catalog_number``, ``vendor``, ``product_type``,
        ``product_code``, ``major``, ``minor``, ``parent_module``,
        ``parent_mod_port_id``, ``inhibited``, ``major_fault``.
    """
    modules = _find_modules_container(project)
    return [
        _module_to_dict(mod, detailed=False)
        for mod in modules.findall("Module")
    ]


def get_module_info(project: etree._Element, name: str) -> dict:
    """Get detailed information about a specific module.

    Returns a dictionary with all summary attributes plus port details,
    description, and EKey state.

    Args:
        project: The root ``RSLogix5000Content`` element.
        name: The module name.

    Returns:
        A dictionary with full module details.  In addition to the keys
        returned by :func:`list_modules`, this includes:
        ``description``, ``ekey_state``, and ``ports`` (a list of port
        dictionaries with keys ``id``, ``address``, ``type``,
        ``upstream``, and optionally ``bus_size``).

    Raises:
        KeyError: If no module with the given name exists.
    """
    module = _find_module(project, name)
    return _module_to_dict(module, detailed=True)


def set_module_address(
    project: etree._Element,
    module_name: str,
    port_id: str,
    address: str,
) -> None:
    """Set the address on a specific port of a module.

    This is commonly used to set the IP address of an Ethernet port or
    the slot number of a backplane port.

    Args:
        project: The root ``RSLogix5000Content`` element.
        module_name: The name of the module.
        port_id: The Id of the port to modify (e.g. ``'1'``, ``'2'``).
        address: The new address value (e.g. ``'192.168.1.100'`` for
            Ethernet, or ``'3'`` for a slot number).

    Raises:
        KeyError: If the module or port is not found.
    """
    logger.info("Updating address for module %r to %r", module_name, address)
    module = _find_module(project, module_name)
    ports = module.find("Ports")
    if ports is None:
        raise KeyError(
            f"Module '{module_name}' has no Ports element"
        )

    for port in ports.findall("Port"):
        if port.get("Id", "") == str(port_id):
            port.set("Address", address)
            return

    raise KeyError(
        f"Port Id '{port_id}' not found on module '{module_name}'"
    )


def set_module_inhibited(
    project: etree._Element, module_name: str, inhibited: bool
) -> None:
    """Set or clear the module inhibit flag.

    When a module is inhibited, the controller does not communicate with
    it.  This is useful for commissioning or troubleshooting.

    Args:
        project: The root ``RSLogix5000Content`` element.
        module_name: The name of the module.
        inhibited: ``True`` to inhibit the module, ``False`` to enable it.

    Raises:
        KeyError: If the module is not found.
    """
    module = _find_module(project, module_name)
    module.set("Inhibited", "true" if inhibited else "false")


def import_module(
    project: etree._Element,
    template_path: str,
    name: str,
    parent_module: str = "Local",
    parent_port_id: str = "4",
    address: str = None,
    slot: str = None,
    description: str = None,
) -> etree._Element:
    """Import a module from an L5X template file.

    Loads the template file, extracts the first ``Module`` element found,
    deep-copies it, and configures it with the specified name, parent
    module, and port settings.  The configured module is then appended to
    the project's ``Modules`` container.

    This is the recommended way to add new modules, since module
    definitions (including connection configuration, input/output data
    structures, and communication settings) are complex and
    template-dependent.

    Args:
        project: The root ``RSLogix5000Content`` element.
        template_path: File path to an ``.L5X`` file containing the
            module definition to import.  The file must contain at least
            one ``Module`` element (either at the root level via a module
            export, or within the ``Controller/Modules`` container).
        name: The name to assign to the imported module.  Must comply
            with L5X naming rules.
        parent_module: The name of the parent module in the I/O tree.
            Defaults to ``'Local'`` (the controller itself).
        parent_port_id: The port Id on the parent module to attach to.
            Defaults to ``'4'`` (common for Ethernet backplane).
        address: Optional address to set on the module's first
            downstream (non-upstream) port.  For Ethernet modules this
            is typically an IP address.
        slot: Optional slot number to set on the module's first
            upstream port.  For backplane modules this indicates the
            physical slot position.
        description: Optional description text for the module.

    Returns:
        The newly imported ``Module`` element (already attached to the
        project tree).

    Raises:
        ValueError: If *name* violates L5X naming rules, or the template
            file does not contain a Module element, or a module with the
            same name already exists.
        FileNotFoundError: If *template_path* does not exist.
        lxml.etree.XMLSyntaxError: If the template file is malformed XML.
    """
    logger.info("Importing module from %r", template_path)
    validate_tag_name(name)

    # Check for duplicate name.
    modules = _find_modules_container(project)
    for existing in modules.findall("Module"):
        if existing.get("Name", "").lower() == name.lower():
            raise ValueError(f"Module '{name}' already exists in the project")

    # Parse the template file.
    template_root = parse_l5x(template_path)

    # Locate the Module element in the template.
    # It may be directly under the root (module-level export) or nested
    # within Controller/Modules.
    template_module = template_root.find(".//Module")
    if template_module is None:
        raise ValueError(
            f"No Module element found in template file '{template_path}'"
        )

    # Deep-copy so the template is not modified.
    new_module = deep_copy(template_module)

    # Set identity attributes.
    new_module.set("Name", name)
    new_module.set("ParentModule", parent_module)
    new_module.set("ParentModPortId", str(parent_port_id))

    # Set description if provided.
    if description is not None:
        set_description(new_module, description)

    # Set port address/slot if provided.
    ports = new_module.find("Ports")
    if ports is not None:
        for port in ports.findall("Port"):
            is_upstream = port.get("Upstream", "false").lower() == "true"

            # Set the address on the first downstream port.
            if address is not None and not is_upstream:
                port.set("Address", address)
                address = None  # Only set on the first downstream port.

            # Set the slot on the first upstream port.
            if slot is not None and is_upstream:
                port.set("Address", slot)
                slot = None  # Only set on the first upstream port.

    # Append to the project's Modules container.
    modules.append(new_module)

    return new_module


def delete_module(
    project: etree._Element, name: str
) -> etree._Element:
    """Delete a module by name.

    The ``'Local'`` module (which represents the controller itself)
    cannot be deleted.

    Args:
        project: The root ``RSLogix5000Content`` element.
        name: The name of the module to delete.

    Returns:
        The removed ``Module`` element (detached from the tree).

    Raises:
        KeyError: If no module with the given name exists.
        ValueError: If attempting to delete the ``'Local'`` module.
    """
    logger.info("Deleting module %r", name)
    if name.lower() == "local":
        raise ValueError(
            "Cannot delete the 'Local' module -- it represents the "
            "controller itself"
        )

    module = _find_module(project, name)
    modules = _find_modules_container(project)
    modules.remove(module)

    return module
