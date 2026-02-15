"""
L5X Project Model - Main entry point for L5X file manipulation.

Loads an L5X file into memory, provides navigation and query operations,
and writes back valid L5X files.

Sub-accessors organise the query API into logical groups:

    project.tags       -- tag listing, element lookup, value reading
    project.programs   -- program/routine/rung queries
    project.types      -- data type, AOI, module, and task queries
    project.analysis   -- cross-reference searches, unused-tag detection

All old method names (``list_controller_tags``, ``get_tag_element``, etc.)
still work via ``__getattr__`` delegation for full backward compatibility.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from lxml import etree

logger = logging.getLogger(__name__)

# Elements whose text content must be wrapped in CDATA sections when writing.
CDATA_ELEMENTS = frozenset({
    'Description',
    'Comment',
    'Text',
    'Line',
    'RevisionNote',
    'AdditionalHelpText',
})

# Additional element+attribute combos that need CDATA (Data Format="L5K").
_DATA_L5K_FORMAT = 'L5K'


class L5XProject:
    """In-memory representation of a complete L5X project.

    Parses an L5X file into an lxml ElementTree and provides high-level
    accessors for all major components: controller metadata, data types,
    modules, AOIs, tags, programs, routines, rungs, and tasks.

    Sub-accessors:
        tags      -- TagAccessor (listing, element lookup, value reading)
        programs  -- ProgramAccessor (programs, routines, rungs)
        types     -- DataTypeAccessor (types, AOIs, modules, tasks)
        analysis  -- AnalysisEngine (cross-references, unused tags)
    """

    def __init__(self, file_path: Optional[str] = None):
        """Load an L5X file or create a new empty project model.

        Args:
            file_path: Path to .L5X file.  If None, creates an empty model
                       (useful for testing or building a project from scratch).

        Raises:
            FileNotFoundError: If *file_path* does not exist.
            ValueError: If the file is not a valid L5X document.
        """
        self._file_path: Optional[str] = None
        self._tree: Optional[etree._ElementTree] = None
        self._root: Optional[etree._Element] = None
        self._controller: Optional[etree._Element] = None
        self._init_accessors()

        if file_path is not None:
            self.load(file_path)

    def _init_accessors(self) -> None:
        """Create sub-accessor instances (called by __init__ and from_element)."""
        from .accessors import (
            AnalysisEngine,
            DataTypeAccessor,
            ProgramAccessor,
            TagAccessor,
        )
        self.tags = TagAccessor(self)
        self.programs = ProgramAccessor(self)
        self.types = DataTypeAccessor(self)
        self.analysis = AnalysisEngine(self)

    # ------------------------------------------------------------------
    # Backward-compatible delegation
    # ------------------------------------------------------------------

    _ACCESSOR_NAMES = ("tags", "programs", "types", "analysis")

    def __getattr__(self, name: str):
        """Delegate old method names to sub-accessors transparently.

        When code calls ``project.list_controller_tags()`` and that method
        no longer lives directly on L5XProject, Python invokes __getattr__
        which searches each sub-accessor for a matching attribute.
        """
        for acc_name in self._ACCESSOR_NAMES:
            # Use object.__getattribute__ to avoid recursion
            try:
                accessor = object.__getattribute__(self, acc_name)
            except AttributeError:
                continue
            try:
                return getattr(accessor, name)
            except AttributeError:
                continue
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    @classmethod
    def from_element(cls, root: etree._Element) -> 'L5XProject':
        """Create an L5XProject from a pre-built in-memory XML tree.

        This is used by component export operations to create export
        files programmatically without reading from disk.  The resulting
        project can be saved with :meth:`write` or manipulated with any
        of the standard toolkit operations.

        Args:
            root: A valid ``RSLogix5000Content`` element.

        Returns:
            A new :class:`L5XProject` wrapping the provided tree.

        Raises:
            ValueError: If *root* is not a valid ``RSLogix5000Content``
                element or has no ``Controller`` child.
        """
        if root.tag != 'RSLogix5000Content':
            raise ValueError(
                f"Expected 'RSLogix5000Content' root, got '{root.tag}'"
            )
        instance = cls.__new__(cls)
        instance._file_path = None
        instance._tree = None
        instance._root = root
        instance._controller = root.find('Controller')
        if instance._controller is None:
            raise ValueError(
                "Root element has no <Controller> child."
            )
        instance._init_accessors()
        return instance

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def load(self, file_path: str) -> None:
        """Load an L5X file.

        Reads the file as raw bytes so we can strip the UTF-8 BOM before
        handing it to lxml.  Validates the root element is
        ``RSLogix5000Content``.

        Args:
            file_path: Absolute or relative path to the .L5X file.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the root element is not ``RSLogix5000Content``.
            etree.XMLSyntaxError: If the XML is malformed.
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"L5X file not found: {file_path}")

        self._file_path = os.path.abspath(file_path)
        logger.info("Loading L5X file: %s", self._file_path)

        # Read as bytes to handle BOM
        with open(file_path, 'rb') as fh:
            raw = fh.read()

        # Strip UTF-8 BOM if present
        if raw.startswith(b'\xef\xbb\xbf'):
            raw = raw[3:]

        # Parse with lxml -- use a parser that preserves CDATA where possible
        parser = etree.XMLParser(
            remove_blank_text=False,
            strip_cdata=False,
            recover=False,
        )
        self._root = etree.fromstring(raw, parser=parser)

        if self._root.tag != 'RSLogix5000Content':
            raise ValueError(
                f"Expected root element 'RSLogix5000Content', "
                f"got '{self._root.tag}'"
            )

        # Cache the Controller element
        self._controller = self._root.find('Controller')
        if self._controller is None:
            raise ValueError(
                "L5X file does not contain a <Controller> element."
            )

        logger.info(
            "Loaded project: %s (%s)",
            self._controller.get("Name", "?"),
            self._root.get("TargetType", "?"),
        )

    def write(self, file_path: str) -> None:
        """Write the project to an L5X file.

        Produces a valid L5X file with:
        - XML declaration with UTF-8 encoding and standalone="yes"
        - Proper CDATA sections for descriptions, comments, rung text, etc.

        Args:
            file_path: Destination path for the output file.

        Raises:
            RuntimeError: If no project has been loaded.
        """
        if self._root is None:
            raise RuntimeError("No project loaded. Call load() first.")

        # Serialize the tree *without* lxml's XML declaration (it uses
        # single quotes which crash Studio 5000).  We prepend the
        # double-quoted declaration manually.
        xml_bytes = etree.tostring(
            self._root,
            xml_declaration=False,
            encoding='unicode',
            pretty_print=False,
        )

        xml_string = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            + xml_bytes
        )

        # Post-process: restore CDATA sections for specific elements.
        xml_string = self._restore_cdata_sections(xml_string)

        # Write with UTF-8 BOM (matches Studio 5000 output)
        with open(file_path, 'w', encoding='utf-8-sig', newline='\r\n') as fh:
            fh.write(xml_string)

        logger.info("Saved project to: %s", file_path)

    @staticmethod
    def _restore_cdata_sections(xml_string: str) -> str:
        """Post-process serialized XML to wrap certain element text in CDATA.

        lxml does not always preserve CDATA sections through a round-trip.
        This method finds elements that require CDATA (Description, Comment,
        Text, Line, RevisionNote, AdditionalHelpText, and Data Format="L5K")
        and re-wraps their text content.

        Returns:
            The XML string with CDATA sections restored.
        """
        for tag_name in CDATA_ELEMENTS:
            pattern = re.compile(
                rf'(<{tag_name}(?:\s[^>]*)?>)'
                rf'((?:(?!</{tag_name}>).)*?)'
                rf'(</{tag_name}>)',
                re.DOTALL,
            )

            def _cdata_replacer(match):
                open_tag = match.group(1)
                content = match.group(2)
                close_tag = match.group(3)
                stripped = content.strip()
                if stripped.startswith('<![CDATA['):
                    return match.group(0)
                if not stripped:
                    return match.group(0)
                if stripped.startswith('<'):
                    return match.group(0)
                content_raw = content
                content_raw = content_raw.replace('&amp;', '&')
                content_raw = content_raw.replace('&lt;', '<')
                content_raw = content_raw.replace('&gt;', '>')
                content_raw = content_raw.replace('&quot;', '"')
                content_raw = content_raw.replace('&apos;', "'")
                return f'{open_tag}\n<![CDATA[{content_raw}]]>\n{close_tag}'

            xml_string = pattern.sub(_cdata_replacer, xml_string)

        data_l5k_pattern = re.compile(
            r'(<Data\s+Format="L5K"\s*>)'
            r'((?:(?!</Data>).)*?)'
            r'(</Data>)',
            re.DOTALL,
        )

        def _data_l5k_replacer(match):
            open_tag = match.group(1)
            content = match.group(2)
            close_tag = match.group(3)
            stripped = content.strip()
            if stripped.startswith('<![CDATA['):
                return match.group(0)
            if not stripped:
                return match.group(0)
            if stripped.startswith('<'):
                return match.group(0)
            content_raw = content
            content_raw = content_raw.replace('&amp;', '&')
            content_raw = content_raw.replace('&lt;', '<')
            content_raw = content_raw.replace('&gt;', '>')
            content_raw = content_raw.replace('&quot;', '"')
            content_raw = content_raw.replace('&apos;', "'")
            return f'{open_tag}\n<![CDATA[{content_raw}]]>\n{close_tag}'

        xml_string = data_l5k_pattern.sub(_data_l5k_replacer, xml_string)
        return xml_string

    # ------------------------------------------------------------------
    # Public accessors for XML tree
    # ------------------------------------------------------------------

    @property
    def root(self) -> etree._Element:
        """Return the root ``RSLogix5000Content`` element."""
        self._ensure_loaded()
        return self._root

    # ------------------------------------------------------------------
    # Project Metadata
    # ------------------------------------------------------------------

    @property
    def target_type(self) -> str:
        """Return the TargetType (Controller, AddOnInstructionDefinition, etc.)."""
        self._ensure_loaded()
        return self._root.get('TargetType', '')

    @property
    def target_name(self) -> str:
        """Return the TargetName."""
        self._ensure_loaded()
        return self._root.get('TargetName', '')

    @property
    def software_revision(self) -> str:
        """Return the SoftwareRevision (e.g. '37.01')."""
        self._ensure_loaded()
        return self._root.get('SoftwareRevision', '')

    @property
    def controller_name(self) -> str:
        """Return the Controller Name attribute."""
        self._ensure_loaded()
        return self._controller.get('Name', '')

    @property
    def processor_type(self) -> str:
        """Return the ProcessorType (catalog number like 5069-L320ER)."""
        self._ensure_loaded()
        return self._controller.get('ProcessorType', '')

    @property
    def firmware_version(self) -> str:
        """Return Major.Minor firmware version string."""
        self._ensure_loaded()
        major = self._controller.get('MajorRev', '0')
        minor = self._controller.get('MinorRev', '0')
        return f'{major}.{minor}'

    # ------------------------------------------------------------------
    # Container Access
    # ------------------------------------------------------------------

    @property
    def controller(self) -> etree._Element:
        """Return the Controller XML element."""
        self._ensure_loaded()
        return self._controller

    @property
    def data_types_element(self) -> Optional[etree._Element]:
        """Return the DataTypes container element, or None."""
        self._ensure_loaded()
        return self._controller.find('DataTypes')

    @property
    def modules_element(self) -> Optional[etree._Element]:
        """Return the Modules container element, or None."""
        self._ensure_loaded()
        return self._controller.find('Modules')

    @property
    def aoi_definitions_element(self) -> Optional[etree._Element]:
        """Return the AddOnInstructionDefinitions container element, or None."""
        self._ensure_loaded()
        return self._controller.find('AddOnInstructionDefinitions')

    @property
    def controller_tags_element(self) -> Optional[etree._Element]:
        """Return the controller-scope Tags container element, or None."""
        self._ensure_loaded()
        return self._controller.find('Tags')

    @property
    def programs_element(self) -> Optional[etree._Element]:
        """Return the Programs container element, or None."""
        self._ensure_loaded()
        return self._controller.find('Programs')

    @property
    def tasks_element(self) -> Optional[etree._Element]:
        """Return the Tasks container element, or None."""
        self._ensure_loaded()
        return self._controller.find('Tasks')

    @property
    def alarm_definitions_element(self) -> Optional[etree._Element]:
        """Return the AlarmDefinitions container element, or None."""
        self._ensure_loaded()
        return self._controller.find('AlarmDefinitions')

    # ------------------------------------------------------------------
    # Alarm Definitions (remain on core project — specialized domain)
    # ------------------------------------------------------------------

    def get_alarm_definition(self, data_type_name: str) -> Optional[etree._Element]:
        """Return the DatatypeAlarmDefinition for a data type, or None."""
        alarm_defs = self.alarm_definitions_element
        if alarm_defs is None:
            return None
        for dtad in alarm_defs.findall('DatatypeAlarmDefinition'):
            if dtad.get('Name') == data_type_name:
                return dtad
        return None

    def list_alarm_definitions(self) -> list:
        """List all DatatypeAlarmDefinitions in the project."""
        self._ensure_loaded()
        alarm_defs = self.alarm_definitions_element
        if alarm_defs is None:
            return []

        results = []
        for dtad in alarm_defs.findall('DatatypeAlarmDefinition'):
            dt_name = dtad.get('Name', '')
            members = []
            for mad in dtad.findall('MemberAlarmDefinition'):
                m: dict = {
                    'name': mad.get('Name', ''),
                    'input': mad.get('Input', ''),
                    'condition_type': mad.get('ConditionType', ''),
                    'severity': int(mad.get('Severity', '500')),
                }
                text_el = mad.find('.//Text')
                if text_el is not None and text_el.text:
                    m['message'] = text_el.text.strip()
                else:
                    m['message'] = None
                members.append(m)
            results.append({
                'data_type': dt_name,
                'member_count': len(members),
                'members': members,
            })
        return results

    def create_alarm_definition(
        self,
        data_type_name: str,
        members: list,
    ) -> etree._Element:
        """Create a DatatypeAlarmDefinition for a UDT or AOI."""
        from .schema import (
            CONTROLLER_CHILD_ORDER,
            VALID_ALARM_CONDITION_TYPES,
            ALARM_SEVERITY_MIN,
            ALARM_SEVERITY_MAX,
            MEMBER_ALARM_DEFINITION_DEFAULTS,
        )
        from .utils import insert_in_order

        self._ensure_loaded()
        dt_def = self.get_data_type_definition(data_type_name)
        if dt_def is None:
            raise KeyError(f"Data type '{data_type_name}' not found in project")
        if self.get_alarm_definition(data_type_name) is not None:
            raise ValueError(
                f"DatatypeAlarmDefinition already exists for '{data_type_name}'"
            )
        if not members:
            raise ValueError("members list must not be empty")

        alarm_defs = self.alarm_definitions_element
        if alarm_defs is None:
            alarm_defs = etree.SubElement(self._controller, 'AlarmDefinitions')
            insert_in_order(self._controller, alarm_defs, CONTROLLER_CHILD_ORDER)

        dtad = etree.SubElement(alarm_defs, 'DatatypeAlarmDefinition')
        dtad.set('Name', data_type_name)

        seen_names: set = set()
        for m in members:
            m_name = m.get('name', '')
            if not m_name:
                raise ValueError("Each member must have a 'name'")
            if m_name in seen_names:
                raise ValueError(f"Duplicate member name '{m_name}'")
            seen_names.add(m_name)
            m_input = m.get('input', '')
            if not m_input.startswith('.'):
                raise ValueError(f"Member input '{m_input}' must start with '.'")
            ctype = m.get('condition_type', '')
            if ctype not in VALID_ALARM_CONDITION_TYPES:
                raise ValueError(
                    f"Invalid condition_type '{ctype}'. "
                    f"Valid: {sorted(VALID_ALARM_CONDITION_TYPES)}"
                )
            sev = m.get('severity', 500)
            if not (ALARM_SEVERITY_MIN <= sev <= ALARM_SEVERITY_MAX):
                raise ValueError(
                    f"severity must be {ALARM_SEVERITY_MIN}-{ALARM_SEVERITY_MAX}"
                )

            mad = etree.SubElement(dtad, 'MemberAlarmDefinition')
            mad.set('Name', m_name)
            mad.set('Input', m_input)
            mad.set('ConditionType', ctype)
            defaults = dict(MEMBER_ALARM_DEFINITION_DEFAULTS)
            defaults['Severity'] = str(sev)
            if 'on_delay' in m:
                defaults['OnDelay'] = str(m['on_delay'])
            if 'off_delay' in m:
                defaults['OffDelay'] = str(m['off_delay'])
            if 'ack_required' in m:
                defaults['AckRequired'] = str(m['ack_required']).lower()
            if 'expression' in m:
                defaults['Expression'] = m['expression']
            for attr, val in defaults.items():
                mad.set(attr, val)
            alarm_config = etree.SubElement(mad, 'AlarmConfig')
            msg_text = m.get('message')
            if msg_text:
                messages_el = etree.SubElement(alarm_config, 'Messages')
                msg_el = etree.SubElement(messages_el, 'Message')
                msg_el.set('Type', 'ADM')
                text_el = etree.SubElement(msg_el, 'Text')
                text_el.set('Lang', 'en-US')
                text_el.text = etree.CDATA(msg_text)

        return dtad

    def remove_alarm_definition(self, data_type_name: str) -> etree._Element:
        """Remove the DatatypeAlarmDefinition for a data type."""
        self._ensure_loaded()
        alarm_defs = self.alarm_definitions_element
        if alarm_defs is None:
            raise KeyError(f"No alarm definition found for '{data_type_name}'")
        for dtad in alarm_defs.findall('DatatypeAlarmDefinition'):
            if dtad.get('Name') == data_type_name:
                alarm_defs.remove(dtad)
                return dtad
        raise KeyError(f"No alarm definition found for '{data_type_name}'")

    # ------------------------------------------------------------------
    # Query Helpers (used by accessors and get_project_summary)
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_routine_type(routine: etree._Element) -> str:
        """Infer the routine type from the Type attribute or child content."""
        explicit = routine.get("Type")
        if explicit:
            return explicit
        for content_tag, rtype in (
            ("RLLContent", "RLL"),
            ("STContent", "ST"),
            ("FBDContent", "FBD"),
            ("SFCContent", "SFC"),
        ):
            if routine.find(content_tag) is not None:
                return rtype
        return ""

    def get_project_summary(self) -> dict:
        """Return a high-level summary of the project."""
        self._ensure_loaded()

        programs = self._all_program_elements()
        routine_count = 0
        for prog in programs:
            routines_container = prog.find('Routines')
            if routines_container is not None:
                routine_count += len(routines_container.findall('Routine'))

        ctrl_tags = self.controller_tags_element
        tag_count = len(ctrl_tags.findall('Tag')) if ctrl_tags is not None else 0
        modules_el = self.modules_element
        module_count = len(modules_el.findall('Module')) if modules_el is not None else 0
        aoi_el = self.aoi_definitions_element
        aoi_count = (
            len(aoi_el.findall('AddOnInstructionDefinition'))
            if aoi_el is not None else 0
        )
        dt_el = self.data_types_element
        udt_count = len(dt_el.findall('DataType')) if dt_el is not None else 0

        return {
            'controller_name': self.controller_name,
            'processor_type': self.processor_type,
            'firmware': self.firmware_version,
            'target_type': self.target_type,
            'program_count': len(programs),
            'routine_count': routine_count,
            'tag_count': tag_count,
            'module_count': module_count,
            'aoi_count': aoi_count,
            'udt_count': udt_count,
            'program_names': [p.get('Name', '') for p in programs],
            'task_names': [t.get('Name', '') for t in self._all_task_elements()],
        }

    # ------------------------------------------------------------------
    # Internal Helpers (shared by accessors)
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Raise RuntimeError if no project has been loaded."""
        if self._root is None or self._controller is None:
            raise RuntimeError(
                "No project loaded. Call load() or pass a file_path to "
                "the constructor."
            )

    def _all_program_elements(self) -> list:
        """Return all Program elements."""
        programs_el = self.programs_element
        if programs_el is None:
            return []
        return programs_el.findall('Program')

    def _all_task_elements(self) -> list:
        """Return all Task elements."""
        tasks_el = self.tasks_element
        if tasks_el is None:
            return []
        return tasks_el.findall('Task')

    @staticmethod
    def _get_description_text(element: etree._Element) -> str:
        """Extract the text content of a <Description> child, or ''."""
        desc_el = element.find('Description')
        if desc_el is not None and desc_el.text:
            return desc_el.text.strip()
        return ''

    @staticmethod
    def _extract_tag_info_list(tags_container: etree._Element) -> list:
        """Build a list of tag info dicts from a <Tags> element."""
        result = []
        for tag in tags_container.findall('Tag'):
            desc_el = tag.find('Description')
            desc = ''
            if desc_el is not None and desc_el.text:
                desc = desc_el.text.strip()
            info = {
                'name': tag.get('Name', ''),
                'data_type': tag.get('DataType', ''),
                'description': desc,
                'tag_type': tag.get('TagType', 'Base'),
            }
            dims = tag.get('Dimensions')
            if dims:
                info['dimensions'] = dims
            alias_for = tag.get('AliasFor')
            if alias_for:
                info['alias_for'] = alias_for
            result.append(info)
        return result

    @staticmethod
    def _find_decorated_data(tag_el: etree._Element) -> Optional[etree._Element]:
        """Find the <Data Format="Decorated"> child element of a tag."""
        for data_el in tag_el.findall('Data'):
            if data_el.get('Format') == 'Decorated':
                children = list(data_el)
                if children:
                    return children[0]
                return None
        return None

    @classmethod
    def _parse_decorated_data(cls, element: etree._Element):
        """Recursively parse a Decorated data element into Python types."""
        tag = element.tag
        if tag in ('DataValue', 'DataValueMember'):
            return cls._parse_atomic_value(element)
        elif tag in ('Structure', 'StructureMember'):
            return cls._parse_structure(element)
        elif tag in ('Array', 'ArrayMember'):
            return cls._parse_array(element)
        elif tag == 'Element':
            children = list(element)
            if children:
                return cls._parse_decorated_data(children[0])
            return cls._parse_atomic_value(element)
        else:
            children = list(element)
            if children:
                return cls._parse_decorated_data(children[0])
            if element.text:
                return element.text.strip()
            return None

    @staticmethod
    def _parse_atomic_value(element: etree._Element):
        """Convert a DataValue/DataValueMember/Element value to a Python type."""
        value_str = element.get('Value', '')
        data_type = element.get('DataType', '').upper()

        if data_type == 'BOOL':
            return int(value_str) if value_str else 0
        if data_type in ('SINT', 'INT', 'DINT', 'LINT',
                         'USINT', 'UINT', 'UDINT'):
            if not value_str:
                return 0
            try:
                return int(value_str)
            except ValueError:
                return value_str
        if data_type in ('REAL', 'LREAL'):
            if not value_str:
                return 0.0
            try:
                return float(value_str)
            except ValueError:
                return value_str
        if data_type == 'STRING':
            return value_str
        if value_str:
            try:
                return int(value_str)
            except ValueError:
                try:
                    return float(value_str)
                except ValueError:
                    return value_str
        return value_str

    @classmethod
    def _parse_structure(cls, element: etree._Element) -> dict:
        """Parse a Structure or StructureMember element into a dict."""
        result = {}
        for child in element:
            name = child.get('Name')
            if name is None:
                continue
            result[name] = cls._parse_decorated_data(child)
        return result

    @classmethod
    def _parse_array(cls, element: etree._Element) -> list:
        """Parse an Array or ArrayMember element into a list."""
        items = []
        for child in element:
            items.append(cls._parse_decorated_data(child))
        return items

    @staticmethod
    def _find_member_element(
        parent: etree._Element, member_name: str
    ) -> Optional[etree._Element]:
        """Find a child element with a matching Name attribute."""
        for child in parent:
            if child.get('Name') == member_name:
                return child
        return None

    @staticmethod
    def _find_array_element(
        parent: etree._Element, index: int
    ) -> Optional[etree._Element]:
        """Find an Element child with a matching Index attribute."""
        target_index = f'[{index}]'
        for child in parent:
            if child.tag == 'Element' and child.get('Index') == target_index:
                return child
        return None

    def _collect_all_code_text(self) -> list:
        """Collect all rung text and ST lines from every routine."""
        texts = []
        for prog in self._all_program_elements():
            routines_container = prog.find('Routines')
            if routines_container is None:
                continue
            for routine in routines_container.findall('Routine'):
                routine_type = self._infer_routine_type(routine)
                if routine_type == 'RLL':
                    rll_content = routine.find('RLLContent')
                    if rll_content is None:
                        continue
                    for rung in rll_content.findall('Rung'):
                        text_el = rung.find('Text')
                        if text_el is not None and text_el.text:
                            texts.append(text_el.text.strip())
                elif routine_type == 'ST':
                    st_content = routine.find('STContent')
                    if st_content is None:
                        continue
                    for line_el in st_content.findall('Line'):
                        if line_el.text:
                            texts.append(line_el.text.strip())
        return texts

    # ------------------------------------------------------------------
    # Dunder Methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        if self._file_path:
            return (
                f"L5XProject(file='{os.path.basename(self._file_path)}', "
                f"controller='{self.controller_name}')"
            )
        return "L5XProject(empty)"

    def __str__(self) -> str:
        if self._root is None:
            return "L5XProject: No project loaded"
        return (
            f"L5XProject: {self.controller_name} "
            f"({self.processor_type}, FW {self.firmware_version})"
        )
