"""Tests for the consolidated MCP server tools.

Verifies that the refactored batch/unified tool endpoints correctly
dispatch to the underlying backing modules and return structured results.
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import patch, MagicMock
from lxml import etree

from l5x_agent_toolkit import mcp_server


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

_MINIMAL_L5X = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="37.00"
    TargetName="TestCtrl" TargetType="Controller" ContainsContext="false"
    Owner="" ExportDate="Thu Jan 01 00:00:00 2099"
    ExportOptions="">
<Controller Use="Target" Name="TestCtrl" ProcessorType="1756-L85E"
    MajorRev="37" MinorRev="11">
<DataTypes/>
<Modules/>
<AddOnInstructionDefinitions/>
<Tags>
  <Tag Name="MyDINT" TagType="Base" DataType="DINT" Radix="Decimal"
       Class="Standard" ExternalAccess="Read/Write">
    <Description><![CDATA[Test tag]]></Description>
    <Data Format="L5K">0</Data>
    <Data Format="Decorated">
      <DataValue DataType="DINT" Radix="Decimal" Value="0"/>
    </Data>
  </Tag>
  <Tag Name="MyBOOL" TagType="Base" DataType="BOOL" Radix="Decimal"
       Class="Standard" ExternalAccess="Read/Write">
    <Data Format="L5K">0</Data>
    <Data Format="Decorated">
      <DataValue DataType="BOOL" Radix="Decimal" Value="0"/>
    </Data>
  </Tag>
  <Tag Name="MyTimer" TagType="Base" DataType="TIMER" Radix="NullType"
       Class="Standard" ExternalAccess="Read/Write">
    <Data Format="L5K">[0,0,0]</Data>
    <Data Format="Decorated">
      <Structure DataType="TIMER">
        <DataValueMember Name="PRE" DataType="DINT" Radix="Decimal" Value="0"/>
        <DataValueMember Name="ACC" DataType="DINT" Radix="Decimal" Value="0"/>
        <DataValueMember Name="EN" DataType="BOOL" Value="0"/>
        <DataValueMember Name="TT" DataType="BOOL" Value="0"/>
        <DataValueMember Name="DN" DataType="BOOL" Value="0"/>
      </Structure>
    </Data>
  </Tag>
</Tags>
<Programs>
  <Program Name="MainProgram" Type="Normal" Class="Standard"
           MainRoutineName="MainRoutine">
    <Tags/>
    <Routines>
      <Routine Name="MainRoutine" Type="RLL">
        <RLLContent>
          <Rung Number="0" Type="N">
            <Text><![CDATA[XIC(MyBOOL)OTE(MyDINT);]]></Text>
            <Comment><![CDATA[Test rung]]></Comment>
          </Rung>
        </RLLContent>
      </Routine>
    </Routines>
  </Program>
</Programs>
<Tasks>
  <Task Name="MainTask" Type="CONTINUOUS" Priority="10" Rate="10">
    <ScheduledPrograms>
      <ScheduledProgram Name="MainProgram"/>
    </ScheduledPrograms>
  </Task>
</Tasks>
</Controller>
</RSLogix5000Content>
"""


@pytest.fixture(autouse=True)
def _load_test_project(tmp_path):
    """Load a minimal L5X project before each test and clean up after."""
    f = tmp_path / "test.L5X"
    f.write_text(_MINIMAL_L5X, encoding="utf-8")
    result = mcp_server.load_project(str(f))
    assert "Error" not in result, result
    yield
    # Reset global state
    mcp_server._project = None
    mcp_server._project_path = None


# ===================================================================
# 1. Tool count verification
# ===================================================================

class TestToolCount:
    def test_total_tools_is_26(self):
        tools = list(mcp_server.mcp._tool_manager._tools.values())
        assert len(tools) == 26


# ===================================================================
# 2. query_project
# ===================================================================

class TestQueryProject:
    def test_query_all(self):
        raw = mcp_server.query_project(entity="all")
        data = json.loads(raw)
        assert "programs" in data
        assert "tags" in data
        assert "modules" in data
        assert "aois" in data
        assert "udts" in data
        assert "tasks" in data
        assert "MainProgram" in data["programs"]

    def test_query_programs(self):
        raw = mcp_server.query_project(entity="programs")
        data = json.loads(raw)
        assert "MainProgram" in data["programs"]

    def test_query_tags_controller(self):
        raw = mcp_server.query_project(entity="tags", scope="controller")
        data = json.loads(raw)
        names = [t["name"] for t in data["tags"]]
        assert "MyDINT" in names
        assert all(t["scope"] == "controller" for t in data["tags"])

    def test_query_tags_all_scopes(self):
        raw = mcp_server.query_project(entity="tags", scope="")
        data = json.loads(raw)
        # Should include controller tags
        names = [t["name"] for t in data["tags"]]
        assert "MyDINT" in names

    def test_query_routines(self):
        raw = mcp_server.query_project(
            entity="routines", program_name="MainProgram",
        )
        data = json.loads(raw)
        names = [r["name"] for r in data["routines"]]
        assert "MainRoutine" in names

    def test_query_routines_missing_program(self):
        raw = mcp_server.query_project(entity="routines")
        assert "Error" in raw

    def test_query_unknown_entity(self):
        raw = mcp_server.query_project(entity="bogus")
        assert "Error" in raw

    def test_name_filter(self):
        raw = mcp_server.query_project(
            entity="tags", scope="controller", name_filter="My*",
        )
        data = json.loads(raw)
        assert len(data["tags"]) >= 2  # MyDINT, MyBOOL, MyTimer
        for t in data["tags"]:
            assert t["name"].startswith("My")

    def test_name_filter_no_match(self):
        raw = mcp_server.query_project(
            entity="tags", scope="controller", name_filter="ZZZ*",
        )
        data = json.loads(raw)
        assert len(data["tags"]) == 0


# ===================================================================
# 3. get_entity_info
# ===================================================================

class TestGetEntityInfo:
    def test_tag_basic(self):
        raw = mcp_server.get_entity_info(entity="tag", name="MyDINT")
        data = json.loads(raw)
        assert data["name"] == "MyDINT"
        assert data["data_type"] == "DINT"

    def test_tag_search_all_scopes(self):
        raw = mcp_server.get_entity_info(
            entity="tag", name="MyDINT", scope="",
        )
        data = json.loads(raw)
        assert data["name"] == "MyDINT"

    def test_tag_member_dot_notation(self):
        raw = mcp_server.get_entity_info(
            entity="tag", name="MyTimer.PRE",
        )
        data = json.loads(raw)
        assert data["member_path"] == "PRE"
        assert "member_value" in data

    def test_tag_with_references_include(self):
        raw = mcp_server.get_entity_info(
            entity="tag", name="MyBOOL", include="references",
        )
        data = json.loads(raw)
        assert "references" in data

    def test_rung_entity(self):
        raw = mcp_server.get_entity_info(
            entity="rung", name="0",
            program_name="MainProgram", routine_name="MainRoutine",
        )
        data = json.loads(raw)
        assert "text" in data
        assert "XIC" in data["text"]

    def test_rung_missing_params(self):
        raw = mcp_server.get_entity_info(entity="rung", name="0")
        assert "Error" in raw

    def test_unknown_entity(self):
        raw = mcp_server.get_entity_info(entity="bogus", name="x")
        assert "Error" in raw


# ===================================================================
# 4. manage_tags
# ===================================================================

class TestManageTags:
    def test_create_single(self):
        ops = [{"action": "create", "name": "NewTag1", "data_type": "DINT"}]
        raw = mcp_server.manage_tags(json.dumps(ops))
        data = json.loads(raw)
        assert data["succeeded"] == 1
        assert data["failed"] == 0

    def test_create_multiple(self):
        ops = [
            {"action": "create", "name": "BatchA", "data_type": "BOOL"},
            {"action": "create", "name": "BatchB", "data_type": "REAL"},
            {"action": "create", "name": "BatchC", "data_type": "INT"},
        ]
        raw = mcp_server.manage_tags(json.dumps(ops))
        data = json.loads(raw)
        assert data["succeeded"] == 3

    def test_create_with_description(self):
        ops = [{"action": "create", "name": "Described",
                "data_type": "DINT", "description": "Has a description"}]
        raw = mcp_server.manage_tags(json.dumps(ops))
        data = json.loads(raw)
        assert data["succeeded"] == 1
        # Verify the description stuck
        info_raw = mcp_server.get_entity_info(entity="tag", name="Described")
        info = json.loads(info_raw)
        assert info["description"] == "Has a description"

    def test_delete(self):
        # Create then delete
        mcp_server.manage_tags(json.dumps(
            [{"action": "create", "name": "ToDelete", "data_type": "DINT"}]
        ))
        raw = mcp_server.manage_tags(json.dumps(
            [{"action": "delete", "name": "ToDelete"}]
        ))
        data = json.loads(raw)
        assert data["succeeded"] == 1

    def test_rename(self):
        mcp_server.manage_tags(json.dumps(
            [{"action": "create", "name": "OldName", "data_type": "DINT"}]
        ))
        raw = mcp_server.manage_tags(json.dumps(
            [{"action": "rename", "name": "OldName", "new_name": "NewName"}]
        ))
        data = json.loads(raw)
        assert data["succeeded"] == 1

    def test_copy(self):
        raw = mcp_server.manage_tags(json.dumps(
            [{"action": "copy", "name": "MyDINT", "new_name": "MyDINT_Copy"}]
        ))
        data = json.loads(raw)
        assert data["succeeded"] == 1

    def test_create_alias(self):
        raw = mcp_server.manage_tags(json.dumps(
            [{"action": "create_alias", "name": "AliasTag",
              "alias_for": "MyDINT"}]
        ))
        data = json.loads(raw)
        assert data["succeeded"] == 1

    def test_unknown_action(self):
        raw = mcp_server.manage_tags(json.dumps(
            [{"action": "explode", "name": "x"}]
        ))
        data = json.loads(raw)
        assert data["failed"] == 1

    def test_invalid_json(self):
        raw = mcp_server.manage_tags("not json")
        assert "Error" in raw

    def test_partial_failure(self):
        ops = [
            {"action": "create", "name": "GoodTag", "data_type": "DINT"},
            {"action": "create", "name": "MyDINT", "data_type": "DINT"},  # duplicate
        ]
        raw = mcp_server.manage_tags(json.dumps(ops))
        data = json.loads(raw)
        assert data["succeeded"] == 1
        assert data["failed"] == 1

    def test_per_op_scope_override(self):
        ops = [
            {"action": "create", "name": "ProgTag1", "data_type": "DINT",
             "scope": "program", "program_name": "MainProgram"},
        ]
        raw = mcp_server.manage_tags(json.dumps(ops), scope="controller")
        data = json.loads(raw)
        assert data["succeeded"] == 1
        # Verify it's in program scope
        info_raw = mcp_server.get_entity_info(
            entity="tag", name="ProgTag1",
            scope="program", program_name="MainProgram",
        )
        info = json.loads(info_raw)
        assert info["name"] == "ProgTag1"


# ===================================================================
# 5. update_tags
# ===================================================================

class TestUpdateTags:
    def test_set_description(self):
        updates = [{"name": "MyDINT", "description": "Updated desc"}]
        raw = mcp_server.update_tags(json.dumps(updates))
        data = json.loads(raw)
        assert data["succeeded"] == 1
        assert "description" in data["details"][0]["changes"]

    def test_set_value(self):
        updates = [{"name": "MyDINT", "value": "42"}]
        raw = mcp_server.update_tags(json.dumps(updates))
        data = json.loads(raw)
        assert data["succeeded"] == 1
        assert "value=42" in data["details"][0]["changes"][0]

    def test_set_member_value(self):
        updates = [{"name": "MyTimer", "members": {"PRE": "5000"}}]
        raw = mcp_server.update_tags(json.dumps(updates))
        data = json.loads(raw)
        assert data["succeeded"] == 1

    def test_combined_update(self):
        updates = [
            {"name": "MyDINT", "value": "99", "description": "Combined"},
        ]
        raw = mcp_server.update_tags(json.dumps(updates))
        data = json.loads(raw)
        assert data["succeeded"] == 1
        changes = data["details"][0]["changes"]
        assert len(changes) == 2  # value + description

    def test_multiple_tags(self):
        updates = [
            {"name": "MyDINT", "value": "1"},
            {"name": "MyBOOL", "value": "1"},
        ]
        raw = mcp_server.update_tags(json.dumps(updates))
        data = json.loads(raw)
        assert data["succeeded"] == 2

    def test_invalid_json(self):
        raw = mcp_server.update_tags("bad json")
        assert "Error" in raw


# ===================================================================
# 6. manage_rungs
# ===================================================================

class TestManageRungs:
    def test_add_rung(self):
        ops = [{"action": "add", "text": "NOP();"}]
        raw = mcp_server.manage_rungs("MainProgram", "MainRoutine",
                                       json.dumps(ops))
        data = json.loads(raw)
        assert data["succeeded"] == 1

    def test_add_multiple_rungs(self):
        ops = [
            {"action": "add", "text": "NOP();", "comment": "Rung A"},
            {"action": "add", "text": "NOP();", "comment": "Rung B"},
        ]
        raw = mcp_server.manage_rungs("MainProgram", "MainRoutine",
                                       json.dumps(ops))
        data = json.loads(raw)
        assert data["succeeded"] == 2

    def test_modify_rung_text_and_comment(self):
        ops = [
            {"action": "modify", "rung_number": 0,
             "text": "NOP();", "comment": "Modified"},
        ]
        raw = mcp_server.manage_rungs("MainProgram", "MainRoutine",
                                       json.dumps(ops))
        data = json.loads(raw)
        assert data["succeeded"] == 1

    def test_modify_comment_only(self):
        ops = [
            {"action": "modify", "rung_number": 0,
             "comment": "New comment only"},
        ]
        raw = mcp_server.manage_rungs("MainProgram", "MainRoutine",
                                       json.dumps(ops))
        data = json.loads(raw)
        assert data["succeeded"] == 1

    def test_delete_rung(self):
        # Add one so we have 2, then delete one
        mcp_server.manage_rungs("MainProgram", "MainRoutine",
                                 json.dumps([{"action": "add", "text": "NOP();"}]))
        ops = [{"action": "delete", "rung_number": 1}]
        raw = mcp_server.manage_rungs("MainProgram", "MainRoutine",
                                       json.dumps(ops))
        data = json.loads(raw)
        assert data["succeeded"] == 1

    def test_duplicate_rung(self):
        ops = [
            {"action": "duplicate", "rung_number": 0,
             "substitutions": {"MyBOOL": "MyDINT"}},
        ]
        raw = mcp_server.manage_rungs("MainProgram", "MainRoutine",
                                       json.dumps(ops))
        data = json.loads(raw)
        assert data["succeeded"] == 1

    def test_unknown_action(self):
        ops = [{"action": "flip"}]
        raw = mcp_server.manage_rungs("MainProgram", "MainRoutine",
                                       json.dumps(ops))
        data = json.loads(raw)
        assert data["failed"] == 1

    def test_invalid_json(self):
        raw = mcp_server.manage_rungs("MainProgram", "MainRoutine", "{bad")
        assert "Error" in raw


# ===================================================================
# 7. analyze_rung_text
# ===================================================================

class TestAnalyzeRungText:
    def test_validate_valid(self):
        raw = mcp_server.analyze_rung_text("XIC(a)OTE(b);", action="validate")
        assert raw == "Valid"

    def test_validate_invalid(self):
        raw = mcp_server.analyze_rung_text("XIC(a)OTE(b)", action="validate")
        # Missing semicolon
        assert raw != "Valid"

    def test_extract_tags(self):
        raw = mcp_server.analyze_rung_text(
            "XIC(Start)TON(Timer1,1000,0)OTE(Run);",
            action="extract_tags",
        )
        tags = json.loads(raw)
        assert "Start" in tags
        assert "Timer1" in tags
        assert "Run" in tags

    def test_substitute(self):
        raw = mcp_server.analyze_rung_text(
            "XIC(OldTag)OTE(OldOut);",
            action="substitute",
            substitutions_json='{"OldTag": "NewTag", "OldOut": "NewOut"}',
        )
        assert "NewTag" in raw
        assert "NewOut" in raw

    def test_substitute_missing_json(self):
        raw = mcp_server.analyze_rung_text(
            "XIC(a);", action="substitute",
        )
        assert "Error" in raw

    def test_unknown_action(self):
        raw = mcp_server.analyze_rung_text("NOP();", action="bogus")
        assert "Error" in raw


# ===================================================================
# 8. manage_alarms
# ===================================================================

class TestManageAlarms:
    def test_create_digital(self):
        ops = [
            {"action": "create_digital", "name": "TestAlarm",
             "message": "Test alarm message", "severity": 750},
        ]
        raw = mcp_server.manage_alarms(json.dumps(ops))
        data = json.loads(raw)
        assert data["succeeded"] == 1

    def test_create_and_configure_digital(self):
        ops = [
            {"action": "create_digital", "name": "AlarmCfg",
             "message": "Initial", "severity": 500},
            {"action": "configure_digital", "name": "AlarmCfg",
             "severity": 900, "message": "Updated message"},
        ]
        raw = mcp_server.manage_alarms(json.dumps(ops))
        data = json.loads(raw)
        assert data["succeeded"] == 2

    def test_create_and_get_info(self):
        ops = [
            {"action": "create_digital", "name": "AlarmInfo",
             "message": "Info test", "severity": 600},
            {"action": "get_info", "name": "AlarmInfo"},
        ]
        raw = mcp_server.manage_alarms(json.dumps(ops))
        data = json.loads(raw)
        assert data["succeeded"] == 2
        # The get_info result should have data
        info_result = data["details"][1]
        assert info_result["action"] == "get_info"
        assert "data" in info_result

    def test_batch_create_digital(self):
        ops = [
            {"action": "create_digital", "name": "Alarm_A",
             "message": "Alarm A"},
            {"action": "create_digital", "name": "Alarm_B",
             "message": "Alarm B", "severity": 800},
            {"action": "create_digital", "name": "Alarm_C",
             "message": "Alarm C", "latched": True},
        ]
        raw = mcp_server.manage_alarms(json.dumps(ops))
        data = json.loads(raw)
        assert data["succeeded"] == 3

    def test_unknown_action(self):
        ops = [{"action": "detonate"}]
        raw = mcp_server.manage_alarms(json.dumps(ops))
        data = json.loads(raw)
        assert data["failed"] == 1

    def test_invalid_json(self):
        raw = mcp_server.manage_alarms("not-json")
        assert "Error" in raw


# ===================================================================
# 9. manage_alarm_definitions
# ===================================================================

class TestManageAlarmDefinitions:
    def test_list_empty(self):
        raw = mcp_server.manage_alarm_definitions(action="list")
        assert "No alarm definitions" in raw or isinstance(json.loads(raw), list)

    def test_unknown_action(self):
        raw = mcp_server.manage_alarm_definitions(action="bogus")
        assert "Error" in raw

    def test_create_missing_params(self):
        raw = mcp_server.manage_alarm_definitions(action="create")
        assert "Error" in raw
        raw2 = mcp_server.manage_alarm_definitions(
            action="create", data_type_name="SomeType",
        )
        assert "Error" in raw2


# ===================================================================
# 10. create_export_shell
# ===================================================================

class TestCreateExportShell:
    def test_rung_shell(self):
        raw = mcp_server.create_export_shell(export_type="rung")
        assert "rung export" in raw.lower()
        assert mcp_server._project is not None

    def test_routine_shell(self):
        raw = mcp_server.create_export_shell(
            export_type="routine", routine_type="ST",
        )
        assert "routine export" in raw.lower()

    def test_program_shell(self):
        raw = mcp_server.create_export_shell(export_type="program")
        assert "program export" in raw.lower()

    def test_unknown_type(self):
        raw = mcp_server.create_export_shell(export_type="bogus")
        assert "Error" in raw


# ===================================================================
# 11. export_component
# ===================================================================

class TestExportComponent:
    def test_export_rung(self, tmp_path):
        fp = str(tmp_path / "out.L5X")
        raw = mcp_server.export_component(
            component_type="rung", name="0",
            program_name="MainProgram", routine_name="MainRoutine",
            file_path=fp,
        )
        assert "Exported" in raw
        assert "Error" not in raw

    def test_export_routine(self, tmp_path):
        fp = str(tmp_path / "routine.L5X")
        raw = mcp_server.export_component(
            component_type="routine",
            program_name="MainProgram", routine_name="MainRoutine",
            file_path=fp,
        )
        assert "Exported" in raw

    def test_export_program(self, tmp_path):
        fp = str(tmp_path / "program.L5X")
        raw = mcp_server.export_component(
            component_type="program", program_name="MainProgram",
            file_path=fp,
        )
        assert "Exported" in raw

    def test_export_tag(self, tmp_path):
        fp = str(tmp_path / "tag.L5X")
        raw = mcp_server.export_component(
            component_type="tag", name="MyDINT", file_path=fp,
        )
        assert "Exported" in raw

    def test_unknown_type(self):
        raw = mcp_server.export_component(component_type="bogus", name="x")
        assert "Error" in raw


# ===================================================================
# 12. import_component (consolidated)
# ===================================================================

class TestImportComponent:
    def test_import_rung_file(self, tmp_path):
        # First export a rung, then reimport it
        fp = str(tmp_path / "rung_exp.L5X")
        mcp_server.export_component(
            component_type="rung", name="0",
            program_name="MainProgram", routine_name="MainRoutine",
            file_path=fp,
        )
        raw = mcp_server.import_component(
            file_path=fp, conflict_resolution="skip",
        )
        assert "Error" not in raw


# ===================================================================
# 13. Integration: full workflow
# ===================================================================

class TestFullWorkflow:
    """Simulates a typical session using only consolidated tools."""

    def test_create_tags_add_rungs_and_verify(self):
        # 1. Query project
        query = json.loads(mcp_server.query_project(entity="all"))
        assert "MainProgram" in query["programs"]

        # 2. Create tags in batch
        tag_ops = [
            {"action": "create", "name": "Conv1_Run", "data_type": "BOOL",
             "description": "Conveyor 1 run command"},
            {"action": "create", "name": "Conv1_Speed", "data_type": "DINT",
             "description": "Conveyor 1 speed setpoint"},
        ]
        result = json.loads(mcp_server.manage_tags(json.dumps(tag_ops)))
        assert result["succeeded"] == 2

        # 3. Update tag values in batch
        updates = [
            {"name": "Conv1_Speed", "value": "1750"},
        ]
        result = json.loads(mcp_server.update_tags(json.dumps(updates)))
        assert result["succeeded"] == 1

        # 4. Add rungs in batch
        rung_ops = [
            {"action": "add",
             "text": "XIC(Conv1_Run)OTE(MyBOOL);",
             "comment": "Conveyor start logic"},
            {"action": "add",
             "text": "MOV(Conv1_Speed,MyDINT);",
             "comment": "Transfer speed setpoint"},
        ]
        result = json.loads(
            mcp_server.manage_rungs(
                "MainProgram", "MainRoutine", json.dumps(rung_ops),
            )
        )
        assert result["succeeded"] == 2

        # 5. Verify the rungs are there
        rungs = json.loads(
            mcp_server.get_all_rungs("MainProgram", "MainRoutine")
        )
        assert len(rungs) == 3  # 1 original + 2 new

        # 6. Verify tag info
        info = json.loads(
            mcp_server.get_entity_info(entity="tag", name="Conv1_Speed")
        )
        assert info["data_type"] == "DINT"

    def test_alarm_workflow(self):
        # Create alarms, configure, and inspect
        alarm_ops = [
            {"action": "create_digital", "name": "Alarm_Motor1",
             "message": "Motor 1 fault", "severity": 750},
            {"action": "create_digital", "name": "Alarm_Motor2",
             "message": "Motor 2 fault", "severity": 500},
        ]
        result = json.loads(mcp_server.manage_alarms(json.dumps(alarm_ops)))
        assert result["succeeded"] == 2

        # Configure first alarm
        cfg_ops = [
            {"action": "configure_digital", "name": "Alarm_Motor1",
             "severity": 900},
            {"action": "get_info", "name": "Alarm_Motor1"},
        ]
        result = json.loads(mcp_server.manage_alarms(json.dumps(cfg_ops)))
        assert result["succeeded"] == 2
        info_data = result["details"][1]["data"]
        assert info_data["Severity"] == 900
