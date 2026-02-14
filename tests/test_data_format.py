"""Tests for the data_format module."""

import pytest
from lxml import etree

from l5x_agent_toolkit.data_format import (
    get_default_radix, scalar_to_l5k, scalar_to_decorated_value,
    generate_default_l5k, generate_default_decorated,
    generate_tag_data_elements, _float_to_l5k, _parse_dimensions,
    _total_elements, _is_structure_type, _is_string_family,
    _generate_aoi_l5k,
)

class TestGetDefaultRadix:
    def test_dint(self): assert get_default_radix('DINT') == 'Decimal'
    def test_real(self): assert get_default_radix('REAL') == 'Float'
    def test_bool(self): assert get_default_radix('BOOL') == 'Decimal'
    def test_string(self): assert get_default_radix('STRING') == 'ASCII'
    def test_timer(self): assert get_default_radix('TIMER') == 'NullType'

class TestScalarToL5k:
    def test_dint_zero(self): assert scalar_to_l5k('DINT', 0) == '0'
    def test_dint_pos(self): assert scalar_to_l5k('DINT', 42) == '42'
    def test_dint_neg(self): assert scalar_to_l5k('DINT', -100) == '-100'
    def test_real(self): assert 'e' in scalar_to_l5k('REAL', 0.0).lower()
    def test_bool_t(self): assert scalar_to_l5k('BOOL', True) == '1'
    def test_bool_f(self): assert scalar_to_l5k('BOOL', False) == '0'
    def test_unsupported(self):
        with pytest.raises(ValueError): scalar_to_l5k('TIMER', 0)

class TestScalarToDecoratedValue:
    def test_dint(self): assert scalar_to_decorated_value('DINT', 0) == '0'
    def test_real(self): assert scalar_to_decorated_value('REAL', 0.0) == '0.0'
    def test_bool(self): assert scalar_to_decorated_value('BOOL', True) == '1'
    def test_unsupported(self):
        with pytest.raises(ValueError): scalar_to_decorated_value('TIMER', 0)

class TestFloatToL5k:
    def test_zero(self): assert _float_to_l5k(0.0) == '0.00000000e+000'
    def test_positive(self):
        result = _float_to_l5k(41.94)
        assert result.endswith('e+001')
    def test_negative(self): assert _float_to_l5k(-5.0).startswith('-')
    def test_large(self): assert 'e+006' in _float_to_l5k(1000000.0)
    def test_small(self): assert 'e-003' in _float_to_l5k(0.001)

class TestDimensionParsing:
    def test_single(self): assert _parse_dimensions('5') == [5]
    def test_two(self): assert _parse_dimensions('3,4') == [3, 4]
    def test_total(self): assert _total_elements('3,4') == 12

class TestGenerateDefaultL5k:
    def test_dint(self): assert generate_default_l5k('DINT') == '0'
    def test_real(self): assert generate_default_l5k('REAL') == '0.00000000e+000'
    def test_bool(self): assert generate_default_l5k('BOOL') == '0'
    def test_string(self):
        result = generate_default_l5k('STRING')
        assert result.startswith('[0,')
        assert chr(36) + "00" in result  # contains dollar-zero-zero null bytes
    def test_timer(self): assert generate_default_l5k('TIMER') == '[0,0,0]'
    def test_counter(self): assert generate_default_l5k('COUNTER') == '[0,0,0]'
    def test_control(self): assert generate_default_l5k('CONTROL') == '[0,0,0]'
    def test_dint_array(self): assert generate_default_l5k('DINT', dimensions='3') == '[0,0,0]'
    def test_timer_array(self): assert generate_default_l5k('TIMER', dimensions='2') == '[[0,0,0],[0,0,0]]'
    def test_unknown_raises(self):
        with pytest.raises(ValueError): generate_default_l5k('MyCustomUDT')

class TestGenerateDefaultDecorated:
    def test_dint(self):
        elem = generate_default_decorated('DINT')
        assert elem.tag == 'DataValue'
        assert elem.get('DataType') == 'DINT'
        assert elem.get('Radix') == 'Decimal'
    def test_string(self):
        elem = generate_default_decorated('STRING')
        assert elem.tag == 'Structure'
        names = [m.get('Name') for m in elem.findall('DataValueMember')]
        assert 'LEN' in names and 'DATA' in names
    def test_timer(self):
        elem = generate_default_decorated('TIMER')
        assert elem.tag == 'Structure'
    def test_dint_array(self):
        elem = generate_default_decorated('DINT', dimensions='3')
        assert elem.tag == 'Array'
        assert len(elem.findall('Element')) == 3
    def test_unknown_raises(self):
        with pytest.raises(ValueError): generate_default_decorated('MyCustomUDT')

class TestGenerateTagDataElements:
    def test_returns_two(self):
        elems = generate_tag_data_elements('DINT')
        assert len(elems) == 2
        assert elems[0].get('Format') == 'L5K'
        assert elems[1].get('Format') == 'Decorated'

class TestTypeChecks:
    def test_timer_struct(self): assert _is_structure_type('TIMER', None) is True
    def test_dint_not_struct(self): assert _is_structure_type('DINT', None) is False
    def test_string_family(self): assert _is_string_family('STRING') is True
    def test_dint_not_family(self): assert _is_string_family('DINT') is False


# ---------------------------------------------------------------------------
# AOI L5K data generation – regression test for declaration-order layout
# ---------------------------------------------------------------------------

# Minimal AOI definition mimicking SimpleMergeController's structure:
#   Parameters: EnableIn(BOOL), EnableOut(BOOL), Cfg_MergeTime(UINT,5000),
#     several BOOLs, several USINTs, more BOOLs, an InOut, final BOOL
#   LocalTags: MergeTimer(TIMER,[0,5000,0]), OneShot(DINT), LaneMemory(USINT[5]),
#     LaneQueuePos(DINT), LaneQueue(SINT[5]), 3x Timers, 2x BOOL
_SIMPLE_MERGE_AOI_XML = """\
<AddOnInstructionDefinition Name="SimpleMergeController">
  <Parameters>
    <Parameter Name="EnableIn" DataType="BOOL" Usage="Input"/>
    <Parameter Name="EnableOut" DataType="BOOL" Usage="Output"/>
    <Parameter Name="Cfg_MergeTime" DataType="UINT" Usage="Input">
      <DefaultData Format="L5K">5000</DefaultData></Parameter>
    <Parameter Name="Fdbk_Lane1Running" DataType="BOOL" Usage="Input">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Fdbk_Lane2Running" DataType="BOOL" Usage="Input">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Fdbk_Lane3Running" DataType="BOOL" Usage="Input">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="DischargeEnable" DataType="BOOL" Usage="Input">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="CHG_EN_Lane1" DataType="BOOL" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="CHG_EN_Lane2" DataType="BOOL" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="CHG_EN_Lane3" DataType="BOOL" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="PE_Lane1" DataType="BOOL" Usage="Input">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="PE_Lane2" DataType="BOOL" Usage="Input">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="PE_Lane3" DataType="BOOL" Usage="Input">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="PE_MergePoint" DataType="BOOL" Usage="Input">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Set_Lane1Priority" DataType="USINT" Usage="Input">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Set_Lane2Priority" DataType="USINT" Usage="Input">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Set_Lane3Priority" DataType="USINT" Usage="Input">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="EM_IN_Lane1" DataType="USINT" Usage="Input">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="EM_IN_Lane2" DataType="USINT" Usage="Input">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="EM_IN_Lane3" DataType="USINT" Usage="Input">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="EM_OUT" DataType="USINT" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Sts_ActiveMergeLane" DataType="USINT" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Sts_Lane1Queued" DataType="BOOL" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Sts_Lane2Queued" DataType="BOOL" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Sts_Lane3Queued" DataType="BOOL" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Sts_Lane1Next" DataType="BOOL" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Sts_Lane2Next" DataType="BOOL" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Sts_Lane3Next" DataType="BOOL" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Sts_Lane1Priority" DataType="BOOL" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Sts_Lane2Priority" DataType="BOOL" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Sts_Lane3Priority" DataType="BOOL" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="Sts_PrioritiesEqual" DataType="BOOL" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
    <Parameter Name="AreaSTSP" DataType="AreaSTSP" Usage="InOut"/>
    <Parameter Name="Sts_MergingActive" DataType="BOOL" Usage="Output">
      <DefaultData Format="L5K">0</DefaultData></Parameter>
  </Parameters>
  <LocalTags>
    <LocalTag Name="MergeTimer" DataType="TIMER">
      <DefaultData Format="L5K">[0,5000,0]</DefaultData></LocalTag>
    <LocalTag Name="OneShot" DataType="DINT">
      <DefaultData Format="L5K">0</DefaultData></LocalTag>
    <LocalTag Name="LaneMemory" DataType="USINT" Dimensions="5">
      <DefaultData Format="L5K">[0,0,0,0,0]</DefaultData></LocalTag>
    <LocalTag Name="LaneQueuePos" DataType="DINT">
      <DefaultData Format="L5K">0</DefaultData></LocalTag>
    <LocalTag Name="LaneQueue" DataType="SINT" Dimensions="5">
      <DefaultData Format="L5K">[0,0,0,0,0]</DefaultData></LocalTag>
    <LocalTag Name="Lane1TimeInQueue" DataType="TIMER">
      <DefaultData Format="L5K">[0,600000,0]</DefaultData></LocalTag>
    <LocalTag Name="Lane2TimeInQueue" DataType="TIMER">
      <DefaultData Format="L5K">[0,600000,0]</DefaultData></LocalTag>
    <LocalTag Name="Lane3TimeInQueue" DataType="TIMER">
      <DefaultData Format="L5K">[0,600000,0]</DefaultData></LocalTag>
    <LocalTag Name="PriorityOverrideActive" DataType="BOOL">
      <DefaultData Format="L5K">0</DefaultData></LocalTag>
    <LocalTag Name="DualPriorityActive" DataType="BOOL">
      <DefaultData Format="L5K">0</DefaultData></LocalTag>
  </LocalTags>
</AddOnInstructionDefinition>
"""


class TestGenerateAoiL5k:
    """Verify AOI L5K data uses declaration order (not atomics-then-structs)."""

    def _parse_aoi(self):
        return etree.fromstring(_SIMPLE_MERGE_AOI_XML.encode())

    def test_simple_merge_controller_layout(self):
        """L5K array must match Studio 5000's known-good export."""
        aoi_def = self._parse_aoi()
        result = _generate_aoi_l5k(aoi_def, project=None)
        expected = (
            "[1,5000,0,0,0,0,0,0,0,0,"
            "[0,5000,0],0,[0,0,0,0,0],0,[0,0,0,0,0],"
            "[0,600000,0],[0,600000,0],[0,600000,0]]"
        )
        assert result == expected

    def test_local_tags_interleave_atomics_and_structs(self):
        """Structs and atomics in local tags must stay in declaration order."""
        aoi_def = self._parse_aoi()
        result = _generate_aoi_l5k(aoi_def, project=None)
        # MergeTimer (struct) must come BEFORE OneShot (atomic) in local tags
        timer_pos = result.index('[0,5000,0]')
        oneshot_region = result[timer_pos + len('[0,5000,0]'):]
        # Next value after MergeTimer should be ,0 (OneShot), not another struct
        assert oneshot_region.startswith(',0,')

