"""Tests for alarm management functions."""

import json
import pytest
from lxml import etree

from l5x_agent_toolkit import tags as _tags
from l5x_agent_toolkit.schema import ALARM_DIGITAL_DEFAULTS


# ---------------------------------------------------------------------------
# Minimal project XML fixture with data types and alarm definitions
# ---------------------------------------------------------------------------

_PROJECT_XML = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="33.00"
 TargetName="TestCtrl" TargetType="Controller" ContainsContext="true"
 Owner="test" ExportDate="x" ExportOptions="NoRawData L5KData DecoratedData">
<Controller Use="Target" Name="TestCtrl" ProcessorType="1769-L33ER"
 MajorRev="33" MinorRev="11">
<DataTypes>
  <DataType Name="MyUDT" Family="NoFamily" Class="User">
    <Members>
      <Member Name="Value1" DataType="REAL" Radix="Float" ExternalAccess="Read/Write"/>
      <Member Name="FaultBit" DataType="BOOL" Radix="Decimal" ExternalAccess="Read/Write"
              Target="Value1" BitNumber="0"/>
    </Members>
  </DataType>
</DataTypes>
<Modules/>
<AddOnInstructionDefinitions/>
<AlarmDefinitions>
  <DatatypeAlarmDefinition Name="MyUDT">
    <MemberAlarmDefinition Name="UDT_HiAlarm" Input=".Value1" ConditionType="HI"
        Limit="100.0" Severity="750" OnDelay="1000" OffDelay="500"
        ShelveDuration="0" MaxShelveDuration="0" Deadband="5.0"
        Required="false" AlarmSetOperIncluded="true" AlarmSetRollupIncluded="true"
        AckRequired="true" Latched="false"
        EvaluationPeriod="500 millisecond" Expression="Input &gt; Limit">
      <AlarmConfig>
        <Messages>
          <Message Type="ADM">
            <Text Lang="en-US"><![CDATA[Value1 exceeded high limit]]></Text>
          </Message>
        </Messages>
      </AlarmConfig>
    </MemberAlarmDefinition>
  </DatatypeAlarmDefinition>
</AlarmDefinitions>
<Tags>
  <Tag Name="ExistingDINT" TagType="Base" DataType="DINT" Radix="Decimal"
       ExternalAccess="Read/Write">
    <Data Format="L5K"><![CDATA[0]]></Data>
    <Data Format="Decorated"><DataValue DataType="DINT" Radix="Decimal" Value="0"/></Data>
  </Tag>
  <Tag Name="ExistingAlarmDigital" TagType="Base" DataType="ALARM_DIGITAL"
       ExternalAccess="Read/Write" OpcUaAccess="None">
    <Data Format="Alarm">
      <AlarmDigitalParameters Severity="500" MinDurationPRE="0"
          ShelveDuration="0" MaxShelveDuration="0"
          ProgTime="DT#1970-01-01-00:00:00.000_000Z"
          EnableIn="false" In="false" InFault="false" Condition="true"
          AckRequired="true" Latched="false"
          ProgAck="false" OperAck="false" ProgReset="false" OperReset="false"
          ProgSuppress="false" OperSuppress="false" ProgUnsuppress="false"
          OperUnsuppress="false" OperShelve="false" ProgUnshelve="false"
          OperUnshelve="false" ProgDisable="false" OperDisable="false"
          ProgEnable="false" OperEnable="false" AlarmCountReset="false"
          UseProgTime="false"/>
      <AlarmConfig>
        <Messages>
          <Message Type="AM">
            <Text Lang="en-US"><![CDATA[Motor Error]]></Text>
          </Message>
        </Messages>
      </AlarmConfig>
    </Data>
  </Tag>
  <Tag Name="TagWithAlarmConditions" TagType="Base" DataType="MyUDT"
       ExternalAccess="Read/Write">
    <AlarmConditions>
      <AlarmCondition Name="UDT_HiAlarm" AlarmConditionDefinition="UDT_HiAlarm"
          Input=".Value1" ConditionType="HI" Limit="100.0" Severity="750"
          OnDelay="1000" OffDelay="500" ShelveDuration="0" MaxShelveDuration="0"
          Deadband="5.0" Used="true" AlarmSetOperIncluded="true"
          AlarmSetRollupIncluded="true" InFault="false" AckRequired="true"
          Latched="false" ProgAck="false" OperAck="false" ProgReset="false"
          OperReset="false" ProgSuppress="false" OperSuppress="false"
          ProgUnsuppress="false" OperUnsuppress="false" OperShelve="false"
          ProgUnshelve="false" OperUnshelve="false" ProgDisable="false"
          OperDisable="false" ProgEnable="false" OperEnable="false"
          AlarmCountReset="false" EvaluationPeriod="500 millisecond"
          Expression="Input &gt; Limit">
        <AlarmConfig/>
      </AlarmCondition>
    </AlarmConditions>
    <Data Format="L5K"><![CDATA[0]]></Data>
    <Data Format="Decorated"><DataValue DataType="DINT" Radix="Decimal" Value="0"/></Data>
  </Tag>
</Tags>
<Programs>
  <Program Name="MainProgram" Type="Normal">
    <Tags/>
    <Routines/>
  </Program>
</Programs>
<Tasks/>
</Controller></RSLogix5000Content>
"""


class FakeProject:
    """Lightweight project-like object for testing alarm functions."""

    def __init__(self):
        parser = etree.XMLParser(strip_cdata=False)
        self.root = etree.fromstring(_PROJECT_XML.encode(), parser)
        self._controller = self.root.find("Controller")

    def _ensure_loaded(self):
        pass  # always loaded

    @property
    def controller(self):
        return self._controller

    @property
    def controller_tags_element(self):
        return self._controller.find('Tags')

    def get_controller_tag_element(self, name):
        tags = self._controller.find('Tags')
        if tags is None:
            return None
        for t in tags.findall('Tag'):
            if t.get('Name') == name:
                return t
        return None

    def get_program_tag_element(self, program_name, tag_name):
        progs = self._controller.find('Programs')
        if progs is None:
            return None
        for p in progs.findall('Program'):
            if p.get('Name') == program_name:
                tags = p.find('Tags')
                if tags is not None:
                    for t in tags.findall('Tag'):
                        if t.get('Name') == tag_name:
                            return t
        return None

    def get_program_element(self, name):
        progs = self._controller.find('Programs')
        if progs is None:
            return None
        for p in progs.findall('Program'):
            if p.get('Name') == name:
                return p
        return None

    @property
    def alarm_definitions_element(self):
        return self._controller.find('AlarmDefinitions')

    def get_alarm_definition(self, data_type_name):
        alarm_defs = self.alarm_definitions_element
        if alarm_defs is None:
            return None
        for dtad in alarm_defs.findall('DatatypeAlarmDefinition'):
            if dtad.get('Name') == data_type_name:
                return dtad
        return None

    def get_data_type_definition(self, name):
        dts = self._controller.find('DataTypes')
        if dts is not None:
            for dt in dts.findall('DataType'):
                if dt.get('Name') == name:
                    return dt
        aois = self._controller.find('AddOnInstructionDefinitions')
        if aois is not None:
            for aoi in aois.findall('AddOnInstructionDefinition'):
                if aoi.get('Name') == name:
                    return aoi
        return None

    # Delegate project-level alarm methods to L5XProject implementations
    def list_alarm_definitions(self):
        from l5x_agent_toolkit.project import L5XProject
        return L5XProject.list_alarm_definitions(self)

    def create_alarm_definition(self, data_type_name, members):
        from l5x_agent_toolkit.project import L5XProject
        return L5XProject.create_alarm_definition(self, data_type_name, members)

    def remove_alarm_definition(self, data_type_name):
        from l5x_agent_toolkit.project import L5XProject
        return L5XProject.remove_alarm_definition(self, data_type_name)


# ===================================================================
# Tests for ALARM_DIGITAL Tag Operations
# ===================================================================

class TestCreateAlarmDigitalTag:
    def test_basic_create(self):
        proj = FakeProject()
        tag = _tags.create_alarm_digital_tag(
            proj, name="NewAlarm", message="Test alarm message",
        )
        assert tag.get('Name') == 'NewAlarm'
        assert tag.get('DataType') == 'ALARM_DIGITAL'
        assert tag.get('TagType') == 'Base'

        # Verify Data Format="Alarm" structure
        data = tag.find("Data[@Format='Alarm']")
        assert data is not None

        params = data.find('AlarmDigitalParameters')
        assert params is not None
        assert params.get('Severity') == '500'
        assert params.get('AckRequired') == 'true'
        assert params.get('Latched') == 'false'

        # Verify message
        text = data.find('.//Text')
        assert text is not None
        assert 'Test alarm message' in text.text

    def test_no_l5k_or_decorated_data(self):
        proj = FakeProject()
        tag = _tags.create_alarm_digital_tag(
            proj, name="NoL5K", message="msg",
        )
        assert tag.find("Data[@Format='L5K']") is None
        assert tag.find("Data[@Format='Decorated']") is None

    def test_custom_severity_and_latched(self):
        proj = FakeProject()
        tag = _tags.create_alarm_digital_tag(
            proj, name="CustomAlarm", message="msg",
            severity=900, latched=True, ack_required=False,
        )
        params = tag.find(".//AlarmDigitalParameters")
        assert params.get('Severity') == '900'
        assert params.get('Latched') == 'true'
        assert params.get('AckRequired') == 'false'

    def test_severity_validation(self):
        proj = FakeProject()
        with pytest.raises(ValueError, match="severity"):
            _tags.create_alarm_digital_tag(
                proj, name="BadSev", message="msg", severity=0,
            )
        with pytest.raises(ValueError, match="severity"):
            _tags.create_alarm_digital_tag(
                proj, name="BadSev2", message="msg", severity=1001,
            )

    def test_empty_message_raises(self):
        proj = FakeProject()
        with pytest.raises(ValueError, match="message"):
            _tags.create_alarm_digital_tag(
                proj, name="NoMsg", message="",
            )

    def test_duplicate_name_raises(self):
        proj = FakeProject()
        with pytest.raises(ValueError, match="already exists"):
            _tags.create_alarm_digital_tag(
                proj, name="ExistingDINT", message="msg",
            )

    def test_with_description(self):
        proj = FakeProject()
        tag = _tags.create_alarm_digital_tag(
            proj, name="DescAlarm", message="msg",
            description="My alarm description",
        )
        desc = tag.find('Description')
        assert desc is not None


class TestBatchCreateAlarmDigitalTags:
    def test_batch_create(self):
        proj = FakeProject()
        specs = [
            {"name": "Alarm1", "message": "Alarm 1 message"},
            {"name": "Alarm2", "message": "Alarm 2 message", "severity": 800},
        ]
        created = _tags.batch_create_alarm_digital_tags(proj, specs)
        assert len(created) == 2
        assert created[0].get('Name') == 'Alarm1'
        assert created[1].get('Name') == 'Alarm2'
        p2 = created[1].find('.//AlarmDigitalParameters')
        assert p2.get('Severity') == '800'


class TestGetAlarmDigitalInfo:
    def test_read_existing(self):
        proj = FakeProject()
        info = _tags.get_alarm_digital_info(proj, "ExistingAlarmDigital")
        assert info['name'] == 'ExistingAlarmDigital'
        assert info['data_type'] == 'ALARM_DIGITAL'
        assert info['Severity'] == 500
        assert info['AckRequired'] is True
        assert info['Latched'] is False
        assert info['message'] == 'Motor Error'

    def test_wrong_type_raises(self):
        proj = FakeProject()
        with pytest.raises(ValueError, match="expected"):
            _tags.get_alarm_digital_info(proj, "ExistingDINT")

    def test_not_found_raises(self):
        proj = FakeProject()
        with pytest.raises(KeyError):
            _tags.get_alarm_digital_info(proj, "NonExistent")


class TestConfigureAlarmDigitalTag:
    def test_update_severity(self):
        proj = FakeProject()
        _tags.configure_alarm_digital_tag(
            proj, "ExistingAlarmDigital", severity=999,
        )
        params = proj.get_controller_tag_element("ExistingAlarmDigital") \
            .find(".//AlarmDigitalParameters")
        assert params.get('Severity') == '999'

    def test_update_message(self):
        proj = FakeProject()
        _tags.configure_alarm_digital_tag(
            proj, "ExistingAlarmDigital", message="New message",
        )
        text = proj.get_controller_tag_element("ExistingAlarmDigital") \
            .find(".//Text")
        assert 'New message' in text.text

    def test_update_ack_required(self):
        proj = FakeProject()
        _tags.configure_alarm_digital_tag(
            proj, "ExistingAlarmDigital", ack_required=False,
        )
        params = proj.get_controller_tag_element("ExistingAlarmDigital") \
            .find(".//AlarmDigitalParameters")
        assert params.get('AckRequired') == 'false'


# ===================================================================
# Tests for Alarm Listing
# ===================================================================

class TestListAlarms:
    def test_list_all(self):
        proj = FakeProject()
        results = _tags.list_alarms(proj)
        types = {r['alarm_type'] for r in results}
        assert 'digital' in types
        assert 'condition' in types

    def test_filter_digital(self):
        proj = FakeProject()
        results = _tags.list_alarms(proj, alarm_type='digital')
        assert all(r['alarm_type'] == 'digital' for r in results)
        assert len(results) >= 1

    def test_filter_condition(self):
        proj = FakeProject()
        results = _tags.list_alarms(proj, alarm_type='condition')
        assert all(r['alarm_type'] == 'condition' for r in results)
        assert len(results) >= 1
        assert results[0]['condition_count'] == 1


# ===================================================================
# Tests for Tag Alarm Conditions
# ===================================================================

class TestGetTagAlarmConditions:
    def test_read_conditions(self):
        proj = FakeProject()
        conditions = _tags.get_tag_alarm_conditions(
            proj, "TagWithAlarmConditions",
        )
        assert len(conditions) == 1
        assert conditions[0]['Name'] == 'UDT_HiAlarm'
        assert conditions[0]['ConditionType'] == 'HI'
        assert conditions[0]['Severity'] == 750
        assert conditions[0]['OnDelay'] == 1000
        assert conditions[0]['Used'] is True

    def test_tag_without_conditions(self):
        proj = FakeProject()
        conditions = _tags.get_tag_alarm_conditions(proj, "ExistingDINT")
        assert conditions == []

    def test_not_found_raises(self):
        proj = FakeProject()
        with pytest.raises(KeyError):
            _tags.get_tag_alarm_conditions(proj, "NonExistent")


class TestConfigureTagAlarmCondition:
    def test_update_severity(self):
        proj = FakeProject()
        _tags.configure_tag_alarm_condition(
            proj, "TagWithAlarmConditions", "UDT_HiAlarm",
            severity=999,
        )
        tag = proj.get_controller_tag_element("TagWithAlarmConditions")
        ac = tag.find(".//AlarmCondition[@Name='UDT_HiAlarm']")
        assert ac.get('Severity') == '999'

    def test_update_delays(self):
        proj = FakeProject()
        _tags.configure_tag_alarm_condition(
            proj, "TagWithAlarmConditions", "UDT_HiAlarm",
            on_delay=5000, off_delay=2000,
        )
        tag = proj.get_controller_tag_element("TagWithAlarmConditions")
        ac = tag.find(".//AlarmCondition[@Name='UDT_HiAlarm']")
        assert ac.get('OnDelay') == '5000'
        assert ac.get('OffDelay') == '2000'

    def test_update_used(self):
        proj = FakeProject()
        _tags.configure_tag_alarm_condition(
            proj, "TagWithAlarmConditions", "UDT_HiAlarm",
            used=False,
        )
        tag = proj.get_controller_tag_element("TagWithAlarmConditions")
        ac = tag.find(".//AlarmCondition[@Name='UDT_HiAlarm']")
        assert ac.get('Used') == 'false'

    def test_set_message_on_empty_config(self):
        proj = FakeProject()
        _tags.configure_tag_alarm_condition(
            proj, "TagWithAlarmConditions", "UDT_HiAlarm",
            message="High value alert",
        )
        tag = proj.get_controller_tag_element("TagWithAlarmConditions")
        ac = tag.find(".//AlarmCondition[@Name='UDT_HiAlarm']")
        text = ac.find(".//Text")
        assert text is not None
        assert 'High value alert' in text.text
        msg = ac.find(".//Message")
        assert msg.get('Type') == 'CAM'

    def test_condition_not_found_raises(self):
        proj = FakeProject()
        with pytest.raises(KeyError, match="not found"):
            _tags.configure_tag_alarm_condition(
                proj, "TagWithAlarmConditions", "NonExistentCondition",
                severity=500,
            )

    def test_no_alarm_conditions_raises(self):
        proj = FakeProject()
        with pytest.raises(ValueError, match="no AlarmConditions"):
            _tags.configure_tag_alarm_condition(
                proj, "ExistingDINT", "SomeCondition",
                severity=500,
            )


# ===================================================================
# Tests for Alarm Definitions (project-level)
# ===================================================================

class TestListAlarmDefinitions:
    def test_list(self):
        proj = FakeProject()
        results = proj.list_alarm_definitions()
        assert len(results) == 1
        assert results[0]['data_type'] == 'MyUDT'
        assert results[0]['member_count'] == 1
        assert results[0]['members'][0]['name'] == 'UDT_HiAlarm'
        assert results[0]['members'][0]['severity'] == 750


class TestCreateAlarmDefinition:
    def test_create_new(self):
        proj = FakeProject()
        # First add a second data type
        dts = proj.controller.find('DataTypes')
        dt2 = etree.SubElement(dts, 'DataType')
        dt2.set('Name', 'AnotherUDT')
        dt2.set('Family', 'NoFamily')
        dt2.set('Class', 'User')

        dtad = proj.create_alarm_definition('AnotherUDT', [
            {
                'name': 'TestAlarm',
                'input': '.SomeMember',
                'condition_type': 'TRIP',
                'message': 'Test fault',
            },
        ])
        assert dtad.get('Name') == 'AnotherUDT'
        mads = dtad.findall('MemberAlarmDefinition')
        assert len(mads) == 1
        assert mads[0].get('Name') == 'TestAlarm'
        assert mads[0].get('Input') == '.SomeMember'
        assert mads[0].get('ConditionType') == 'TRIP'
        # Verify message
        text = mads[0].find('.//Text')
        assert text is not None
        assert 'Test fault' in text.text
        msg = mads[0].find('.//Message')
        assert msg.get('Type') == 'ADM'

    def test_duplicate_raises(self):
        proj = FakeProject()
        with pytest.raises(ValueError, match="already exists"):
            proj.create_alarm_definition('MyUDT', [
                {'name': 'X', 'input': '.V', 'condition_type': 'TRIP'},
            ])

    def test_invalid_input_raises(self):
        proj = FakeProject()
        dts = proj.controller.find('DataTypes')
        dt2 = etree.SubElement(dts, 'DataType')
        dt2.set('Name', 'BadInputUDT')
        dt2.set('Family', 'NoFamily')
        dt2.set('Class', 'User')

        with pytest.raises(ValueError, match="must start with '.'"):
            proj.create_alarm_definition('BadInputUDT', [
                {'name': 'X', 'input': 'NoLeadingDot', 'condition_type': 'TRIP'},
            ])

    def test_invalid_condition_type_raises(self):
        proj = FakeProject()
        dts = proj.controller.find('DataTypes')
        dt2 = etree.SubElement(dts, 'DataType')
        dt2.set('Name', 'BadCtypeUDT')
        dt2.set('Family', 'NoFamily')
        dt2.set('Class', 'User')

        with pytest.raises(ValueError, match="Invalid condition_type"):
            proj.create_alarm_definition('BadCtypeUDT', [
                {'name': 'X', 'input': '.V', 'condition_type': 'INVALID'},
            ])


class TestRemoveAlarmDefinition:
    def test_remove_existing(self):
        proj = FakeProject()
        removed = proj.remove_alarm_definition('MyUDT')
        assert removed.get('Name') == 'MyUDT'
        # Verify it's gone
        assert proj.get_alarm_definition('MyUDT') is None

    def test_remove_nonexistent_raises(self):
        proj = FakeProject()
        with pytest.raises(KeyError):
            proj.remove_alarm_definition('NonExistentType')


# ===================================================================
# Tests for schema constants
# ===================================================================

class TestSchemaConstants:
    def test_controller_child_order_includes_alarm_definitions(self):
        from l5x_agent_toolkit.schema import CONTROLLER_CHILD_ORDER
        assert 'AlarmDefinitions' in CONTROLLER_CHILD_ORDER
        aoi_idx = CONTROLLER_CHILD_ORDER.index('AddOnInstructionDefinitions')
        alarm_idx = CONTROLLER_CHILD_ORDER.index('AlarmDefinitions')
        tags_idx = CONTROLLER_CHILD_ORDER.index('Tags')
        assert aoi_idx < alarm_idx < tags_idx

    def test_alarm_digital_defaults_has_all_attrs(self):
        assert 'Severity' in ALARM_DIGITAL_DEFAULTS
        assert 'AckRequired' in ALARM_DIGITAL_DEFAULTS
        assert 'UseProgTime' in ALARM_DIGITAL_DEFAULTS
        assert ALARM_DIGITAL_DEFAULTS['Condition'] == 'true'


class TestAlarmTagAttributes:
    """Alarm tags must have Class='Standard' at controller scope, no Constant."""
    def test_controller_alarm_has_class(self):
        proj = FakeProject()
        tag = _tags.create_alarm_digital_tag(
            proj, name="CAlarm", message="test",
        )
        assert tag.get('Class') == 'Standard'

    def test_alarm_no_constant_attribute(self):
        proj = FakeProject()
        tag = _tags.create_alarm_digital_tag(
            proj, name="NoConst", message="test",
        )
        assert tag.get('Constant') is None
