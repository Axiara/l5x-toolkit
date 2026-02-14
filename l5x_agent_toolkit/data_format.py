"""
Data Format Synchronizer for L5X file manipulation.

In Rockwell Automation L5X files, every tag value must be represented in BOTH
L5K format (compact, flat text) and Decorated format (structured XML). If they
are out of sync, Studio 5000 will crash or refuse to import the file. This
module ensures both representations are generated correctly and consistently.

L5K Format:
    Scalar:    ``0``  (DINT), ``0.00000000e+000``  (REAL), ``0``  (BOOL)
    Structure: ``[member1_val,member2_val,...]``  (nested brackets for sub-structs)
    Array:     ``[elem0,elem1,elem2,...]``

Decorated Format:
    Scalar:    ``<DataValue DataType="DINT" Radix="Decimal" Value="0"/>``
    Structure: ``<Structure DataType="TIMER"><DataValueMember .../> ...</Structure>``
    Array:     ``<Array DataType="DINT" Dimensions="5" Radix="Decimal">
                    <Element Index="[0]" Value="0"/> ...
                </Array>``

The ``project`` parameter, when provided, must expose a method
``get_data_type_definition(name)`` that returns the lxml Element for a
user-defined DataType (UDT) or AddOnInstruction definition, enabling
resolution of custom structure members.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional

from lxml import etree

from .schema import BASE_DATA_TYPES, BUILTIN_STRUCTURES


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_default_radix(data_type: str) -> str:
    """Return the default Radix string for a given data type.

    Args:
        data_type: The Logix data type name (e.g. ``'DINT'``, ``'REAL'``).

    Returns:
        A Radix string such as ``'Decimal'``, ``'Float'``, or ``'NullType'``
        for structure types.
    """
    if data_type in BASE_DATA_TYPES:
        return BASE_DATA_TYPES[data_type]['radix']
    # Structures (built-in or UDT) do not carry a Radix on the outer element;
    # Studio 5000 uses NullType internally but it is usually omitted.
    return 'NullType'


# ---------------------------------------------------------------------------
# Scalar value conversion
# ---------------------------------------------------------------------------

def scalar_to_l5k(data_type: str, value: Any) -> str:
    """Convert a Python value to its L5K format string representation.

    Args:
        data_type: Base data type name (``'DINT'``, ``'REAL'``, etc.).
        value:     The Python value to convert.

    Returns:
        The L5K-format string for the value.

    Raises:
        ValueError: If *data_type* is not a recognised scalar type.
    """
    if data_type in ('REAL', 'LREAL'):
        return _float_to_l5k(float(value))
    if data_type in ('BOOL',):
        return '1' if value else '0'
    if data_type in ('SINT', 'INT', 'DINT', 'LINT', 'USINT', 'UINT', 'UDINT'):
        return str(int(value))
    if data_type == 'STRING':
        # STRING in L5K is represented as a structure [LEN, 'DATA...']
        # This helper handles only raw scalar conversion; full STRING
        # generation is done in the structure generators.
        return str(value)
    raise ValueError(f"Unsupported scalar data type for L5K conversion: {data_type}")


def scalar_to_decorated_value(data_type: str, value: Any, radix: Optional[str] = None) -> str:
    """Convert a Python value to the ``Value`` attribute string for Decorated format.

    In Decorated XML, the ``Value`` attribute on ``<DataValue>`` /
    ``<DataValueMember>`` / ``<Element>`` elements uses a slightly different
    representation than L5K (e.g. REAL uses a shorter decimal form rather than
    scientific notation).

    Args:
        data_type: Base data type name.
        value:     The Python value.
        radix:     Optional radix override; if ``None``, the default for the
                   data type is used.

    Returns:
        The Value attribute string.
    """
    if radix is None:
        radix = get_default_radix(data_type)

    if data_type in ('REAL', 'LREAL'):
        # Decorated format uses compact decimal/float notation.
        fval = float(value)
        if fval == 0.0:
            return '0.0'
        # Use repr-like formatting that avoids unnecessary trailing zeros
        # but keeps at least one decimal place.
        formatted = f'{fval}'
        if '.' not in formatted and 'e' not in formatted.lower():
            formatted += '.0'
        return formatted

    if data_type == 'BOOL':
        return '1' if value else '0'

    if data_type in ('SINT', 'INT', 'DINT', 'LINT', 'USINT', 'UINT', 'UDINT'):
        int_val = int(value)
        if radix == 'Hex':
            # Studio 5000 hex format: 16#xxxx_xxxx
            if data_type in ('SINT', 'USINT'):
                return f'16#{int_val & 0xFF:02x}'
            elif data_type in ('INT', 'UINT'):
                return f'16#{int_val & 0xFFFF:04x}'
            elif data_type in ('DINT', 'UDINT'):
                raw = int_val & 0xFFFFFFFF
                return f'16#{raw:04x}_{raw:04x}'
            elif data_type == 'LINT':
                raw = int_val & 0xFFFFFFFFFFFFFFFF
                return f'16#{raw:016x}'
            return f'16#{int_val:x}'
        if radix == 'Binary':
            if data_type in ('SINT', 'USINT'):
                return f'2#{int_val & 0xFF:08b}'
            elif data_type in ('INT', 'UINT'):
                raw = int_val & 0xFFFF
                return f'2#{raw:04b}_{raw >> 4 & 0xFFF:04b}_{raw >> 8 & 0xFF:04b}_{raw >> 12 & 0xF:04b}'
            return f'2#{int_val:b}'
        if radix == 'Octal':
            return f'8#{int_val & 0xFFFFFFFF:o}'
        # Default: Decimal
        return str(int_val)

    raise ValueError(f"Unsupported scalar data type for Decorated conversion: {data_type}")


# ---------------------------------------------------------------------------
# L5K format generation
# ---------------------------------------------------------------------------

def generate_default_l5k(
    data_type: str,
    dimensions: Optional[str] = None,
    project: Optional[Any] = None,
) -> str:
    """Generate a default (all-zeros) L5K format data string.

    Args:
        data_type:  The Logix data type name.
        dimensions: Comma-separated dimension string (e.g. ``'5'``, ``'3,4'``)
                    for array tags. ``None`` for scalars/structures.
        project:    Optional project object with
                    ``get_data_type_definition(name)`` for UDT/AOI resolution.

    Returns:
        The L5K text to embed inside a ``<Data Format="L5K">`` element.
    """
    if dimensions:
        return _generate_array_l5k(data_type, dimensions, project)
    return _generate_scalar_or_struct_l5k(data_type, project)


def _generate_scalar_or_struct_l5k(data_type: str, project: Optional[Any]) -> str:
    """Return the default L5K string for a single scalar or structure value."""
    # --- Base (atomic) types ---
    if data_type in BASE_DATA_TYPES:
        if data_type in ('REAL', 'LREAL'):
            return '0.00000000e+000'
        if data_type == 'STRING':
            return _generate_string_l5k_default()
        return '0'

    # --- Built-in structures (TIMER, COUNTER, CONTROL) ---
    if data_type in BUILTIN_STRUCTURES:
        return BUILTIN_STRUCTURES[data_type]['l5k_default']

    # --- String-family UDTs (e.g. STRING_CaseCode, STRING_16, STRING_32) ---
    if _is_string_family(data_type, project):
        data_len = _get_string_data_length(data_type, project)
        return _generate_string_l5k_default(length=data_len)

    # --- User-defined types (UDT / AOI) ---
    if project is not None:
        dt_def = project.get_data_type_definition(data_type)
        if dt_def is not None and _is_aoi_definition(dt_def):
            return _generate_aoi_l5k(dt_def, project)
        return _generate_udt_l5k(data_type, project)

    # Fallback: cannot resolve UDT without project context.
    raise ValueError(
        f"Cannot generate default L5K for data type '{data_type}' without "
        f"a project reference. Pass a project object that exposes "
        f"get_data_type_definition(name)."
    )


def _generate_string_l5k_default(length: int = 82) -> str:
    """Generate the default L5K string for the STRING built-in type.

    STRING in Logix is a structure with LEN (DINT) and DATA (SINT[82]).
    Default: ``[0,'$00$00...(82 times)']``
    """
    null_bytes = '$00' * length
    return f"[0,'{null_bytes}']"


def _generate_udt_l5k(data_type: str, project: Any) -> str:
    """Resolve a UDT/AOI definition and generate its default L5K string."""
    dt_def = project.get_data_type_definition(data_type)
    if dt_def is None:
        raise ValueError(
            f"DataType definition for '{data_type}' not found in project."
        )

    members = dt_def.findall('Members/Member')
    if not members:
        return '[]'

    parts: List[str] = []
    for member in members:
        member_name = member.get('Name', '')
        member_dt = member.get('DataType', '')
        member_dim = member.get('Dimension', '0')
        member_hidden = member.get('Hidden', 'false')

        # Hidden members (like ZZZZZZZZZZ* padding) are included in L5K as
        # part of the flat structure, but we still generate their defaults.
        if member_dim and member_dim != '0':
            # Member is an array
            if member_dt == 'SINT' and member.get('Radix') == 'ASCII':
                # This is a string-family DATA member (like STRING_CaseCode.DATA)
                null_bytes = '$00' * int(member_dim)
                parts.append(f"'{null_bytes}'")
            else:
                parts.append(_generate_array_l5k(member_dt, member_dim, project))
        elif member_dt == 'BIT':
            # BIT members are packed into the DINT they reference; they do
            # not appear as separate values in L5K format.
            continue
        else:
            parts.append(_generate_scalar_or_struct_l5k(member_dt, project))

    return '[' + ','.join(parts) + ']'


def _is_aoi_definition(dt_def: etree._Element) -> bool:
    """Return True if the element is an AddOnInstructionDefinition (has Parameters)."""
    return dt_def.tag == 'AddOnInstructionDefinition'


def _generate_aoi_l5k(aoi_def: etree._Element, project: Any) -> str:
    """Generate the default L5K string for an AOI instance tag.

    AOI L5K format packs data as:
        [BOOL_bitfield, param_non_bools..., local_non_bools..., overflow_DINTs...]
    where BOOLs from both Parameters (non-InOut) and LocalTags are bit-packed
    into position 0, followed by non-BOOL parameter values in declaration order,
    then non-BOOL local tag values in declaration order.  Within each group,
    atomics, structures, and arrays are interleaved in their original
    declaration order (matching Studio 5000's memory layout).
    InOut parameters are excluded entirely.
    """
    bool_bits: List[int] = []  # default values for each BOOL, in order
    non_bool_values: List[str] = []  # all non-BOOL values in declaration order

    # Process Parameters (excluding InOut)
    params_el = aoi_def.find('Parameters')
    if params_el is not None:
        for param in params_el.findall('Parameter'):
            usage = param.get('Usage', '')
            if usage == 'InOut':
                continue
            dt = param.get('DataType', '')
            name = param.get('Name', '')
            dims = param.get('Dimensions', '')
            # Get default value from DefaultData Format="L5K"
            default_val = _get_default_data_l5k(param)
            if dt == 'BOOL' and not dims:
                # EnableIn defaults to 1, everything else to 0
                if default_val is not None:
                    bool_bits.append(int(default_val) & 1)
                elif name == 'EnableIn':
                    bool_bits.append(1)
                else:
                    bool_bits.append(0)
            else:
                if default_val is not None:
                    non_bool_values.append(default_val)
                elif dims:
                    non_bool_values.append(
                        _generate_array_l5k(dt, dims, project))
                else:
                    non_bool_values.append(
                        _generate_scalar_or_struct_l5k(dt, project))

    # Process LocalTags
    local_tags_el = aoi_def.find('LocalTags')
    if local_tags_el is not None:
        for lt in local_tags_el.findall('LocalTag'):
            dt = lt.get('DataType', '')
            dims = lt.get('Dimensions', '')
            default_val = _get_default_data_l5k(lt)
            if dt == 'BOOL' and not dims:
                if default_val is not None:
                    bool_bits.append(int(default_val) & 1)
                else:
                    bool_bits.append(0)
            else:
                if default_val is not None:
                    non_bool_values.append(default_val)
                elif dims:
                    non_bool_values.append(
                        _generate_array_l5k(dt, dims, project))
                else:
                    non_bool_values.append(
                        _generate_scalar_or_struct_l5k(dt, project))

    # Pack BOOLs into 32-bit DINTs.  When there are more than 32 BOOLs the
    # overflow words go at the END of the L5K array (after non-BOOL values),
    # matching Studio 5000's serialization order.
    bool_dints: List[int] = []
    for word_start in range(0, max(len(bool_bits), 1), 32):
        word_bits = bool_bits[word_start:word_start + 32]
        packed = 0
        for i, bit in enumerate(word_bits):
            if bit:
                packed |= (1 << i)
        bool_dints.append(packed)
    if not bool_dints:
        bool_dints = [0]

    parts: List[str] = [str(bool_dints[0])]
    parts.extend(non_bool_values)
    # Overflow BOOL DINTs (for AOIs with > 32 BOOLs)
    for overflow in bool_dints[1:]:
        parts.append(str(overflow))

    return '[' + ','.join(parts) + ']'


def rebuild_aoi_l5k_from_decorated(
    aoi_def: etree._Element,
    decorated_structure: etree._Element,
    project: Any,
) -> str:
    """Rebuild AOI L5K data from current Decorated values with BOOL bitpacking.

    When ``set_tag_member_value`` modifies a parameter in the Decorated XML,
    the L5K data must be regenerated with proper AOI packing (BOOLs packed
    into a bitfield at position 0).  This function reads current parameter
    values from the Decorated ``<Structure>`` element and local-tag defaults
    from the AOI definition, then produces correctly packed L5K text.

    Args:
        aoi_def: The ``<AddOnInstructionDefinition>`` element.
        decorated_structure: The ``<Structure>`` element from Decorated data
            containing current ``<DataValueMember>`` values.
        project: Project object for resolving nested types.

    Returns:
        Packed L5K string like ``'[bitfield,non_bool1,non_bool2,...]'``.
    """
    # Build lookup of current parameter values from Decorated XML
    param_values: dict[str, str] = {}
    for member in decorated_structure:
        if member.tag == 'DataValueMember':
            param_values[member.get('Name', '')] = member.get('Value', '0')

    bool_bits: List[int] = []
    non_bool_values: List[str] = []

    # Process Parameters (excluding InOut) — same order as _generate_aoi_l5k
    params_el = aoi_def.find('Parameters')
    if params_el is not None:
        for param in params_el.findall('Parameter'):
            if param.get('Usage', '') == 'InOut':
                continue
            name = param.get('Name', '')
            dt = param.get('DataType', '')
            dims = param.get('Dimensions', '')

            if dt == 'BOOL' and not dims:
                val = param_values.get(name, '0')
                bool_bits.append(int(val) & 1)
            else:
                val = param_values.get(name)
                if val is not None:
                    non_bool_values.append(val)
                else:
                    # Fallback to default from AOI definition
                    default_val = _get_default_data_l5k(param)
                    if default_val is not None:
                        non_bool_values.append(default_val)
                    elif dims:
                        non_bool_values.append(
                            _generate_array_l5k(dt, dims, project))
                    else:
                        non_bool_values.append(
                            _generate_scalar_or_struct_l5k(dt, project))

    # Process LocalTags (always use defaults — not present in Decorated)
    local_tags_el = aoi_def.find('LocalTags')
    if local_tags_el is not None:
        for lt in local_tags_el.findall('LocalTag'):
            dt = lt.get('DataType', '')
            dims = lt.get('Dimensions', '')
            default_val = _get_default_data_l5k(lt)
            if dt == 'BOOL' and not dims:
                bool_bits.append(int(default_val) & 1 if default_val else 0)
            else:
                if default_val is not None:
                    non_bool_values.append(default_val)
                elif dims:
                    non_bool_values.append(
                        _generate_array_l5k(dt, dims, project))
                else:
                    non_bool_values.append(
                        _generate_scalar_or_struct_l5k(dt, project))

    # Pack BOOLs into 32-bit DINTs (same logic as _generate_aoi_l5k)
    bool_dints: List[int] = []
    for word_start in range(0, max(len(bool_bits), 1), 32):
        word_bits = bool_bits[word_start:word_start + 32]
        packed = 0
        for i, bit in enumerate(word_bits):
            if bit:
                packed |= (1 << i)
        bool_dints.append(packed)
    if not bool_dints:
        bool_dints = [0]

    parts: List[str] = [str(bool_dints[0])]
    parts.extend(non_bool_values)
    for overflow in bool_dints[1:]:
        parts.append(str(overflow))

    return '[' + ','.join(parts) + ']'


def _get_default_data_l5k(elem: etree._Element) -> Optional[str]:
    """Extract the L5K default value text from DefaultData or Data children."""
    for dd in elem.findall('DefaultData'):
        if dd.get('Format') == 'L5K':
            text = dd.text
            if text is not None:
                return text.strip()
    return None


def _generate_aoi_decorated(data_type: str, aoi_def: etree._Element) -> etree._Element:
    """Generate the Decorated ``<Structure>`` for an AOI instance tag.

    Only non-InOut Parameters are included. LocalTags are NOT included in
    the Decorated format. Each parameter becomes a ``<DataValueMember>``.
    """
    struct = etree.Element('Structure')
    struct.set('DataType', data_type)

    params_el = aoi_def.find('Parameters')
    if params_el is not None:
        for param in params_el.findall('Parameter'):
            usage = param.get('Usage', '')
            if usage == 'InOut':
                continue
            name = param.get('Name', '')
            dt = param.get('DataType', '')

            # Get default value from DefaultData Decorated format
            default_decorated = _get_default_data_decorated(param)
            if default_decorated is not None:
                # Ensure the element is a DataValueMember (not DataValue)
                default_decorated.tag = 'DataValueMember'
                default_decorated.set('Name', name)
                # Studio 5000 does NOT allow Radix on BOOL DataValueMembers
                # inside tag instance data, even though the AOI DefaultData
                # includes it.  Strip it to prevent import errors.
                if dt == 'BOOL':
                    if 'Radix' in default_decorated.attrib:
                        del default_decorated.attrib['Radix']
                struct.append(default_decorated)
            else:
                # Generate a default DataValueMember
                dvm = etree.SubElement(struct, 'DataValueMember')
                dvm.set('Name', name)
                dvm.set('DataType', dt)
                if dt == 'BOOL':
                    if name == 'EnableIn':
                        dvm.set('Value', '1')
                    else:
                        dvm.set('Value', '0')
                else:
                    if dt not in ('BOOL',):
                        dvm.set('Radix', get_default_radix(dt))
                    dvm.set('Value', _default_decorated_value(dt))

    return struct


def _get_default_data_decorated(elem: etree._Element) -> Optional[etree._Element]:
    """Extract the Decorated default element from DefaultData children.

    Returns the first child element (DataValue, Structure, etc.) found
    inside a ``<DefaultData Format="Decorated">`` element, or None.
    """
    for dd in elem.findall('DefaultData'):
        if dd.get('Format') == 'Decorated':
            for child in dd:
                # Return a deep copy so we don't modify the AOI definition
                import copy
                return copy.deepcopy(child)
    return None


def _generate_array_l5k(data_type: str, dimensions: str, project: Optional[Any]) -> str:
    """Generate the default L5K string for an array."""
    total = _total_elements(dimensions)
    element_default = _generate_scalar_or_struct_l5k(data_type, project)
    return '[' + ','.join([element_default] * total) + ']'


# ---------------------------------------------------------------------------
# Decorated format generation
# ---------------------------------------------------------------------------

def generate_default_decorated(
    data_type: str,
    dimensions: Optional[str] = None,
    radix: Optional[str] = None,
    project: Optional[Any] = None,
) -> etree._Element:
    """Generate the default Decorated format XML for a tag value.

    Returns the inner content element (``<DataValue>``, ``<Structure>``, or
    ``<Array>``) that goes inside a ``<Data Format="Decorated">`` wrapper.

    Args:
        data_type:  The Logix data type name.
        dimensions: Comma-separated dimension string for arrays, or ``None``.
        radix:      Optional radix override for the root element.
        project:    Optional project object for UDT/AOI resolution.

    Returns:
        An ``lxml.etree.Element`` representing the Decorated XML.
    """
    if dimensions:
        return _generate_array_decorated(data_type, dimensions, radix, project)
    return _generate_scalar_or_struct_decorated(data_type, radix, project)


def _generate_scalar_or_struct_decorated(
    data_type: str,
    radix: Optional[str],
    project: Optional[Any],
) -> etree._Element:
    """Return the Decorated XML element for a single scalar or structure."""
    # --- Base (atomic) types ---
    if data_type in BASE_DATA_TYPES:
        if data_type == 'STRING':
            return _generate_string_decorated_default()
        return _make_data_value(data_type, radix)

    # --- Built-in structures ---
    if data_type in BUILTIN_STRUCTURES:
        return _generate_builtin_struct_decorated(data_type)

    # --- String-family UDTs (e.g. STRING_CaseCode, STRING_16, STRING_32) ---
    if _is_string_family(data_type, project):
        data_len = _get_string_data_length(data_type, project)
        return _generate_string_decorated_default(string_type=data_type, data_length=data_len)

    # --- UDT / AOI ---
    if project is not None:
        dt_def = project.get_data_type_definition(data_type)
        if dt_def is not None and _is_aoi_definition(dt_def):
            return _generate_aoi_decorated(data_type, dt_def)
        return _generate_udt_decorated(data_type, project)

    raise ValueError(
        f"Cannot generate default Decorated format for data type '{data_type}' "
        f"without a project reference."
    )


def _make_data_value(data_type: str, radix: Optional[str] = None) -> etree._Element:
    """Create a ``<DataValue>`` element for a base scalar type with default value."""
    if radix is None:
        radix = get_default_radix(data_type)

    elem = etree.Element('DataValue')
    elem.set('DataType', data_type)
    elem.set('Radix', radix)
    elem.set('Value', _default_decorated_value(data_type))
    return elem


def _default_decorated_value(data_type: str) -> str:
    """Return the default Decorated Value attribute string for a base type."""
    if data_type in ('REAL', 'LREAL'):
        return '0.0'
    return '0'


def _generate_builtin_struct_decorated(data_type: str) -> etree._Element:
    """Generate a ``<Structure>`` element for TIMER, COUNTER, or CONTROL."""
    info = BUILTIN_STRUCTURES[data_type]
    struct = etree.Element('Structure')
    struct.set('DataType', data_type)

    for member_name, member_dt, member_radix in info['members']:
        dvm = etree.SubElement(struct, 'DataValueMember')
        dvm.set('Name', member_name)
        dvm.set('DataType', member_dt)
        if member_dt != 'BOOL':
            dvm.set('Radix', member_radix)
        dvm.set('Value', _default_decorated_value(member_dt))

    return struct


def _generate_string_decorated_default(
    string_type: str = 'STRING',
    data_length: int = 82,
) -> etree._Element:
    """Generate the Decorated ``<Structure>`` element for a STRING type.

    STRING is a built-in structure with members LEN (DINT) and DATA
    (represented as a string-valued DataValueMember with Radix="ASCII").
    """
    struct = etree.Element('Structure')
    struct.set('DataType', string_type)

    # LEN member
    len_elem = etree.SubElement(struct, 'DataValueMember')
    len_elem.set('Name', 'LEN')
    len_elem.set('DataType', 'DINT')
    len_elem.set('Radix', 'Decimal')
    len_elem.set('Value', '0')

    # DATA member -- empty string represented with ASCII radix
    data_elem = etree.SubElement(struct, 'DataValueMember')
    data_elem.set('Name', 'DATA')
    data_elem.set('DataType', string_type)
    data_elem.set('Radix', 'ASCII')
    data_elem.text = "\n''\n"

    return struct


def _generate_udt_decorated(data_type: str, project: Any) -> etree._Element:
    """Generate a ``<Structure>`` element for a UDT/AOI type by looking up
    the DataType definition in the project.
    """
    dt_def = project.get_data_type_definition(data_type)
    if dt_def is None:
        raise ValueError(
            f"DataType definition for '{data_type}' not found in project."
        )

    struct = etree.Element('Structure')
    struct.set('DataType', data_type)

    members = dt_def.findall('Members/Member')
    for member in members:
        member_name = member.get('Name', '')
        member_dt = member.get('DataType', '')
        member_dim = member.get('Dimension', '0')
        member_radix = member.get('Radix', None)
        member_hidden = member.get('Hidden', 'false')

        # Hidden members (e.g. ZZZZZZZZZZ* padding/backing fields) are
        # NOT emitted in Decorated format.
        if member_hidden == 'true':
            continue

        # BIT type members in UDTs are rendered as BOOL in Decorated format.
        # They represent individual bits of a hidden backing integer field.
        # In Decorated XML, they appear as <DataValueMember DataType="BOOL">.
        if member_dt == 'BIT':
            dvm = etree.SubElement(struct, 'DataValueMember')
            dvm.set('Name', member_name)
            dvm.set('DataType', 'BOOL')
            dvm.set('Value', '0')
            continue

        if member_dim and member_dim != '0':
            # Member is an array
            if member_dt == 'SINT' and member_radix == 'ASCII':
                # This is a string-family DATA member (like STRING_CaseCode.DATA).
                # Rendered as a DataValueMember with Radix="ASCII" and the
                # parent structure's type name as DataType.
                dvm = etree.SubElement(struct, 'DataValueMember')
                dvm.set('Name', member_name)
                dvm.set('DataType', data_type)  # Use parent UDT type name
                dvm.set('Radix', 'ASCII')
                dvm.text = "\n''\n"
            else:
                # Regular array member -- wrapped in an ArrayMember element
                arr_member = etree.SubElement(struct, 'ArrayMember')
                arr_member.set('Name', member_name)
                arr_member.set('DataType', member_dt)
                arr_member.set('Dimensions', member_dim)
                if member_dt in BASE_DATA_TYPES and member_dt != 'STRING':
                    arr_radix = member_radix if member_radix else get_default_radix(member_dt)
                    arr_member.set('Radix', arr_radix)

                total = _total_elements(member_dim)
                for i in range(total):
                    if _is_structure_type(member_dt, project):
                        el = etree.SubElement(arr_member, 'Element')
                        el.set('Index', f'[{i}]')
                        child_struct = _generate_scalar_or_struct_decorated(
                            member_dt, member_radix, project
                        )
                        el.append(child_struct)
                    else:
                        el = etree.SubElement(arr_member, 'Element')
                        el.set('Index', f'[{i}]')
                        el.set('Value', _default_decorated_value(member_dt))
        elif _is_structure_type(member_dt, project):
            # Nested structure member
            sm = etree.SubElement(struct, 'StructureMember')
            sm.set('Name', member_name)
            sm.set('DataType', member_dt)
            child_struct = _generate_scalar_or_struct_decorated(
                member_dt, member_radix, project
            )
            # Append the children of the generated <Structure> directly,
            # because StructureMember itself acts as the container.
            for child in child_struct:
                sm.append(child)
        else:
            # Simple scalar member
            dvm = etree.SubElement(struct, 'DataValueMember')
            dvm.set('Name', member_name)
            dvm.set('DataType', member_dt)
            if member_dt != 'BOOL':
                eff_radix = member_radix if member_radix else get_default_radix(member_dt)
                dvm.set('Radix', eff_radix)
            dvm.set('Value', _default_decorated_value(member_dt))

    return struct


def _generate_array_decorated(
    data_type: str,
    dimensions: str,
    radix: Optional[str],
    project: Optional[Any],
) -> etree._Element:
    """Generate an ``<Array>`` element for an array tag."""
    total = _total_elements(dimensions)
    is_struct = _is_structure_type(data_type, project)

    arr = etree.Element('Array')
    arr.set('DataType', data_type)
    arr.set('Dimensions', dimensions)

    if not is_struct and data_type in BASE_DATA_TYPES and data_type != 'STRING':
        eff_radix = radix if radix else get_default_radix(data_type)
        arr.set('Radix', eff_radix)

    dim_list = _parse_dimensions(dimensions)

    if len(dim_list) == 1:
        for i in range(dim_list[0]):
            _append_array_element(arr, f'[{i}]', data_type, radix, project, is_struct)
    elif len(dim_list) == 2:
        for i in range(dim_list[0]):
            for j in range(dim_list[1]):
                _append_array_element(
                    arr, f'[{i},{j}]', data_type, radix, project, is_struct
                )
    elif len(dim_list) == 3:
        for i in range(dim_list[0]):
            for j in range(dim_list[1]):
                for k in range(dim_list[2]):
                    _append_array_element(
                        arr, f'[{i},{j},{k}]', data_type, radix, project, is_struct
                    )
    else:
        raise ValueError(f"Unsupported dimension count: {len(dim_list)}")

    return arr


def _append_array_element(
    parent: etree._Element,
    index_str: str,
    data_type: str,
    radix: Optional[str],
    project: Optional[Any],
    is_struct: bool,
) -> None:
    """Append a single ``<Element>`` to an ``<Array>`` parent."""
    el = etree.SubElement(parent, 'Element')
    el.set('Index', index_str)

    if is_struct:
        child = _generate_scalar_or_struct_decorated(data_type, radix, project)
        el.append(child)
    else:
        el.set('Value', _default_decorated_value(data_type))


# ---------------------------------------------------------------------------
# Combined Data element generation
# ---------------------------------------------------------------------------

def generate_tag_data_elements(
    data_type: str,
    dimensions: Optional[str] = None,
    radix: Optional[str] = None,
    project: Optional[Any] = None,
) -> List[etree._Element]:
    """Generate both ``<Data>`` elements (L5K and Decorated) for a tag.

    This is the primary entry point for tag creation. It returns a list of
    two ``<Data>`` elements that should be appended as children of the
    ``<Tag>`` element.

    Args:
        data_type:  The Logix data type name.
        dimensions: Comma-separated dimension string, or ``None`` for
                    non-array tags.
        radix:      Optional radix override.
        project:    Optional project object for UDT/AOI resolution.

    Returns:
        A list of two ``lxml.etree.Element`` objects:
        ``[<Data Format="L5K">, <Data Format="Decorated">]``

    Note:
        For STRING-type scalar tags, Studio 5000 also expects a third
        ``<Data Format="String">`` element. This function does NOT generate
        that element; callers that create STRING tags should add it
        separately if needed.
    """
    elements: List[etree._Element] = []

    # --- L5K Data element ---
    l5k_data = etree.Element('Data')
    l5k_data.set('Format', 'L5K')
    l5k_text = generate_default_l5k(data_type, dimensions, project)
    # L5K data is stored as text content with surrounding newlines.
    l5k_data.text = '\n' + l5k_text + '\n'
    elements.append(l5k_data)

    # --- Decorated Data element ---
    dec_data = etree.Element('Data')
    dec_data.set('Format', 'Decorated')
    dec_inner = generate_default_decorated(data_type, dimensions, radix, project)
    dec_data.append(dec_inner)
    elements.append(dec_data)

    return elements


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _float_to_l5k(value: float) -> str:
    """Format a float in the L5K scientific notation style.

    Studio 5000 expects the form ``X.XXXXXXXXe+NNN`` with exactly 8 decimal
    places and a 3-digit exponent with explicit sign.

    Examples:
        >>> _float_to_l5k(0.0)
        '0.00000000e+000'
        >>> _float_to_l5k(41.94)
        '4.19399986e+001'
    """
    if value == 0.0:
        # Handle positive and negative zero identically.
        return '0.00000000e+000'

    sign = ''
    if value < 0:
        sign = '-'
        value = -value

    # Use Python's formatting then reformat to match Studio 5000 exactly.
    # Python's %e gives us e.g. "4.19400000e+01", we need "4.19400000e+001"
    formatted = f'{value:.8e}'
    # Split on 'e' and reformat exponent
    mantissa, exp_part = formatted.split('e')
    exp_sign = '+' if int(exp_part) >= 0 else '-'
    exp_val = abs(int(exp_part))
    return f'{sign}{mantissa}e{exp_sign}{exp_val:03d}'


def _is_structure_type(data_type: str, project: Optional[Any]) -> bool:
    """Return ``True`` if *data_type* is a structure (not a base scalar).

    This includes TIMER, COUNTER, CONTROL, STRING, and any UDT/AOI.
    """
    if data_type in BUILTIN_STRUCTURES:
        return True
    if data_type == 'STRING':
        return True
    if data_type in BASE_DATA_TYPES:
        return False
    # Check for string-family types (STRING_xx) -- these are structures
    if data_type.startswith('STRING_'):
        return True
    # If we have a project, check if it's a defined DataType or AOI
    if project is not None:
        try:
            dt_def = project.get_data_type_definition(data_type)
            if dt_def is not None:
                return True
        except KeyError:
            pass
    # If we cannot resolve it and it is not a base type, assume structure.
    return True


def _is_string_family(data_type: str, project: Optional[Any] = None) -> bool:
    """Return ``True`` if *data_type* is STRING or a string-family UDT.

    String-family types have ``Family="StringFamily"`` in their DataType
    definition, or are the built-in ``STRING`` type.
    """
    if data_type == 'STRING':
        return True
    if project is not None:
        try:
            dt_def = project.get_data_type_definition(data_type)
            if dt_def is not None:
                return dt_def.get('Family', '') == 'StringFamily'
        except KeyError:
            pass
    return data_type.startswith('STRING_')


def _parse_dimensions(dimensions: str) -> List[int]:
    """Parse a dimension string like ``'5'`` or ``'3,4'`` into a list of ints."""
    parts = dimensions.strip().split(',')
    result = []
    for p in parts:
        p = p.strip()
        if p:
            result.append(int(p))
    return result


def _total_elements(dimensions: str) -> int:
    """Compute the total number of elements from a dimension string."""
    dims = _parse_dimensions(dimensions)
    total = 1
    for d in dims:
        total *= d
    return total


def _get_string_data_length(data_type: str, project: Optional[Any] = None) -> int:
    """Determine the DATA array length for a string-family type.

    The built-in STRING type has 82 SINT bytes. Custom string types
    (STRING_xx) have a variable length determined by their DataType
    definition's DATA member Dimension.

    Returns:
        The number of SINT bytes in the DATA member.
    """
    if data_type == 'STRING':
        return 82

    if project is not None:
        dt_def = project.get_data_type_definition(data_type)
        if dt_def is not None:
            for member in dt_def.findall('Members/Member'):
                if member.get('Name') == 'DATA':
                    dim = member.get('Dimension', '82')
                    return int(dim)

    # Fallback: try to extract from the name convention STRING_NN
    match = re.match(r'STRING_(\d+)', data_type)
    if match:
        return int(match.group(1))

    # Default to standard STRING length
    return 82
