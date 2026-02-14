"""
L5X Project Model - Main entry point for L5X file manipulation.

Loads an L5X file into memory, provides navigation and query operations,
and writes back valid L5X files.

This module uses lxml for all XML operations and handles the UTF-8 BOM
that L5X files typically contain.
"""
from lxml import etree
import copy
import os
import re
from datetime import datetime
from typing import Optional, Union


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

        if file_path is not None:
            self.load(file_path)

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

        # Serialize the tree to bytes
        xml_bytes = etree.tostring(
            self._root,
            xml_declaration=True,
            encoding='UTF-8',
            standalone=True,
            pretty_print=False,
        )

        # Post-process: restore CDATA sections for specific elements.
        xml_string = xml_bytes.decode('utf-8')
        xml_string = self._restore_cdata_sections(xml_string)

        # Write with UTF-8 BOM (matches Studio 5000 output)
        with open(file_path, 'w', encoding='utf-8-sig', newline='\r\n') as fh:
            fh.write(xml_string)

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
        # Build a pattern that matches the opening tag, captured content,
        # and closing tag for each CDATA element.
        for tag_name in CDATA_ELEMENTS:
            # Pattern: <TagName ...>content</TagName>
            # We need to be careful not to match self-closing tags or
            # tags whose content is already CDATA.
            pattern = re.compile(
                rf'(<{tag_name}(?:\s[^>]*)?>)'   # opening tag (group 1)
                rf'((?:(?!</{tag_name}>).)*?)'    # content (group 2) - non-greedy
                rf'(</{tag_name}>)',               # closing tag (group 3)
                re.DOTALL,
            )

            def _cdata_replacer(match):
                open_tag = match.group(1)
                content = match.group(2)
                close_tag = match.group(3)

                # Skip if already wrapped in CDATA
                stripped = content.strip()
                if stripped.startswith('<![CDATA['):
                    return match.group(0)

                # Skip if content is empty
                if not stripped:
                    return match.group(0)

                # Skip if content looks like child XML elements (not text)
                # For example, <Data Format="Decorated"><Structure ...> should NOT
                # be wrapped. Only wrap plain text content.
                if stripped.startswith('<'):
                    return match.group(0)

                # Unescape XML entities back to raw text for CDATA
                content_raw = content
                content_raw = content_raw.replace('&amp;', '&')
                content_raw = content_raw.replace('&lt;', '<')
                content_raw = content_raw.replace('&gt;', '>')
                content_raw = content_raw.replace('&quot;', '"')
                content_raw = content_raw.replace('&apos;', "'")

                return f'{open_tag}\n<![CDATA[{content_raw}]]>\n{close_tag}'

            xml_string = pattern.sub(_cdata_replacer, xml_string)

        # Handle <Data Format="L5K"> elements specifically.
        data_l5k_pattern = re.compile(
            r'(<Data\s+Format="L5K"\s*>)'   # opening tag
            r'((?:(?!</Data>).)*?)'          # content
            r'(</Data>)',                     # closing tag
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

    def get_alarm_definition(self, data_type_name: str) -> Optional[etree._Element]:
        """Return the DatatypeAlarmDefinition for a data type, or None.

        Args:
            data_type_name: The data type name (e.g. ``'BW_4ChannelMDR'``).

        Returns:
            The ``DatatypeAlarmDefinition`` element, or ``None`` if the
            data type has no alarm definition.
        """
        alarm_defs = self.alarm_definitions_element
        if alarm_defs is None:
            return None
        for dtad in alarm_defs.findall('DatatypeAlarmDefinition'):
            if dtad.get('Name') == data_type_name:
                return dtad
        return None

    def list_alarm_definitions(self) -> list:
        """List all DatatypeAlarmDefinitions in the project.

        Returns:
            List of dicts with ``data_type``, ``member_count``, and
            ``members`` (list of dicts with name, input, condition_type,
            severity, message).
        """
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
                # Extract message
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
        """Create a DatatypeAlarmDefinition for a UDT or AOI.

        Args:
            data_type_name: Name of an existing data type (UDT or AOI).
            members: List of dicts, each with ``'name'``, ``'input'``,
                     ``'condition_type'``, and optionally ``'severity'``,
                     ``'on_delay'``, ``'off_delay'``, ``'message'``,
                     ``'ack_required'``, ``'expression'``.

        Returns:
            The created ``DatatypeAlarmDefinition`` element.
        """
        from .schema import (
            CONTROLLER_CHILD_ORDER,
            VALID_ALARM_CONDITION_TYPES,
            ALARM_SEVERITY_MIN,
            ALARM_SEVERITY_MAX,
            MEMBER_ALARM_DEFINITION_DEFAULTS,
        )
        from .utils import insert_in_order

        self._ensure_loaded()

        # Validate data type exists
        dt_def = self.get_data_type_definition(data_type_name)
        if dt_def is None:
            raise KeyError(
                f"Data type '{data_type_name}' not found in project"
            )

        # Ensure no existing definition
        if self.get_alarm_definition(data_type_name) is not None:
            raise ValueError(
                f"DatatypeAlarmDefinition already exists for "
                f"'{data_type_name}'"
            )

        if not members:
            raise ValueError("members list must not be empty")

        # Get or create AlarmDefinitions container
        alarm_defs = self.alarm_definitions_element
        if alarm_defs is None:
            alarm_defs = etree.SubElement(self._controller, 'AlarmDefinitions')
            insert_in_order(
                self._controller, alarm_defs, CONTROLLER_CHILD_ORDER
            )

        # Build DatatypeAlarmDefinition
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
                raise ValueError(
                    f"Member input '{m_input}' must start with '.'"
                )

            ctype = m.get('condition_type', '')
            if ctype not in VALID_ALARM_CONDITION_TYPES:
                raise ValueError(
                    f"Invalid condition_type '{ctype}'. "
                    f"Valid: {sorted(VALID_ALARM_CONDITION_TYPES)}"
                )

            sev = m.get('severity', 500)
            if not (ALARM_SEVERITY_MIN <= sev <= ALARM_SEVERITY_MAX):
                raise ValueError(
                    f"severity must be {ALARM_SEVERITY_MIN}-"
                    f"{ALARM_SEVERITY_MAX}"
                )

            mad = etree.SubElement(dtad, 'MemberAlarmDefinition')
            mad.set('Name', m_name)
            mad.set('Input', m_input)
            mad.set('ConditionType', ctype)

            # Apply defaults then overrides
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

            # AlarmConfig with optional message
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
        """Remove the DatatypeAlarmDefinition for a data type.

        Args:
            data_type_name: The data type name.

        Returns:
            The removed ``DatatypeAlarmDefinition`` element.

        Raises:
            KeyError: If no alarm definition exists for the data type.
        """
        self._ensure_loaded()
        alarm_defs = self.alarm_definitions_element
        if alarm_defs is None:
            raise KeyError(
                f"No alarm definition found for '{data_type_name}'"
            )
        for dtad in alarm_defs.findall('DatatypeAlarmDefinition'):
            if dtad.get('Name') == data_type_name:
                alarm_defs.remove(dtad)
                return dtad
        raise KeyError(
            f"No alarm definition found for '{data_type_name}'"
        )

    @staticmethod
    def _infer_routine_type(routine: etree._Element) -> str:
        """Infer the routine type from the Type attribute or child content elements.

        Rung export files may omit the ``Type`` attribute on ``<Routine>``
        elements.  In that case the type is inferred from the presence of
        content child elements (``RLLContent``, ``STContent``, etc.).

        Returns:
            The routine type string (``'RLL'``, ``'ST'``, ``'FBD'``,
            ``'SFC'``), or ``''`` if it cannot be determined.
        """
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

    # ------------------------------------------------------------------
    # Query Operations
    # ------------------------------------------------------------------

    def get_project_summary(self) -> dict:
        """Return a high-level summary of the project.

        Returns:
            dict with keys:
                controller_name, processor_type, firmware,
                program_count, routine_count, tag_count,
                module_count, aoi_count, udt_count,
                program_names, task_names
        """
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

    def list_programs(self) -> list:
        """Return list of program names."""
        self._ensure_loaded()
        return [p.get('Name', '') for p in self._all_program_elements()]

    def list_routines(self, program_name: str) -> list:
        """Return list of routine info dicts for a program.

        Each dict: {'name': str, 'type': str}
        The type is one of RLL, ST, FBD, SFC.

        Raises:
            KeyError: If the program does not exist.
        """
        prog = self.get_program_element(program_name)
        routines_container = prog.find('Routines')
        if routines_container is None:
            return []
        result = []
        for routine in routines_container.findall('Routine'):
            result.append({
                'name': routine.get('Name', ''),
                'type': self._infer_routine_type(routine),
            })
        return result

    def list_controller_tags(self) -> list:
        """Return list of controller-scope tag info dicts.

        Each dict: {'name': str, 'data_type': str, 'description': str}
        """
        self._ensure_loaded()
        tags_el = self.controller_tags_element
        if tags_el is None:
            return []
        return self._extract_tag_info_list(tags_el)

    def list_program_tags(self, program_name: str) -> list:
        """Return list of program-scope tag info dicts.

        Each dict: {'name': str, 'data_type': str, 'description': str}

        Raises:
            KeyError: If the program does not exist.
        """
        prog = self.get_program_element(program_name)
        tags_el = prog.find('Tags')
        if tags_el is None:
            return []
        return self._extract_tag_info_list(tags_el)

    def list_modules(self) -> list:
        """Return list of module info dicts.

        Each dict: {'name': str, 'catalog_number': str, 'parent': str}
        """
        self._ensure_loaded()
        modules_el = self.modules_element
        if modules_el is None:
            return []
        result = []
        for mod in modules_el.findall('Module'):
            result.append({
                'name': mod.get('Name', ''),
                'catalog_number': mod.get('CatalogNumber', ''),
                'parent': mod.get('ParentModule', ''),
            })
        return result

    def list_aois(self) -> list:
        """Return list of AOI info dicts.

        Each dict: {'name': str, 'revision': str, 'description': str}
        """
        self._ensure_loaded()
        aoi_el = self.aoi_definitions_element
        if aoi_el is None:
            return []
        result = []
        for aoi in aoi_el.findall('AddOnInstructionDefinition'):
            desc = self._get_description_text(aoi)
            result.append({
                'name': aoi.get('Name', ''),
                'revision': aoi.get('Revision', ''),
                'description': desc,
            })
        return result

    def list_udts(self) -> list:
        """Return list of UDT info dicts.

        Each dict: {'name': str, 'description': str, 'member_count': int}
        """
        self._ensure_loaded()
        dt_el = self.data_types_element
        if dt_el is None:
            return []
        result = []
        for dt in dt_el.findall('DataType'):
            desc = self._get_description_text(dt)
            members_el = dt.find('Members')
            member_count = (
                len(members_el.findall('Member'))
                if members_el is not None else 0
            )
            result.append({
                'name': dt.get('Name', ''),
                'description': desc,
                'member_count': member_count,
            })
        return result

    def list_tasks(self) -> list:
        """Return list of task info dicts.

        Each dict: {'name': str, 'type': str, 'priority': str, 'rate': str,
                     'watchdog': str, 'programs': list[str]}
        """
        self._ensure_loaded()
        tasks_el = self.tasks_element
        if tasks_el is None:
            return []
        result = []
        for task in tasks_el.findall('Task'):
            scheduled = task.find('ScheduledPrograms')
            prog_names = []
            if scheduled is not None:
                for sp in scheduled.findall('ScheduledProgram'):
                    prog_names.append(sp.get('Name', ''))
            result.append({
                'name': task.get('Name', ''),
                'type': task.get('Type', ''),
                'priority': task.get('Priority', ''),
                'rate': task.get('Rate', ''),
                'watchdog': task.get('Watchdog', ''),
                'programs': prog_names,
            })
        return result

    # ------------------------------------------------------------------
    # Element Lookup
    # ------------------------------------------------------------------

    def get_program_element(self, program_name: str) -> etree._Element:
        """Return Program XML element by name.

        Raises:
            KeyError: If no program with that name exists.
        """
        self._ensure_loaded()
        programs_el = self.programs_element
        if programs_el is not None:
            for prog in programs_el.findall('Program'):
                if prog.get('Name') == program_name:
                    return prog
        raise KeyError(f"Program '{program_name}' not found.")

    def is_safety_program(self, program_name: str) -> bool:
        """Return ``True`` if the named program has ``Class='Safety'``."""
        prog = self.get_program_element(program_name)
        return prog.get('Class', '') == 'Safety'

    def get_routine_element(
        self, program_name: str, routine_name: str
    ) -> etree._Element:
        """Return Routine XML element.

        Raises:
            KeyError: If the program or routine does not exist.
        """
        prog = self.get_program_element(program_name)
        routines_container = prog.find('Routines')
        if routines_container is not None:
            for routine in routines_container.findall('Routine'):
                if routine.get('Name') == routine_name:
                    return routine
        raise KeyError(
            f"Routine '{routine_name}' not found in program '{program_name}'."
        )

    def get_controller_tag_element(self, tag_name: str) -> etree._Element:
        """Return controller-scope Tag element by name.

        Raises:
            KeyError: If the tag does not exist.
        """
        self._ensure_loaded()
        tags_el = self.controller_tags_element
        if tags_el is not None:
            for tag in tags_el.findall('Tag'):
                if tag.get('Name') == tag_name:
                    return tag
        raise KeyError(f"Controller tag '{tag_name}' not found.")

    def get_program_tag_element(
        self, program_name: str, tag_name: str
    ) -> etree._Element:
        """Return program-scope Tag element by name.

        Raises:
            KeyError: If the program or tag does not exist.
        """
        prog = self.get_program_element(program_name)
        tags_el = prog.find('Tags')
        if tags_el is not None:
            for tag in tags_el.findall('Tag'):
                if tag.get('Name') == tag_name:
                    return tag
        raise KeyError(
            f"Tag '{tag_name}' not found in program '{program_name}'."
        )

    def get_tag_element(
        self,
        tag_name: str,
        scope: str = 'controller',
        program_name: Optional[str] = None,
    ) -> etree._Element:
        """Generic tag element lookup.

        Args:
            tag_name: Name of the tag.
            scope: 'controller' or 'program'.
            program_name: Required when scope is 'program'.

        Raises:
            KeyError: If the tag does not exist.
            ValueError: If scope is 'program' but no program_name given.
        """
        if scope == 'controller':
            return self.get_controller_tag_element(tag_name)
        elif scope == 'program':
            if not program_name:
                raise ValueError(
                    "program_name is required when scope is 'program'."
                )
            return self.get_program_tag_element(program_name, tag_name)
        else:
            raise ValueError(f"Invalid scope '{scope}'. Use 'controller' or 'program'.")

    def get_module_element(self, module_name: str) -> etree._Element:
        """Return Module element by name.

        Raises:
            KeyError: If the module does not exist.
        """
        self._ensure_loaded()
        modules_el = self.modules_element
        if modules_el is not None:
            for mod in modules_el.findall('Module'):
                if mod.get('Name') == module_name:
                    return mod
        raise KeyError(f"Module '{module_name}' not found.")

    def get_aoi_element(self, aoi_name: str) -> etree._Element:
        """Return AddOnInstructionDefinition element by name.

        Raises:
            KeyError: If the AOI does not exist.
        """
        self._ensure_loaded()
        aoi_el = self.aoi_definitions_element
        if aoi_el is not None:
            for aoi in aoi_el.findall('AddOnInstructionDefinition'):
                if aoi.get('Name') == aoi_name:
                    return aoi
        raise KeyError(f"AOI '{aoi_name}' not found.")

    def get_data_type_element(self, type_name: str) -> etree._Element:
        """Return DataType element by name.

        This is used by external modules (e.g. data_format) to resolve
        UDT structures.

        Raises:
            KeyError: If the data type does not exist.
        """
        self._ensure_loaded()
        dt_el = self.data_types_element
        if dt_el is not None:
            for dt in dt_el.findall('DataType'):
                if dt.get('Name') == type_name:
                    return dt
        raise KeyError(f"DataType '{type_name}' not found.")

    def get_data_type_definition(self, type_name: str) -> etree._Element:
        """Return a DataType or AddOnInstructionDefinition element by name.

        First searches ``<DataTypes>``, then falls back to
        ``<AddOnInstructionDefinitions>`` so AOI types can be resolved
        for data format generation.

        Raises:
            KeyError: If the type is not found in either container.
        """
        self._ensure_loaded()
        # Try UDTs first
        dt_el = self.data_types_element
        if dt_el is not None:
            for dt in dt_el.findall('DataType'):
                if dt.get('Name') == type_name:
                    return dt
        # Try AOIs
        aoi_el = self.aoi_definitions_element
        if aoi_el is not None:
            for aoi in aoi_el.findall('AddOnInstructionDefinition'):
                if aoi.get('Name') == type_name:
                    return aoi
        raise KeyError(f"DataType or AOI '{type_name}' not found.")

    # ------------------------------------------------------------------
    # Rung Access
    # ------------------------------------------------------------------

    def get_rung_count(self, program_name: str, routine_name: str) -> int:
        """Return the number of rungs in a routine.

        Raises:
            KeyError: If the program or routine does not exist.
            ValueError: If the routine is not an RLL routine.
        """
        rungs = self._get_rung_elements(program_name, routine_name)
        return len(rungs)

    def get_rung_text(
        self, program_name: str, routine_name: str, rung_number: int
    ) -> str:
        """Return the instruction text of a specific rung.

        Args:
            program_name: Name of the program.
            routine_name: Name of the routine.
            rung_number: Zero-based rung index (matches the Number attribute).

        Raises:
            KeyError: If the program, routine, or rung does not exist.
        """
        rung = self._get_rung_by_number(program_name, routine_name, rung_number)
        text_el = rung.find('Text')
        if text_el is not None and text_el.text:
            return text_el.text.strip()
        return ''

    def get_rung_comment(
        self, program_name: str, routine_name: str, rung_number: int
    ) -> Optional[str]:
        """Return the comment text of a rung, or None if no comment exists.

        Args:
            program_name: Name of the program.
            routine_name: Name of the routine.
            rung_number: Zero-based rung index.

        Raises:
            KeyError: If the program, routine, or rung does not exist.
        """
        rung = self._get_rung_by_number(program_name, routine_name, rung_number)
        comment_el = rung.find('Comment')
        if comment_el is not None and comment_el.text:
            return comment_el.text.strip()
        return None

    def get_all_rungs(
        self, program_name: str, routine_name: str
    ) -> list:
        """Return all rungs in a routine.

        Each dict in the list contains:
            'number': int, 'type': str, 'text': str, 'comment': str|None

        Raises:
            KeyError: If the program or routine does not exist.
        """
        rungs = self._get_rung_elements(program_name, routine_name)
        result = []
        for rung in rungs:
            text_el = rung.find('Text')
            text = ''
            if text_el is not None and text_el.text:
                text = text_el.text.strip()

            comment_el = rung.find('Comment')
            comment = None
            if comment_el is not None and comment_el.text:
                comment = comment_el.text.strip()

            result.append({
                'number': int(rung.get('Number', '0')),
                'type': rung.get('Type', 'N'),
                'text': text,
                'comment': comment,
            })
        return result

    # ------------------------------------------------------------------
    # Tag Value Access
    # ------------------------------------------------------------------

    def get_tag_value(
        self,
        tag_name: str,
        scope: str = 'controller',
        program_name: Optional[str] = None,
    ):
        """Read a tag's value from the Decorated data format.

        For atomic types (BOOL, SINT, INT, DINT, LINT, REAL, LREAL),
        returns the corresponding Python type (int, float, bool).

        For structure types (TIMER, COUNTER, UDTs), returns a dict
        mapping member names to their values.

        For array types, returns a list of values (or list of dicts
        for arrays of structures).

        Args:
            tag_name: Name of the tag.
            scope: 'controller' or 'program'.
            program_name: Required when scope is 'program'.

        Returns:
            The tag value as a Python type.

        Raises:
            KeyError: If the tag is not found.
            ValueError: If the Decorated data format is not present.
        """
        tag_el = self.get_tag_element(tag_name, scope, program_name)
        data_el = self._find_decorated_data(tag_el)
        if data_el is None:
            raise ValueError(
                f"Tag '{tag_name}' does not have Decorated format data."
            )
        return self._parse_decorated_data(data_el)

    def get_tag_member_value(
        self,
        tag_name: str,
        member_path: str,
        scope: str = 'controller',
        program_name: Optional[str] = None,
    ):
        """Read a specific member value from a structured tag.

        Args:
            tag_name: Name of the tag.
            member_path: Dot-separated path like 'PRE' or 'Status.Active'.
            scope: 'controller' or 'program'.
            program_name: Required when scope is 'program'.

        Returns:
            The member value as a Python type.

        Raises:
            KeyError: If the tag or member path is not found.
        """
        tag_el = self.get_tag_element(tag_name, scope, program_name)
        data_el = self._find_decorated_data(tag_el)
        if data_el is None:
            raise ValueError(
                f"Tag '{tag_name}' does not have Decorated format data."
            )

        parts = member_path.split('.')
        current = data_el

        for part in parts:
            # Check for array index notation like "MyArray[3]"
            array_match = re.match(r'^(\w+)\[(\d+)\]$', part)
            if array_match:
                member_name = array_match.group(1)
                index = int(array_match.group(2))
                # Navigate to the member first
                current = self._find_member_element(current, member_name)
                if current is None:
                    raise KeyError(
                        f"Member '{member_name}' not found in path "
                        f"'{member_path}' for tag '{tag_name}'."
                    )
                # Then navigate to array element
                current = self._find_array_element(current, index)
                if current is None:
                    raise KeyError(
                        f"Array index [{index}] not found in path "
                        f"'{member_path}' for tag '{tag_name}'."
                    )
            else:
                found = self._find_member_element(current, part)
                if found is None:
                    raise KeyError(
                        f"Member '{part}' not found in path '{member_path}' "
                        f"for tag '{tag_name}'."
                    )
                current = found

        return self._parse_decorated_data(current)

    # ------------------------------------------------------------------
    # Cross-Reference
    # ------------------------------------------------------------------

    def find_tag_references(self, tag_name: str) -> list:
        """Find all references to a tag across all routines.

        Searches rung text in all RLL routines for occurrences of the
        tag name.  Also searches ST (Structured Text) lines.

        Args:
            tag_name: The tag name to search for.

        Returns:
            List of dicts: {'program': str, 'routine': str, 'rung': int,
                            'text': str}
            For ST routines, 'rung' is replaced by 'line'.
        """
        self._ensure_loaded()
        results = []

        # Build a pattern that matches the tag name as a whole word or
        # as a prefix followed by . or [ (member / array access).
        # This avoids matching substrings of other tag names.
        escaped = re.escape(tag_name)
        pattern = re.compile(
            rf'(?<![A-Za-z0-9_]){escaped}(?=[.\[\],\)\s;]|$)',
            re.IGNORECASE,
        )

        for prog in self._all_program_elements():
            prog_name = prog.get('Name', '')
            routines_container = prog.find('Routines')
            if routines_container is None:
                continue

            for routine in routines_container.findall('Routine'):
                routine_name = routine.get('Name', '')
                routine_type = self._infer_routine_type(routine)

                if routine_type == 'RLL':
                    rll_content = routine.find('RLLContent')
                    if rll_content is None:
                        continue
                    for rung in rll_content.findall('Rung'):
                        text_el = rung.find('Text')
                        if text_el is None or not text_el.text:
                            continue
                        rung_text = text_el.text.strip()
                        if pattern.search(rung_text):
                            results.append({
                                'program': prog_name,
                                'routine': routine_name,
                                'rung': int(rung.get('Number', '0')),
                                'text': rung_text,
                            })

                elif routine_type == 'ST':
                    st_content = routine.find('STContent')
                    if st_content is None:
                        continue
                    for line_el in st_content.findall('Line'):
                        if line_el.text and pattern.search(line_el.text.strip()):
                            results.append({
                                'program': prog_name,
                                'routine': routine_name,
                                'line': int(line_el.get('Number', '0')),
                                'text': line_el.text.strip(),
                            })

        return results

    def find_unused_tags(
        self,
        scope: str = 'controller',
        program_name: Optional[str] = None,
    ) -> list:
        """Find tags that are not referenced in any rung or ST line.

        Args:
            scope: 'controller' or 'program'.
            program_name: Required when scope is 'program'.

        Returns:
            List of tag names that have no references.
        """
        self._ensure_loaded()

        # Get the list of tags to check
        if scope == 'controller':
            tag_infos = self.list_controller_tags()
        elif scope == 'program':
            if not program_name:
                raise ValueError(
                    "program_name is required when scope is 'program'."
                )
            tag_infos = self.list_program_tags(program_name)
        else:
            raise ValueError(f"Invalid scope '{scope}'.")

        tag_names = [t['name'] for t in tag_infos]

        # Collect all rung text and ST lines across the entire project
        all_code_text = self._collect_all_code_text()

        unused = []
        for name in tag_names:
            escaped = re.escape(name)
            pattern = re.compile(
                rf'(?<![A-Za-z0-9_]){escaped}(?=[.\[\],\)\s;]|$)',
                re.IGNORECASE,
            )
            found = False
            for text in all_code_text:
                if pattern.search(text):
                    found = True
                    break
            if not found:
                unused.append(name)

        return unused

    # ------------------------------------------------------------------
    # Internal Helpers
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
        """Build a list of tag info dicts from a <Tags> element.

        Each dict: {'name': str, 'data_type': str, 'description': str,
                     'tag_type': str, 'dimensions': str, 'alias_for': str}
        """
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

            # Include dimensions if present (for arrays)
            dims = tag.get('Dimensions')
            if dims:
                info['dimensions'] = dims

            # Include alias info if this is an alias tag
            alias_for = tag.get('AliasFor')
            if alias_for:
                info['alias_for'] = alias_for

            result.append(info)
        return result

    def _get_rung_elements(
        self, program_name: str, routine_name: str
    ) -> list:
        """Return the list of Rung elements for a given routine.

        Raises:
            KeyError: If the program or routine does not exist.
            ValueError: If the routine is not an RLL routine.
        """
        routine = self.get_routine_element(program_name, routine_name)
        routine_type = self._infer_routine_type(routine)
        if routine_type != 'RLL':
            raise ValueError(
                f"Routine '{routine_name}' in program '{program_name}' "
                f"is type '{routine_type}', not RLL. "
                f"Rung access is only available for RLL routines."
            )
        rll_content = routine.find('RLLContent')
        if rll_content is None:
            return []
        return rll_content.findall('Rung')

    def _get_rung_by_number(
        self, program_name: str, routine_name: str, rung_number: int
    ) -> etree._Element:
        """Return a specific Rung element by its Number attribute.

        Raises:
            KeyError: If the rung number does not exist.
        """
        rungs = self._get_rung_elements(program_name, routine_name)
        for rung in rungs:
            if int(rung.get('Number', '-1')) == rung_number:
                return rung
        raise KeyError(
            f"Rung {rung_number} not found in routine '{routine_name}' "
            f"of program '{program_name}'. "
            f"Available rungs: 0-{len(rungs) - 1}."
        )

    @staticmethod
    def _find_decorated_data(tag_el: etree._Element) -> Optional[etree._Element]:
        """Find the <Data Format="Decorated"> child element of a tag.

        Returns the first child element inside the Decorated Data block
        (DataValue, Structure, or Array), or None.
        """
        for data_el in tag_el.findall('Data'):
            if data_el.get('Format') == 'Decorated':
                # Return the first child element (DataValue, Structure, Array)
                children = list(data_el)
                if children:
                    return children[0]
                return None
        return None

    @classmethod
    def _parse_decorated_data(cls, element: etree._Element):
        """Recursively parse a Decorated data element into Python types.

        Handles:
        - <DataValue DataType="..." Value="..."/>  -> atomic value
        - <DataValueMember Name="..." DataType="..." Value="..."/>  -> atomic
        - <Structure DataType="..."> ... </Structure>  -> dict
        - <StructureMember ...> ... </StructureMember>  -> nested dict
        - <Array DataType="..." Dimensions="..."> ... </Array>  -> list
        - <ArrayMember ...> ... </ArrayMember>  -> nested list
        - <Element Index="[n]" Value="..."/>  -> used inside Array
        """
        tag = element.tag

        if tag in ('DataValue', 'DataValueMember'):
            return cls._parse_atomic_value(element)

        elif tag in ('Structure', 'StructureMember'):
            return cls._parse_structure(element)

        elif tag in ('Array', 'ArrayMember'):
            return cls._parse_array(element)

        elif tag == 'Element':
            # An Element inside an Array -- could be atomic or contain children
            children = list(element)
            if children:
                # Element contains nested Structure or Array
                return cls._parse_decorated_data(children[0])
            return cls._parse_atomic_value(element)

        else:
            # Fallback: try to return text or recurse into first child
            children = list(element)
            if children:
                return cls._parse_decorated_data(children[0])
            if element.text:
                return element.text.strip()
            return None

    @staticmethod
    def _parse_atomic_value(element: etree._Element):
        """Convert a DataValue/DataValueMember/Element value to a Python type.

        Maps:
        - BOOL -> bool (Python int 0/1 for consistency with PLC convention)
        - SINT, INT, DINT, LINT, USINT, UINT, UDINT -> int
        - REAL, LREAL -> float
        - STRING -> str

        Returns the raw string if the data type is unrecognized.
        """
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
            # STRING values might be stored differently; return as-is
            return value_str

        # For unknown types, try numeric conversion then fall back to string
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
        """Parse a Structure or StructureMember element into a dict.

        Returns a dict mapping member names to their parsed values.
        """
        result = {}
        for child in element:
            name = child.get('Name')
            if name is None:
                continue
            result[name] = cls._parse_decorated_data(child)
        return result

    @classmethod
    def _parse_array(cls, element: etree._Element) -> list:
        """Parse an Array or ArrayMember element into a list.

        Array children are <Element Index="[n]" Value="..."/> or
        <Element Index="[n]"><Structure ...>...</Structure></Element>.
        """
        items = []
        for child in element:
            if child.tag == 'Element':
                items.append(cls._parse_decorated_data(child))
            else:
                # Unexpected child type -- still try to parse
                items.append(cls._parse_decorated_data(child))
        return items

    @staticmethod
    def _find_member_element(
        parent: etree._Element, member_name: str
    ) -> Optional[etree._Element]:
        """Find a child element with a matching Name attribute.

        Searches DataValueMember, StructureMember, and ArrayMember children.
        """
        for child in parent:
            if child.get('Name') == member_name:
                return child
        return None

    @staticmethod
    def _find_array_element(
        parent: etree._Element, index: int
    ) -> Optional[etree._Element]:
        """Find an Element child with a matching Index attribute.

        Indices are stored like "[0]", "[1]", etc.
        """
        target_index = f'[{index}]'
        for child in parent:
            if child.tag == 'Element' and child.get('Index') == target_index:
                return child
        return None

    def _collect_all_code_text(self) -> list:
        """Collect all rung text and ST lines from every routine in the project.

        Returns a list of strings (each being one rung's text or one ST line).
        """
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
