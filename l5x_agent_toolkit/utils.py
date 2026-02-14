"""
Utility functions for L5X file manipulation.

Provides CDATA handling, XML helpers, text helpers, and name validation
for working with Rockwell Automation L5X (RSLogix 5000 / Studio 5000)
export files using lxml.

L5X files make extensive use of CDATA sections for descriptions, rung
comments, and structured text content. Since lxml's default parser strips
CDATA markers, this module provides functions that preserve CDATA sections
during round-trip read/write operations.
"""

import copy
import re
from typing import List, Optional, Union

from lxml import etree


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Tag name used by the original l5x library when converting CDATA sections
# into normal elements for in-memory processing.  We support reading files
# that have already been through that conversion.
_LEGACY_CDATA_TAG = "CDATAContent"

# Regex for valid L5X tag names: starts with letter or underscore, followed
# by letters, digits, or underscores.  Maximum 40 characters.
_TAG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")

# Characters allowed in L5X tag names.
_VALID_TAG_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
)

# XML declaration expected at the top of L5X files.
_XML_DECLARATION = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'

# UTF-8 BOM bytes.  Many L5X files exported from Studio 5000 begin with this.
_UTF8_BOM = b"\xef\xbb\xbf"


# ---------------------------------------------------------------------------
# CDATA handling
# ---------------------------------------------------------------------------

def make_cdata(text: str) -> str:
    """Wrap *text* in a CDATA section string.

    Args:
        text: The text content to wrap.

    Returns:
        A string of the form ``<![CDATA[text]]>``.

    Raises:
        ValueError: If *text* contains the CDATA closing delimiter ``]]>``.
    """
    if "]]>" in text:
        raise ValueError(
            "CDATA content must not contain the closing delimiter ']]>'"
        )
    return f"<![CDATA[{text}]]>"


def set_element_cdata(element: etree._Element, text: str) -> None:
    """Set the text content of *element* as a CDATA section.

    This uses lxml's CDATA wrapper so that serialisation via
    :func:`element_to_string` or :func:`write_l5x` preserves the CDATA
    markers.

    Args:
        element: The lxml element whose text to set.
        text: The string content for the CDATA section.
    """
    element.text = etree.CDATA(text)


def get_element_cdata(element: etree._Element) -> Optional[str]:
    """Return the text content of *element*, whether stored as plain text
    or inside a CDATA section.

    If the element has no text at all, returns ``None``.  If the element
    carries the legacy ``CDATAContent`` child element pattern used by the
    original l5x library, the text is read from that child instead.

    Args:
        element: The lxml element to read from.

    Returns:
        The text content as a string, or ``None`` if no text is present.
    """
    # Check for legacy CDATAContent child element first.
    legacy_child = element.find(_LEGACY_CDATA_TAG)
    if legacy_child is not None:
        return legacy_child.text  # May be None for empty elements.

    return element.text


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def find_or_create(
    parent: etree._Element, tag_name: str, attrib: Optional[dict] = None
) -> etree._Element:
    """Find the first child of *parent* with *tag_name*, or create it.

    If a child with the given tag already exists it is returned as-is.
    Otherwise a new child element is appended to *parent* and returned.

    Args:
        parent: The parent element to search under.
        tag_name: The XML tag name to look for or create.
        attrib: Optional dictionary of attributes for a newly created element.
            Ignored if the element already exists.

    Returns:
        The existing or newly created child element.
    """
    child = parent.find(tag_name)
    if child is not None:
        return child
    return etree.SubElement(parent, tag_name, attrib=attrib or {})


def insert_in_order(
    parent: etree._Element,
    element: etree._Element,
    ordering: List[str],
) -> None:
    """Insert *element* into *parent* at the correct position according to
    an ordered list of tag names.

    The *ordering* list defines the canonical sequence of child element tag
    names (e.g. ``schema.CONTROLLER_CHILD_ORDER``).  The new *element* is
    inserted just before the first existing child whose tag name appears
    **after** the new element's tag in *ordering*.  If no such sibling
    exists, the element is appended at the end.

    Elements whose tag names are not in *ordering* are treated as if they
    appear at the very end of the list.

    Args:
        parent: The parent element to insert into.
        element: The element to insert.
        ordering: A list of tag name strings defining the desired order.

    Example::

        insert_in_order(controller, new_tags_elem, CONTROLLER_CHILD_ORDER)
    """
    tag_name = element.tag

    # Build a fast lookup: tag -> position index.
    order_map = {name: idx for idx, name in enumerate(ordering)}

    # Position of the element being inserted.  Default to end if unknown.
    new_pos = order_map.get(tag_name, len(ordering))

    for idx, child in enumerate(parent):
        child_pos = order_map.get(child.tag, len(ordering))
        if child_pos > new_pos:
            parent.insert(idx, element)
            return

    # No child with a later position found -- append at end.
    parent.append(element)


def element_to_string(
    element: etree._Element,
    *,
    xml_declaration: bool = False,
    pretty_print: bool = True,
    encoding: str = "unicode",
) -> str:
    """Serialize an lxml element to a string with CDATA sections preserved.

    Args:
        element: The element to serialize.
        xml_declaration: Whether to include an XML declaration.
        pretty_print: Whether to indent the output.
        encoding: Output encoding.  Defaults to ``"unicode"`` which returns
            a Python ``str``.  Use ``"UTF-8"`` to get ``bytes``.

    Returns:
        A string (or bytes if *encoding* is not ``"unicode"``) representation
        of the XML element.
    """
    return etree.tostring(
        element,
        xml_declaration=xml_declaration,
        pretty_print=pretty_print,
        encoding=encoding,
    )


def parse_l5x(file_path: str) -> etree._Element:
    """Load and parse an L5X file, returning the root element.

    Handles the UTF-8 BOM that Studio 5000 often prepends to exported
    L5X files, and uses an lxml parser configured to preserve CDATA
    sections so they survive round-trip read/write operations.

    Args:
        file_path: Path to the ``.L5X`` file on disk.

    Returns:
        The root ``lxml.etree._Element`` of the parsed XML tree.

    Raises:
        FileNotFoundError: If *file_path* does not exist.
        etree.XMLSyntaxError: If the file contains malformed XML.
        ValueError: If the root element is not ``RSLogix5000Content``.
    """
    with open(file_path, "rb") as fh:
        raw = fh.read()

    # Strip UTF-8 BOM if present.
    if raw.startswith(_UTF8_BOM):
        raw = raw[len(_UTF8_BOM):]

    parser = etree.XMLParser(
        strip_cdata=False,
        remove_blank_text=False,
        encoding="UTF-8",
    )
    root = etree.fromstring(raw, parser=parser)

    if root.tag != "RSLogix5000Content":
        raise ValueError(
            f"Expected root element 'RSLogix5000Content', got '{root.tag}'"
        )

    return root


def write_l5x(root: etree._Element, file_path: str) -> None:
    """Write an L5X XML tree to a file.

    Produces output matching the format expected by Studio 5000:
    - UTF-8 encoding with XML declaration
    - CDATA sections preserved
    - Windows-style line endings (``\\r\\n``)

    Args:
        root: The root ``RSLogix5000Content`` element.
        file_path: Destination file path.
    """
    tree = etree.ElementTree(root)

    xml_bytes = etree.tostring(
        tree,
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=True,
    )

    # Normalise line endings to CRLF for Windows / Studio 5000 compatibility.
    # First collapse any existing CRLF to LF, then convert all LF to CRLF.
    xml_bytes = xml_bytes.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")

    with open(file_path, "wb") as fh:
        fh.write(xml_bytes)


def indent_xml(root: etree._Element, space: str = "  ") -> None:
    """Re-indent an entire XML tree for human-readable formatting.

    Uses ``lxml.etree.indent`` to apply consistent indentation to every
    element.  Text-bearing elements (CDATA descriptions, L5K data, etc.)
    are **not** altered — only the whitespace-only text/tail nodes that
    control visual indentation are touched.

    This modifies the tree **in place**.  Call before ``write_l5x`` to
    produce a pretty-printed output file.

    Args:
        root: The root element of the tree to indent.
        space: The string used for one indentation level (default two
            spaces, matching Studio 5000's export style).
    """
    etree.indent(root, space=space)


def deep_copy(element: etree._Element) -> etree._Element:
    """Create an independent deep copy of an lxml element.

    The returned element (and all its descendants) are fully detached from
    the original tree and can be modified without affecting the source.

    After copying, CDATA markers are restored on elements that typically
    require them (Description, Comment, Text, Line, and L5K Data elements),
    since ``copy.deepcopy`` drops CDATA wrapping from lxml elements.

    Args:
        element: The element to copy.

    Returns:
        A new element that is a deep clone of *element*.
    """
    clone = copy.deepcopy(element)
    _restore_cdata_markers(clone)
    return clone


# Tags whose text content should be CDATA-wrapped after a deep copy.
_CDATA_TAGS = frozenset({
    'Description', 'Comment', 'Text', 'Line',
    'RevisionNote', 'AdditionalHelpText',
})


def _restore_cdata_markers(element: etree._Element) -> None:
    """Walk an element tree and re-wrap CDATA on known CDATA elements."""
    # Check the element itself
    tag = element.tag if isinstance(element.tag, str) else ''
    if tag in _CDATA_TAGS:
        if element.text and len(element) == 0:
            element.text = etree.CDATA(element.text)
    # L5K Data elements
    if tag == 'Data' and element.get('Format') == 'L5K':
        if element.text:
            element.text = etree.CDATA(element.text)
    if tag == 'DefaultData' and element.get('Format') == 'L5K':
        if element.text:
            element.text = etree.CDATA(element.text)
    # Recurse
    for child in element:
        _restore_cdata_markers(child)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def cdata_text(element: etree._Element) -> Optional[str]:
    """Get the text content from an element, whether plain or CDATA-wrapped.

    This is a convenience alias for :func:`get_element_cdata` that also
    handles the common pattern where a ``Description`` element contains
    a CDATA section.

    Args:
        element: The element to read text from.

    Returns:
        The text string, or ``None`` if no text is present.
    """
    return get_element_cdata(element)


def set_cdata_text(element: etree._Element, text: str) -> None:
    """Set the text content of *element* as a CDATA section.

    Convenience alias for :func:`set_element_cdata`.

    Args:
        element: The element to modify.
        text: The text content to set as CDATA.
    """
    set_element_cdata(element, text)


def make_description_element(text: str) -> etree._Element:
    """Create a ``<Description>`` element with CDATA-wrapped text.

    Produces::

        <Description><![CDATA[text]]></Description>

    This matches the format that Studio 5000 expects for tag descriptions,
    module descriptions, and similar documentation fields.

    Args:
        text: The description text.

    Returns:
        A new ``Description`` element containing a CDATA section.
    """
    desc = etree.Element("Description")
    desc.text = etree.CDATA(text)
    return desc


def get_description(element: etree._Element) -> Optional[str]:
    """Get the description text from an element's ``Description`` child.

    Looks for a direct ``Description`` child element and returns its text
    content.  Handles both plain text and CDATA-wrapped content, as well
    as the legacy ``CDATAContent`` element pattern.

    For multi-language projects, this returns the text from the first
    localized child found (or the top-level text if it exists).

    Args:
        element: The parent element that may contain a ``Description`` child.

    Returns:
        The description text as a string, or ``None`` if no description
        is present.
    """
    desc = element.find("Description")
    if desc is None:
        return None

    # Direct text content (single-language projects).
    text = get_element_cdata(desc)
    if text is not None:
        return text

    # Multi-language: look for LocalizedDescription children.
    localized = desc.find("LocalizedDescription")
    if localized is not None:
        return get_element_cdata(localized)

    # Also check generic child elements that may carry the text
    # (e.g. language-specific children with a Lang attribute).
    for child in desc:
        child_text = get_element_cdata(child)
        if child_text is not None:
            return child_text

    return None


def set_description(element: etree._Element, text: Optional[str]) -> None:
    """Set or remove the description on an element.

    If *text* is a string, a ``Description`` child with CDATA content is
    created (or its text is updated if one already exists).  If *text* is
    ``None``, any existing ``Description`` child is removed.

    Args:
        element: The parent element (e.g. a Tag or Module element).
        text: The description string, or ``None`` to remove the description.
    """
    desc = element.find("Description")

    if text is None:
        # Remove existing description if present.
        if desc is not None:
            element.remove(desc)
        return

    if desc is None:
        desc = make_description_element(text)
        # Insert Description after AlarmConditions (if present) but before
        # Data elements.  Studio 5000 requires this exact ordering:
        #   AlarmConditions -> Description -> Data -> Data
        alarm = element.find('AlarmConditions')
        if alarm is not None:
            # Insert immediately after AlarmConditions.
            idx = list(element).index(alarm) + 1
            element.insert(idx, desc)
        else:
            element.insert(0, desc)
    else:
        desc.text = etree.CDATA(text)


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

def validate_tag_name(name: str) -> bool:
    """Validate a tag name against L5X naming rules.

    L5X tag names must:
    - Start with a letter (A-Z, a-z) or underscore (``_``)
    - Contain only letters, digits (0-9), and underscores
    - Be between 1 and 40 characters long

    Args:
        name: The candidate tag name to validate.

    Returns:
        ``True`` if *name* is a valid L5X tag name.

    Raises:
        ValueError: If *name* violates any of the naming rules.  The
            exception message describes the specific violation.
    """
    if not name:
        raise ValueError("Tag name must not be empty")

    if len(name) > 40:
        raise ValueError(
            f"Tag name '{name}' exceeds the 40-character limit "
            f"(length: {len(name)})"
        )

    if name[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_":
        raise ValueError(
            f"Tag name '{name}' must start with a letter or underscore, "
            f"not '{name[0]}'"
        )

    if not _TAG_NAME_RE.match(name):
        bad_chars = sorted(set(name) - _VALID_TAG_CHARS)
        raise ValueError(
            f"Tag name '{name}' contains invalid characters: {bad_chars}"
        )

    return True


def sanitize_name(name: str) -> str:
    """Clean a name to conform to L5X tag naming rules.

    Applies the following transformations:
    1. Replace spaces and hyphens with underscores.
    2. Remove all characters that are not letters, digits, or underscores.
    3. If the result starts with a digit, prepend an underscore.
    4. If the result is empty, return ``"_unnamed"``.
    5. Truncate to 40 characters.

    This function does **not** guarantee uniqueness -- the caller must
    check for collisions with existing names.

    Args:
        name: The input name to sanitize.

    Returns:
        A string that passes :func:`validate_tag_name`.

    Examples::

        >>> sanitize_name("My Tag-1")
        'My_Tag_1'
        >>> sanitize_name("123start")
        '_123start'
        >>> sanitize_name("   ")
        '_unnamed'
    """
    # Step 1: Replace common separators with underscores.
    result = name.replace(" ", "_").replace("-", "_")

    # Step 2: Strip invalid characters.
    result = "".join(ch for ch in result if ch in _VALID_TAG_CHARS)

    # Step 3: Handle empty result.
    if not result:
        return "_unnamed"

    # Step 4: Ensure it starts with a letter or underscore.
    if result[0].isdigit():
        result = "_" + result

    # Step 5: Truncate to 40 characters.
    result = result[:40]

    return result
