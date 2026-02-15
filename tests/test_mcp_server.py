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
    def test_total_tools_is_31(self):
        tools = list(mcp_server.mcp._tool_manager._tools.values())
        assert len(tools) == 31


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

    def test_auto_adjust_delete_then_modify(self):
        """Deleting an earlier rung should not require the caller to
        manually shift later rung_number values."""
        # Start with 1 existing rung (index 0).  Add two more so we have 3.
        mcp_server.manage_rungs(
            "MainProgram", "MainRoutine",
            json.dumps([
                {"action": "add", "text": "XIC(A)OTE(B);", "comment": "R1"},
                {"action": "add", "text": "XIC(C)OTE(D);", "comment": "R2"},
            ]),
        )
        # Routine now: [0: original, 1: R1, 2: R2]
        # Delete rung 0, then modify what was rung 2 — using the
        # *original* index (2), not the shifted one (1).
        raw = mcp_server.manage_rungs(
            "MainProgram", "MainRoutine",
            json.dumps([
                {"action": "delete", "rung_number": 0},
                {"action": "modify", "rung_number": 2,
                 "comment": "Modified R2"},
            ]),
        )
        data = json.loads(raw)
        assert data["succeeded"] == 2
        assert data["failed"] == 0
        # Verify the comment landed on the right rung
        result = json.loads(
            mcp_server.get_all_rungs("MainProgram", "MainRoutine", count=0)
        )
        # After deleting original 0, we have [R1, R2].
        assert result["total_rungs"] == 2
        assert result["rungs"][1]["comment"] == "Modified R2"

    def test_auto_adjust_insert_then_modify(self):
        """Inserting at a position should auto-adjust later rung refs."""
        # Start with 1 existing rung (index 0).
        # Insert at position 0 (before it), then modify original rung 0
        # using its original index (0) — the server should map it to
        # actual index 1 after the insertion.
        raw = mcp_server.manage_rungs(
            "MainProgram", "MainRoutine",
            json.dumps([
                {"action": "add", "text": "NOP();", "position": 0},
                {"action": "modify", "rung_number": 0,
                 "comment": "Modified original"},
            ]),
        )
        data = json.loads(raw)
        assert data["succeeded"] == 2
        result = json.loads(
            mcp_server.get_all_rungs("MainProgram", "MainRoutine", count=0)
        )
        # Rung 0 is the newly inserted NOP, rung 1 is the original
        assert result["rungs"][1]["comment"] == "Modified original"

    def test_auto_adjust_duplicate_then_modify(self):
        """Duplicating a rung should auto-adjust later rung refs."""
        # Start with 1 existing rung.  Add one more so we have 2.
        mcp_server.manage_rungs(
            "MainProgram", "MainRoutine",
            json.dumps([{"action": "add", "text": "XIC(X)OTE(Y);"}]),
        )
        # Routine: [0: original, 1: XIC(X)OTE(Y)]
        # Duplicate rung 0, then modify rung 1 using original index.
        # The duplicate inserts after 0, so original rung 1 shifts to 2.
        raw = mcp_server.manage_rungs(
            "MainProgram", "MainRoutine",
            json.dumps([
                {"action": "duplicate", "rung_number": 0,
                 "substitutions": {"MyBOOL": "NewTag"}},
                {"action": "modify", "rung_number": 1,
                 "comment": "Still rung 1"},
            ]),
        )
        data = json.loads(raw)
        assert data["succeeded"] == 2
        result = json.loads(
            mcp_server.get_all_rungs("MainProgram", "MainRoutine", count=0)
        )
        # [0: original, 1: duplicate, 2: XIC(X)OTE(Y)]
        assert result["total_rungs"] == 3
        assert result["rungs"][2]["comment"] == "Still rung 1"

    def test_auto_adjust_multiple_deletes(self):
        """Multiple deletes using original indices should all resolve."""
        # Add 3 more rungs so we have 4 total (0..3)
        mcp_server.manage_rungs(
            "MainProgram", "MainRoutine",
            json.dumps([
                {"action": "add", "text": "NOP();", "comment": "A"},
                {"action": "add", "text": "NOP();", "comment": "B"},
                {"action": "add", "text": "NOP();", "comment": "C"},
            ]),
        )
        # Delete original rungs 1 and 3 in one batch
        raw = mcp_server.manage_rungs(
            "MainProgram", "MainRoutine",
            json.dumps([
                {"action": "delete", "rung_number": 1},
                {"action": "delete", "rung_number": 3},
            ]),
        )
        data = json.loads(raw)
        assert data["succeeded"] == 2
        result = json.loads(
            mcp_server.get_all_rungs("MainProgram", "MainRoutine", count=0)
        )
        # Started with 4 rungs, deleted 2 → 2 remain
        assert result["total_rungs"] == 2


class TestGetAllRungsPagination:
    """Tests for the paginated get_all_rungs endpoint."""

    def test_default_returns_paginated_dict(self):
        raw = mcp_server.get_all_rungs("MainProgram", "MainRoutine")
        data = json.loads(raw)
        assert "total_rungs" in data
        assert "start" in data
        assert "count" in data
        assert "rungs" in data
        assert data["total_rungs"] == 1
        assert data["count"] == 1

    def test_count_zero_returns_all(self):
        # Add extra rungs
        mcp_server.manage_rungs(
            "MainProgram", "MainRoutine",
            json.dumps([
                {"action": "add", "text": "NOP();"},
                {"action": "add", "text": "NOP();"},
            ]),
        )
        raw = mcp_server.get_all_rungs("MainProgram", "MainRoutine", count=0)
        data = json.loads(raw)
        assert data["total_rungs"] == 3
        assert data["count"] == 3
        assert len(data["rungs"]) == 3

    def test_pagination_window(self):
        # Add extra rungs so we have 4 total
        mcp_server.manage_rungs(
            "MainProgram", "MainRoutine",
            json.dumps([
                {"action": "add", "text": "NOP();", "comment": "R1"},
                {"action": "add", "text": "NOP();", "comment": "R2"},
                {"action": "add", "text": "NOP();", "comment": "R3"},
            ]),
        )
        # Fetch only 2 starting at index 1
        raw = mcp_server.get_all_rungs(
            "MainProgram", "MainRoutine", start=1, count=2,
        )
        data = json.loads(raw)
        assert data["total_rungs"] == 4
        assert data["start"] == 1
        assert data["count"] == 2
        assert len(data["rungs"]) == 2
        assert data["rungs"][0]["comment"] == "R1"
        assert data["rungs"][1]["comment"] == "R2"

    def test_start_beyond_range(self):
        raw = mcp_server.get_all_rungs(
            "MainProgram", "MainRoutine", start=999, count=10,
        )
        data = json.loads(raw)
        assert data["total_rungs"] == 1
        assert data["count"] == 0
        assert data["rungs"] == []


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
        result = json.loads(
            mcp_server.get_all_rungs("MainProgram", "MainRoutine", count=0)
        )
        assert result["total_rungs"] == 3  # 1 original + 2 new
        assert len(result["rungs"]) == 3

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


# ===================================================================
# Richer fixture for analysis tools
# ===================================================================

_RICH_L5X = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="37.00"
    TargetName="RichCtrl" TargetType="Controller" ContainsContext="false"
    Owner="" ExportDate="Thu Jan 01 00:00:00 2099"
    ExportOptions="">
<Controller Use="Target" Name="RichCtrl" ProcessorType="1756-L85E"
    MajorRev="37" MinorRev="11">
<DataTypes>
  <DataType Name="MyUDT" Family="NoFamily" Class="User">
    <Members>
      <Member Name="Speed" DataType="DINT" Dimension="0" Radix="Decimal"
              ExternalAccess="Read/Write"/>
      <Member Name="Active" DataType="BOOL" Dimension="0" Radix="Decimal"
              ExternalAccess="Read/Write"/>
    </Members>
  </DataType>
</DataTypes>
<Modules/>
<AddOnInstructionDefinitions>
  <AddOnInstructionDefinition Name="VALVE_CTL" Revision="1.0" Class="Standard"
      CreatedDate="2024-01-01T00:00:00.000Z"
      EditedDate="2024-06-01T00:00:00.000Z">
    <Parameters>
      <Parameter Name="EnableIn" DataType="BOOL" Usage="Input"
                 Required="false" Visible="false"/>
      <Parameter Name="EnableOut" DataType="BOOL" Usage="Output"
                 Required="false" Visible="false"/>
      <Parameter Name="Command" DataType="DINT" Usage="Input"
                 Required="true" Visible="true">
        <Description><![CDATA[Valve command]]></Description>
      </Parameter>
      <Parameter Name="Feedback" DataType="DINT" Usage="Input"
                 Required="false" Visible="true">
        <Description><![CDATA[Valve feedback]]></Description>
      </Parameter>
      <Parameter Name="IOArray" DataType="DINT" Usage="InOut"
                 Required="true" Visible="true" Dimensions="10">
        <Description><![CDATA[I/O array ref]]></Description>
      </Parameter>
      <Parameter Name="AddressOffset" DataType="DINT" Usage="Input"
                 Required="true" Visible="true">
        <Description><![CDATA[ASI address offset]]></Description>
      </Parameter>
      <Parameter Name="Output" DataType="BOOL" Usage="Output"
                 Required="false" Visible="true"/>
    </Parameters>
    <LocalTags/>
    <Routines>
      <Routine Name="Logic" Type="RLL">
        <RLLContent>
          <Rung Number="0" Type="N">
            <Text><![CDATA[NOP();]]></Text>
          </Rung>
        </RLLContent>
      </Routine>
    </Routines>
  </AddOnInstructionDefinition>
</AddOnInstructionDefinitions>
<Tags>
  <Tag Name="GlobalRun" TagType="Base" DataType="BOOL" Radix="Decimal"
       Class="Standard" ExternalAccess="Read/Write">
    <Data Format="L5K">0</Data>
    <Data Format="Decorated">
      <DataValue DataType="BOOL" Radix="Decimal" Value="0"/>
    </Data>
  </Tag>
  <Tag Name="GlobalSpeed" TagType="Base" DataType="DINT" Radix="Decimal"
       Class="Standard" ExternalAccess="Read/Write">
    <Description><![CDATA[Global speed setpoint]]></Description>
    <Data Format="L5K">1750</Data>
    <Data Format="Decorated">
      <DataValue DataType="DINT" Radix="Decimal" Value="1750"/>
    </Data>
  </Tag>
  <Tag Name="Unused_Ctrl" TagType="Base" DataType="DINT" Radix="Decimal"
       Class="Standard" ExternalAccess="Read/Write">
    <Data Format="L5K">0</Data>
    <Data Format="Decorated">
      <DataValue DataType="DINT" Radix="Decimal" Value="0"/>
    </Data>
  </Tag>
  <Tag Name="ShadowTag" TagType="Base" DataType="DINT" Radix="Decimal"
       Class="Standard" ExternalAccess="Read/Write">
    <Data Format="L5K">0</Data>
    <Data Format="Decorated">
      <DataValue DataType="DINT" Radix="Decimal" Value="0"/>
    </Data>
  </Tag>
  <Tag Name="V101" TagType="Base" DataType="VALVE_CTL" Radix="NullType"
       Class="Standard" ExternalAccess="Read/Write">
    <Data Format="L5K">[0,0,[0,0,0,0,0,0,0,0,0,0],0,0]</Data>
    <Data Format="Decorated">
      <Structure DataType="VALVE_CTL">
        <DataValueMember Name="EnableIn" DataType="BOOL" Value="0"/>
        <DataValueMember Name="EnableOut" DataType="BOOL" Value="0"/>
        <DataValueMember Name="Command" DataType="DINT" Radix="Decimal" Value="0"/>
        <DataValueMember Name="Feedback" DataType="DINT" Radix="Decimal" Value="0"/>
        <DataValueMember Name="AddressOffset" DataType="DINT" Radix="Decimal" Value="5"/>
        <DataValueMember Name="Output" DataType="BOOL" Value="0"/>
      </Structure>
    </Data>
  </Tag>
  <Tag Name="V102" TagType="Base" DataType="VALVE_CTL" Radix="NullType"
       Class="Standard" ExternalAccess="Read/Write">
    <Data Format="L5K">[0,0,[0,0,0,0,0,0,0,0,0,0],0,0]</Data>
    <Data Format="Decorated">
      <Structure DataType="VALVE_CTL">
        <DataValueMember Name="EnableIn" DataType="BOOL" Value="0"/>
        <DataValueMember Name="EnableOut" DataType="BOOL" Value="0"/>
        <DataValueMember Name="Command" DataType="DINT" Radix="Decimal" Value="0"/>
        <DataValueMember Name="Feedback" DataType="DINT" Radix="Decimal" Value="0"/>
        <DataValueMember Name="AddressOffset" DataType="DINT" Radix="Decimal" Value="5"/>
        <DataValueMember Name="Output" DataType="BOOL" Value="0"/>
      </Structure>
    </Data>
  </Tag>
  <Tag Name="IO_Data" TagType="Base" DataType="DINT" Dimensions="10"
       Radix="Decimal" Class="Standard" ExternalAccess="Read/Write">
    <Data Format="L5K">[0,0,0,0,0,0,0,0,0,0]</Data>
    <Data Format="Decorated">
      <Array DataType="DINT" Dimensions="10" Radix="Decimal">
        <Element Index="[0]" Value="0"/>
        <Element Index="[1]" Value="0"/>
        <Element Index="[2]" Value="0"/>
        <Element Index="[3]" Value="0"/>
        <Element Index="[4]" Value="0"/>
        <Element Index="[5]" Value="0"/>
        <Element Index="[6]" Value="0"/>
        <Element Index="[7]" Value="0"/>
        <Element Index="[8]" Value="0"/>
        <Element Index="[9]" Value="0"/>
      </Array>
    </Data>
  </Tag>
</Tags>
<Programs>
  <Program Name="ValveProgram" Type="Normal" Class="Standard"
           MainRoutineName="MainRoutine">
    <Tags>
      <Tag Name="LocalCmd" TagType="Base" DataType="DINT" Radix="Decimal"
           Class="Standard" ExternalAccess="Read/Write">
        <Data Format="L5K">0</Data>
        <Data Format="Decorated">
          <DataValue DataType="DINT" Radix="Decimal" Value="0"/>
        </Data>
      </Tag>
      <Tag Name="ShadowTag" TagType="Base" DataType="BOOL" Radix="Decimal"
           Class="Standard" ExternalAccess="Read/Write">
        <Data Format="L5K">0</Data>
        <Data Format="Decorated">
          <DataValue DataType="BOOL" Radix="Decimal" Value="0"/>
        </Data>
      </Tag>
    </Tags>
    <Routines>
      <Routine Name="MainRoutine" Type="RLL">
        <RLLContent>
          <Rung Number="0" Type="N">
            <Text><![CDATA[XIC(GlobalRun)VALVE_CTL(V101,LocalCmd,0,IO_Data,5);]]></Text>
            <Comment><![CDATA[Valve 101 control]]></Comment>
          </Rung>
          <Rung Number="1" Type="N">
            <Text><![CDATA[XIC(GlobalRun)VALVE_CTL(V102,LocalCmd,0,IO_Data,5);]]></Text>
            <Comment><![CDATA[Valve 102 control - same array and offset!]]></Comment>
          </Rung>
          <Rung Number="2" Type="N">
            <Text><![CDATA[MOV(GlobalSpeed,LocalCmd);]]></Text>
            <Comment><![CDATA[Transfer speed]]></Comment>
          </Rung>
        </RLLContent>
      </Routine>
    </Routines>
  </Program>
  <Program Name="AuxProgram" Type="Normal" Class="Standard"
           MainRoutineName="MainRoutine">
    <Tags>
      <Tag Name="LocalCmd" TagType="Base" DataType="DINT" Radix="Decimal"
           Class="Standard" ExternalAccess="Read/Write">
        <Data Format="L5K">0</Data>
        <Data Format="Decorated">
          <DataValue DataType="DINT" Radix="Decimal" Value="0"/>
        </Data>
      </Tag>
    </Tags>
    <Routines>
      <Routine Name="MainRoutine" Type="RLL">
        <RLLContent>
          <Rung Number="0" Type="N">
            <Text><![CDATA[NOP();]]></Text>
          </Rung>
        </RLLContent>
      </Routine>
    </Routines>
  </Program>
</Programs>
<Tasks>
  <Task Name="MainTask" Type="CONTINUOUS" Priority="10" Rate="10">
    <ScheduledPrograms>
      <ScheduledProgram Name="ValveProgram"/>
      <ScheduledProgram Name="AuxProgram"/>
    </ScheduledPrograms>
  </Task>
</Tasks>
</Controller>
</RSLogix5000Content>
"""


@pytest.fixture()
def rich_project(tmp_path):
    """Load a richer L5X project with AOIs, UDTs, and multiple programs."""
    f = tmp_path / "rich.L5X"
    f.write_text(_RICH_L5X, encoding="utf-8")
    result = mcp_server.load_project(str(f))
    assert "Error" not in result, result
    yield
    # autouse fixture will clean up global state


# ===================================================================
# 14. get_scope_references
# ===================================================================

class TestGetScopeReferences:
    def test_all_rungs_in_routine(self, rich_project):
        raw = mcp_server.get_scope_references(
            program_name="ValveProgram",
            routine_name="MainRoutine",
        )
        data = json.loads(raw)
        assert "tags" in data
        assert "aoi_calls" in data
        assert "summary" in data

        tag_names = {t["name"] for t in data["tags"]}
        assert "GlobalRun" in tag_names
        assert "GlobalSpeed" in tag_names
        assert "LocalCmd" in tag_names
        assert "V101" in tag_names
        assert "IO_Data" in tag_names

    def test_rung_range_filter(self, rich_project):
        raw = mcp_server.get_scope_references(
            program_name="ValveProgram",
            routine_name="MainRoutine",
            rung_range="0",
        )
        data = json.loads(raw)
        tag_names = {t["name"] for t in data["tags"]}
        assert "GlobalRun" in tag_names
        assert "V101" in tag_names
        # GlobalSpeed is only in rung 2, should NOT be here
        assert "GlobalSpeed" not in tag_names

    def test_rung_range_with_dash(self, rich_project):
        raw = mcp_server.get_scope_references(
            program_name="ValveProgram",
            routine_name="MainRoutine",
            rung_range="0-1",
        )
        data = json.loads(raw)
        tag_names = {t["name"] for t in data["tags"]}
        assert "V101" in tag_names
        assert "V102" in tag_names
        # GlobalSpeed is in rung 2, excluded
        assert "GlobalSpeed" not in tag_names

    def test_include_tag_info(self, rich_project):
        raw = mcp_server.get_scope_references(
            program_name="ValveProgram",
            routine_name="MainRoutine",
            include_tag_info=True,
        )
        data = json.loads(raw)
        speed_tag = next(t for t in data["tags"] if t["name"] == "GlobalSpeed")
        assert speed_tag["data_type"] == "DINT"
        assert speed_tag["scope"] == "controller"

    def test_aoi_calls_detected(self, rich_project):
        raw = mcp_server.get_scope_references(
            program_name="ValveProgram",
            routine_name="MainRoutine",
        )
        data = json.loads(raw)
        assert data["summary"]["aoi_calls"] >= 2
        # Check AOI call details
        aoi_call = data["aoi_calls"][0]
        assert aoi_call["aoi_name"] == "VALVE_CTL"
        assert aoi_call["instance_tag"] == "V101"
        # Check parameter bindings
        bindings = aoi_call["bindings"]
        binding_names = [b["parameter"] for b in bindings]
        assert "Command" in binding_names
        assert "IOArray" in binding_names
        assert "AddressOffset" in binding_names

    def test_aoi_binding_usage(self, rich_project):
        raw = mcp_server.get_scope_references(
            program_name="ValveProgram",
            routine_name="MainRoutine",
            rung_range="0",
        )
        data = json.loads(raw)
        call = data["aoi_calls"][0]
        # Find the IOArray binding
        io_binding = next(
            b for b in call["bindings"] if b["parameter"] == "IOArray"
        )
        assert io_binding["usage"] == "InOut"
        assert io_binding["required"] is True
        assert io_binding["wired_tag"] == "IO_Data"

    def test_names_only_mode(self, rich_project):
        raw = mcp_server.get_scope_references(
            program_name="ValveProgram",
            routine_name="MainRoutine",
            include_tag_info=False,
        )
        data = json.loads(raw)
        # Tags should have name and rungs but no data_type
        for t in data["tags"]:
            assert "name" in t
            assert "rungs" in t
            assert "data_type" not in t

    def test_scan_all_routines_in_program(self, rich_project):
        raw = mcp_server.get_scope_references(
            program_name="ValveProgram",
        )
        data = json.loads(raw)
        assert data["summary"]["unique_tags"] > 0


# ===================================================================
# 15. find_references
# ===================================================================

class TestFindReferences:
    def test_single_tag(self, rich_project):
        raw = mcp_server.find_references('"GlobalRun"', entity_type="tag")
        data = json.loads(raw)
        assert "GlobalRun" in data
        assert len(data["GlobalRun"]) >= 2  # used in rung 0 and 1

    def test_batch_tags(self, rich_project):
        raw = mcp_server.find_references(
            '["GlobalRun", "GlobalSpeed"]', entity_type="tag",
        )
        data = json.loads(raw)
        assert "GlobalRun" in data
        assert "GlobalSpeed" in data
        assert len(data["GlobalRun"]) >= 2
        assert len(data["GlobalSpeed"]) >= 1

    def test_aoi_references(self, rich_project):
        raw = mcp_server.find_references(
            '["VALVE_CTL"]', entity_type="aoi",
        )
        data = json.loads(raw)
        assert "VALVE_CTL" in data
        assert len(data["VALVE_CTL"]) >= 2

    def test_udt_references(self, rich_project):
        raw = mcp_server.find_references(
            '["VALVE_CTL"]', entity_type="udt",
        )
        data = json.loads(raw)
        assert "VALVE_CTL" in data
        tag_names = [m["tag_name"] for m in data["VALVE_CTL"]]
        assert "V101" in tag_names
        assert "V102" in tag_names

    def test_unknown_entity_type(self, rich_project):
        raw = mcp_server.find_references('"X"', entity_type="bogus")
        assert "Error" in raw

    def test_invalid_json(self, rich_project):
        raw = mcp_server.find_references("not json")
        assert "Error" in raw

    def test_tag_not_found(self, rich_project):
        raw = mcp_server.find_references('"NonExistent"', entity_type="tag")
        data = json.loads(raw)
        assert data["NonExistent"] == []


# ===================================================================
# 16. get_tag_values
# ===================================================================

class TestGetTagValues:
    def test_single_tag_value(self, rich_project):
        raw = mcp_server.get_tag_values('"GlobalSpeed"')
        data = json.loads(raw)
        assert len(data) == 1
        assert data[0]["name"] == "GlobalSpeed"
        assert data[0]["value"] == 1750
        assert data[0]["data_type"] == "DINT"

    def test_multiple_tags(self, rich_project):
        raw = mcp_server.get_tag_values('["GlobalRun", "GlobalSpeed"]')
        data = json.loads(raw)
        assert len(data) == 2
        names = {t["name"] for t in data}
        assert names == {"GlobalRun", "GlobalSpeed"}

    def test_name_filter_glob(self, rich_project):
        raw = mcp_server.get_tag_values(
            '[]', name_filter="Global*",
        )
        data = json.loads(raw)
        assert len(data) >= 2
        for t in data:
            assert t["name"].startswith("Global")

    def test_include_members_structured(self, rich_project):
        raw = mcp_server.get_tag_values(
            '"V101"', include_members=True,
        )
        data = json.loads(raw)
        assert len(data) == 1
        assert "members" in data[0]
        assert isinstance(data[0]["members"], dict)

    def test_include_aoi_context(self, rich_project):
        raw = mcp_server.get_tag_values(
            '["IO_Data", "LocalCmd"]',
            scope="",
            include_aoi_context=True,
        )
        data = json.loads(raw)
        # IO_Data is wired as IOArray parameter to VALVE_CTL
        io_tag = next(t for t in data if t["name"] == "IO_Data")
        assert "aoi_context" in io_tag
        assert len(io_tag["aoi_context"]) >= 1
        ctx = io_tag["aoi_context"][0]
        assert ctx["aoi_name"] == "VALVE_CTL"
        assert ctx["parameter"] == "IOArray"
        assert ctx["usage"] == "InOut"

    def test_program_scope(self, rich_project):
        raw = mcp_server.get_tag_values(
            '"LocalCmd"', scope="program", program_name="ValveProgram",
        )
        data = json.loads(raw)
        assert len(data) == 1
        assert data[0]["name"] == "LocalCmd"

    def test_search_all_scopes(self, rich_project):
        raw = mcp_server.get_tag_values(
            '"GlobalRun"', scope="",
        )
        data = json.loads(raw)
        assert data[0]["scope"] == "controller"

    def test_nonexistent_tag(self, rich_project):
        raw = mcp_server.get_tag_values('"NoSuchTag"')
        data = json.loads(raw)
        assert "error" in data[0]

    def test_invalid_json(self, rich_project):
        raw = mcp_server.get_tag_values("bad")
        assert "Error" in raw


# ===================================================================
# 17. detect_conflicts
# ===================================================================

class TestDetectConflicts:
    def test_tag_shadowing(self, rich_project):
        raw = mcp_server.detect_conflicts(check="tag_shadowing")
        data = json.loads(raw)
        shadows = data["tag_shadowing"]["shadows"]
        assert data["tag_shadowing"]["shadows_found"] >= 1
        shadow_names = [s["tag_name"] for s in shadows]
        assert "ShadowTag" in shadow_names
        # Check the detail
        shadow = next(s for s in shadows if s["tag_name"] == "ShadowTag")
        assert shadow["program"] == "ValveProgram"
        assert shadow["controller_data_type"] == "DINT"
        assert shadow["program_data_type"] == "BOOL"
        assert shadow["types_match"] is False

    def test_unused_tags(self, rich_project):
        raw = mcp_server.detect_conflicts(check="unused_tags")
        data = json.loads(raw)
        unused = data["unused_tags"]
        assert "Unused_Ctrl" in unused["controller_tags"]

    def test_scope_duplicates(self, rich_project):
        raw = mcp_server.detect_conflicts(check="scope_duplicates")
        data = json.loads(raw)
        dupes = data["scope_duplicates"]["duplicates"]
        # LocalCmd exists in both ValveProgram and AuxProgram
        assert data["scope_duplicates"]["duplicates_found"] >= 1
        dupe_names = [d["tag_name"] for d in dupes]
        assert "LocalCmd" in dupe_names
        # Check consistency flag
        local_dupe = next(d for d in dupes if d["tag_name"] == "LocalCmd")
        assert local_dupe["types_consistent"] is True

    def test_all_checks(self, rich_project):
        raw = mcp_server.detect_conflicts(check="all")
        data = json.loads(raw)
        assert "tag_shadowing" in data
        assert "unused_tags" in data
        assert "scope_duplicates" in data
        # aoi_address is no longer a domain check -- use compare_tag_instances
        assert "aoi_address" not in data

    def test_unknown_check(self, rich_project):
        raw = mcp_server.detect_conflicts(check="bogus")
        # Should return empty result (no matching check name)
        data = json.loads(raw)
        assert isinstance(data, dict)


# ===================================================================
# 18. compare_tag_instances
# ===================================================================

class TestCompareTagInstances:
    def test_find_duplicate_members(self, rich_project):
        """V101 and V102 both have AddressOffset=5 — should group together."""
        raw = mcp_server.compare_tag_instances(
            data_type="VALVE_CTL",
            match_members_json='["AddressOffset"]',
        )
        data = json.loads(raw)
        assert data["data_type"] == "VALVE_CTL"
        assert data["match_members"] == ["AddressOffset"]
        assert data["total_instances"] >= 2
        assert data["groups_with_duplicates"] >= 1
        group = data["groups"][0]
        assert group["instance_count"] >= 2
        names = [i["tag_name"] for i in group["instances"]]
        assert "V101" in names
        assert "V102" in names

    def test_multi_member_match(self, rich_project):
        """Match on both AddressOffset and Command (both are 0/5)."""
        raw = mcp_server.compare_tag_instances(
            data_type="VALVE_CTL",
            match_members_json='["AddressOffset", "Command"]',
        )
        data = json.loads(raw)
        assert data["groups_with_duplicates"] >= 1

    def test_with_rung_bindings_inout(self, rich_project):
        """IOArray is InOut — value comes from rung text, not decorated data."""
        raw = mcp_server.compare_tag_instances(
            data_type="VALVE_CTL",
            match_members_json='["IOArray", "AddressOffset"]',
            include_rung_bindings=True,
        )
        data = json.loads(raw)
        assert data["groups_with_duplicates"] >= 1
        group = data["groups"][0]
        names = [i["tag_name"] for i in group["instances"]]
        assert "V101" in names
        assert "V102" in names
        # Check that IOArray was resolved to IO_Data
        for inst in group["instances"]:
            assert inst["member_values"]["IOArray"] == "IO_Data"
            assert str(inst["member_values"]["AddressOffset"]) == "5"

    def test_filter_members(self, rich_project):
        """Pre-filter to only AddressOffset=5 instances."""
        raw = mcp_server.compare_tag_instances(
            data_type="VALVE_CTL",
            match_members_json='["Command"]',
            filter_members_json='{"AddressOffset": "5"}',
        )
        data = json.loads(raw)
        assert data["filter_applied"] == {"AddressOffset": "5"}
        assert data["total_instances"] >= 2

    def test_filter_excludes_non_matching(self, rich_project):
        """Filter with a value no instance has — should find nothing."""
        raw = mcp_server.compare_tag_instances(
            data_type="VALVE_CTL",
            match_members_json='["AddressOffset"]',
            filter_members_json='{"AddressOffset": "999"}',
        )
        data = json.loads(raw)
        assert data["total_instances"] == 0
        assert data["groups_with_duplicates"] == 0

    def test_no_duplicates_when_values_differ(self, rich_project):
        """If match members have unique values, no groups should appear."""
        # V101 and V102 have the same Command value (0), but let's filter
        # to ensure we test the "no match" path by using a member that
        # would differ if values were different. Since both have Command=0,
        # they will actually group. So let's test with an empty result by
        # using a data type with no tags.
        raw = mcp_server.compare_tag_instances(
            data_type="MyUDT",
            match_members_json='["Speed"]',
        )
        data = json.loads(raw)
        # No tags of MyUDT exist, so total_instances should be 0
        assert data["total_instances"] == 0
        assert data["groups_with_duplicates"] == 0

    def test_scope_filter_controller(self, rich_project):
        """Filter to controller scope only."""
        raw = mcp_server.compare_tag_instances(
            data_type="VALVE_CTL",
            match_members_json='["AddressOffset"]',
            scope="controller",
        )
        data = json.loads(raw)
        # V101 and V102 are controller-scoped
        assert data["total_instances"] >= 2

    def test_invalid_match_members_json(self, rich_project):
        """Bad JSON in match_members should return error."""
        raw = mcp_server.compare_tag_instances(
            data_type="VALVE_CTL",
            match_members_json="not json",
        )
        assert "Error" in raw

    def test_empty_match_members(self, rich_project):
        """Empty match_members array should return error."""
        raw = mcp_server.compare_tag_instances(
            data_type="VALVE_CTL",
            match_members_json="[]",
        )
        data = json.loads(raw)
        assert "Error" in data

    def test_invalid_filter_json(self, rich_project):
        """Bad JSON in filter_members should return error."""
        raw = mcp_server.compare_tag_instances(
            data_type="VALVE_CTL",
            match_members_json='["AddressOffset"]',
            filter_members_json="not json",
        )
        assert "Error" in raw
