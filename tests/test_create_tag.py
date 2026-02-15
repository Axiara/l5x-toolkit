"""Tests for tags.create_tag."""

import pytest
from lxml import etree
from l5x_agent_toolkit import tags
from l5x_agent_toolkit.project import L5XProject
from l5x_agent_toolkit.utils import validate_tag_name


MINIMAL_XML = (
    "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
    "<RSLogix5000Content SchemaRevision=\"1.0\" SoftwareRevision=\"33.00\""
    " TargetName=\"TestCtrl\" TargetType=\"Controller\""
    " ContainsContext=\"true\" Owner=\"test\" ExportDate=\"x\""
    " ExportOptions=\"NoRawData L5KData DecoratedData\">"
    "<Controller Use=\"Target\" Name=\"TestCtrl\" ProcessorType=\"1769-L33ER\""
    " MajorRev=\"33\" MinorRev=\"11\">"
    "<DataTypes/><Modules/><AddOnInstructionDefinitions/>"
    "<Tags/>"
    "<Programs>"
    "<Program Name=\"MainProgram\" TestEdits=\"false\" MainRoutineName=\"MainRoutine\" Class=\"Standard\">"
    "<Tags/><Routines><Routine Name=\"MainRoutine\" Type=\"RLL\"><RLLContent/></Routine></Routines>"
    "</Program>"
    "<Program Name=\"SafetyProgram\" TestEdits=\"false\" MainRoutineName=\"MainRoutine\" Class=\"Safety\">"
    "<Tags/><Routines><Routine Name=\"MainRoutine\" Type=\"RLL\"><RLLContent/></Routine></Routines>"
    "</Program>"
    "</Programs>"
    "<Tasks><Task Name=\"MainTask\" Type=\"CONTINUOUS\"><ScheduledPrograms>"
    "<ScheduledProgram Name=\"MainProgram\"/></ScheduledPrograms></Task></Tasks>"
    "</Controller></RSLogix5000Content>"
)


class FakeProject:
    def __init__(self):
        parser = etree.XMLParser(strip_cdata=False, remove_blank_text=False)
        self.root = etree.fromstring(MINIMAL_XML.encode(), parser)
        self._controller = self.root.find("Controller")
    @property
    def controller(self): return self._controller
    @property
    def controller_tags_element(self): return self._controller.find("Tags")
    @property
    def data_types_element(self): return self._controller.find("DataTypes")
    @property
    def aoi_definitions_element(self): return self._controller.find("AddOnInstructionDefinitions")
    def get_controller_tag_element(self, tag_name):
        tags_el = self.controller_tags_element
        if tags_el is not None:
            for t in tags_el.findall("Tag"):
                if t.get("Name", "").lower() == tag_name.lower(): return t
        raise KeyError("Tag not found")
    def get_program_element(self, program_name):
        progs = self._controller.find("Programs")
        if progs is not None:
            for p in progs.findall("Program"):
                if p.get("Name", "").lower() == program_name.lower(): return p
        raise KeyError("Program not found")
    def get_program_tag_element(self, program_name, tag_name):
        prog = self.get_program_element(program_name)
        tags_el = prog.find("Tags")
        if tags_el is not None:
            for t in tags_el.findall("Tag"):
                if t.get("Name", "").lower() == tag_name.lower(): return t
        raise KeyError("Tag not found")
    def get_data_type_element(self, n): raise KeyError(n)
    def get_aoi_element(self, n): raise KeyError(n)
    def get_data_type_definition(self, n): raise KeyError(n)
    def get_alarm_definition(self, n): return None
    _parse_decorated_data = L5XProject._parse_decorated_data
    def is_safety_program(self, program_name):
        prog = self.get_program_element(program_name)
        return prog.get('Class', '') == 'Safety'


class TestValidateTagName:
    def test_valid(self): assert validate_tag_name("MyTag") is True
    def test_underscore(self): assert validate_tag_name("_private") is True
    def test_max_length(self): assert validate_tag_name("A" * 40) is True
    def test_empty(self):
        with pytest.raises(ValueError): validate_tag_name("")
    def test_too_long(self):
        with pytest.raises(ValueError): validate_tag_name("A" * 41)
    def test_digit_start(self):
        with pytest.raises(ValueError): validate_tag_name("1Bad")
    def test_invalid_chars(self):
        with pytest.raises(ValueError): validate_tag_name("My-Tag")


class TestCreateTagBase:
    def test_create_dint(self):
        proj = FakeProject()
        tag = tags.create_tag(proj, "Counter", "DINT")
        assert tag.get("Name") == "Counter"
        assert tag.get("DataType") == "DINT"
        assert tag.get("Radix") == "Decimal"
        data_elems = tag.findall("Data")
        assert len(data_elems) == 2
    def test_create_real(self):
        proj = FakeProject()
        tag = tags.create_tag(proj, "Temperature", "REAL")
        assert tag.get("Radix") == "Float"
    def test_create_constant(self):
        proj = FakeProject()
        tag = tags.create_tag(proj, "MaxVal", "DINT", constant=True)
        assert tag.get("Constant") == "true"
    def test_invalid_external_access(self):
        proj = FakeProject()
        with pytest.raises(ValueError): tags.create_tag(proj, "Bad", "DINT", external_access="Invalid")


class TestCreateTagStructure:
    def test_timer(self):
        proj = FakeProject()
        tag = tags.create_tag(proj, "DelayTimer", "TIMER")
        assert tag.get("DataType") == "TIMER"
        assert tag.get("Radix") is None
    def test_counter(self):
        proj = FakeProject()
        tag = tags.create_tag(proj, "PartCount", "COUNTER")
        assert tag.get("DataType") == "COUNTER"


class TestCreateTagErrors:
    def test_duplicate(self):
        proj = FakeProject()
        tags.create_tag(proj, "MyTag", "DINT")
        with pytest.raises(ValueError): tags.create_tag(proj, "MyTag", "DINT")
    def test_invalid_name(self):
        proj = FakeProject()
        with pytest.raises(ValueError): tags.create_tag(proj, "1Bad", "DINT")
    def test_invalid_type(self):
        proj = FakeProject()
        with pytest.raises(KeyError): tags.create_tag(proj, "BadType", "NONEXISTENT")


class TestCreateTagProgramScope:
    def test_create_in_program(self):
        proj = FakeProject()
        tag = tags.create_tag(proj, "LocalTag", "DINT", scope="program", program_name="MainProgram")
        assert tag.get("Name") == "LocalTag"
    def test_scope_needs_name(self):
        proj = FakeProject()
        with pytest.raises(ValueError): tags.create_tag(proj, "Bad", "DINT", scope="program")
    def test_bad_program(self):
        proj = FakeProject()
        with pytest.raises(KeyError): tags.create_tag(proj, "Bad", "DINT", scope="program", program_name="NoSuch")


class TestTagClassAttribute:
    """Controller-scoped tags get Class='Standard'; program-scoped do not."""
    def test_controller_tag_has_class(self):
        proj = FakeProject()
        tag = tags.create_tag(proj, "CTag", "DINT")
        assert tag.get("Class") == "Standard"

    def test_program_tag_no_class(self):
        proj = FakeProject()
        tag = tags.create_tag(proj, "PTag", "DINT", scope="program", program_name="MainProgram")
        assert tag.get("Class") is None

    def test_attribute_order(self):
        """Class appears between Name and TagType in the attribute list."""
        proj = FakeProject()
        tag = tags.create_tag(proj, "OrderTest", "DINT")
        keys = list(tag.attrib.keys())
        assert keys.index("Name") < keys.index("Class") < keys.index("TagType")


class TestTagNewlineFormatting:
    """Appended tags inherit tail whitespace from siblings."""
    def test_new_tag_gets_tail(self):
        # Parse XML with existing whitespace (simulates a formatted file).
        xml = (
            '<Tags>\n'
            '  <Tag Name="Existing" TagType="Base" DataType="DINT"/>\n'
            '</Tags>'
        )
        container = etree.fromstring(xml.encode())
        # The existing tag should have '\n' as its tail.
        existing = container.find('Tag')
        assert existing.tail is not None and '\n' in existing.tail
        # Append via the helper.
        new_tag = etree.Element('Tag', Name='New', TagType='Base', DataType='DINT')
        tags._append_with_tail(container, new_tag)
        # New tag should have the same tail pattern.
        assert new_tag.tail is not None and '\n' in new_tag.tail


class TestSafetyAutoDetect:
    """Safety class is auto-detected from program type."""
    def test_controller_explicit_safety(self):
        proj = FakeProject()
        tag = tags.create_tag(proj, "SafeTag", "DINT", tag_class="Safety")
        assert tag.get("Class") == "Safety"

    def test_safety_program_auto_detects(self):
        proj = FakeProject()
        tag = tags.create_tag(
            proj, "SPTag", "DINT",
            scope="program", program_name="SafetyProgram",
        )
        assert tag.get("Class") == "Safety"

    def test_standard_program_no_class(self):
        proj = FakeProject()
        tag = tags.create_tag(
            proj, "StdTag", "DINT",
            scope="program", program_name="MainProgram",
        )
        assert tag.get("Class") is None

    def test_safety_program_with_explicit_override(self):
        """Explicit tag_class overrides auto-detection."""
        proj = FakeProject()
        tag = tags.create_tag(
            proj, "OverTag", "DINT",
            scope="program", program_name="SafetyProgram",
            tag_class="Standard",
        )
        assert tag.get("Class") == "Standard"


class TestCreateAliasTag:
    def test_basic_alias(self):
        proj = FakeProject()
        # Create a target tag first
        tags.create_tag(proj, "RealTag", "DINT")
        alias = tags.create_alias_tag(proj, "MyAlias", "RealTag")
        assert alias.get("TagType") == "Alias"
        assert alias.get("AliasFor") == "RealTag"
        assert alias.get("Class") == "Standard"  # controller scope
        assert alias.get("DataType") is None  # no DataType on aliases
        assert alias.findall("Data") == []  # no Data elements

    def test_alias_to_io_path(self):
        proj = FakeProject()
        alias = tags.create_alias_tag(proj, "IO_Alias", "Local:1:I.Data.0")
        assert alias.get("AliasFor") == "Local:1:I.Data.0"

    def test_alias_with_description(self):
        proj = FakeProject()
        alias = tags.create_alias_tag(
            proj, "DescAlias", "SomeTag", description="Alias description",
        )
        desc = alias.find("Description")
        assert desc is not None

    def test_alias_in_safety_program(self):
        proj = FakeProject()
        alias = tags.create_alias_tag(
            proj, "SafeAlias", "SomeTag",
            scope="program", program_name="SafetyProgram",
        )
        assert alias.get("Class") == "Safety"

    def test_alias_in_standard_program(self):
        proj = FakeProject()
        alias = tags.create_alias_tag(
            proj, "StdAlias", "SomeTag",
            scope="program", program_name="MainProgram",
        )
        assert alias.get("Class") is None

    def test_duplicate_raises(self):
        proj = FakeProject()
        tags.create_alias_tag(proj, "DupAlias", "Target")
        with pytest.raises(ValueError):
            tags.create_alias_tag(proj, "DupAlias", "Target2")

    def test_empty_alias_for_raises(self):
        proj = FakeProject()
        with pytest.raises(ValueError, match="alias_for"):
            tags.create_alias_tag(proj, "BadAlias", "")

    def test_attribute_order(self):
        proj = FakeProject()
        alias = tags.create_alias_tag(proj, "OrdAlias", "Target")
        keys = list(alias.attrib.keys())
        assert keys.index("Name") < keys.index("Class") < keys.index("TagType")
        assert keys.index("TagType") < keys.index("AliasFor")


class TestEnrichedTagInfo:
    def test_class_in_info(self):
        proj = FakeProject()
        tags.create_tag(proj, "InfoTag", "DINT")
        info = tags.get_tag_info(proj, "InfoTag")
        assert info['class'] == 'Standard'

    def test_alias_for_in_info(self):
        proj = FakeProject()
        tags.create_alias_tag(proj, "AliasInfo", "SomeTarget")
        info = tags.get_tag_info(proj, "AliasInfo")
        assert info['alias_for'] == 'SomeTarget'
        assert info['tag_type'] == 'Alias'

    def test_base_tag_no_alias(self):
        proj = FakeProject()
        tags.create_tag(proj, "BaseTag", "DINT")
        info = tags.get_tag_info(proj, "BaseTag")
        assert info['alias_for'] is None

    def test_produce_info_none_for_base(self):
        proj = FakeProject()
        tags.create_tag(proj, "NoProd", "DINT")
        info = tags.get_tag_info(proj, "NoProd")
        assert info['produce_info'] is None
        assert info['consume_info'] is None

    def test_produce_info_extraction(self):
        """Manually build a produced tag and verify extraction."""
        proj = FakeProject()
        container = proj.controller_tags_element
        tag = etree.SubElement(container, 'Tag',
            Name='ProdTag', TagType='Produced',
            DataType='DINT', ExternalAccess='Read/Write')
        pi = etree.SubElement(tag, 'ProduceInfo',
            ProduceCount='2', UnicastPermitted='true',
            MinimumRPI='0.200', MaximumRPI='536870.900')
        info = tags.get_tag_info(proj, "ProdTag")
        assert info['produce_info'] is not None
        assert info['produce_info']['produce_count'] == '2'
        assert info['produce_info']['unicast_permitted'] == 'true'

    def test_consume_info_extraction(self):
        """Manually build a consumed tag and verify extraction."""
        proj = FakeProject()
        container = proj.controller_tags_element
        tag = etree.SubElement(container, 'Tag',
            Name='ConsTag', TagType='Consumed',
            DataType='DINT', ExternalAccess='Read/Write')
        ci = etree.SubElement(tag, 'ConsumeInfo',
            Producer='RemotePLC', RemoteTag='SharedData',
            RemoteInstance='0', RPI='20', Unicast='true')
        info = tags.get_tag_info(proj, "ConsTag")
        assert info['consume_info'] is not None
        assert info['consume_info']['producer'] == 'RemotePLC'
        assert info['consume_info']['remote_tag'] == 'SharedData'
        assert info['consume_info']['rpi'] == '20'
