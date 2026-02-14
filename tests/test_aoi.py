"""Tests for the aoi module."""

import pytest
from lxml import etree
from l5x_agent_toolkit import aoi


AOI_XML = (
    "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
    "<RSLogix5000Content SchemaRevision=\"1.0\" SoftwareRevision=\"33.00\""
    " TargetName=\"TestCtrl\" TargetType=\"Controller\" ContainsContext=\"true\""
    " Owner=\"test\" ExportDate=\"x\" ExportOptions=\"NoRawData L5KData DecoratedData\">"
    "<Controller Use=\"Target\" Name=\"TestCtrl\" ProcessorType=\"1769-L33ER\" MajorRev=\"33\" MinorRev=\"11\">"
    "<DataTypes/><Modules/>"
    "<AddOnInstructionDefinitions>"
    "<AddOnInstructionDefinition Name=\"ConveyorAOI\" Revision=\"1.0\""
    " CreatedDate=\"2024-01-01T00:00:00.000Z\" EditedDate=\"2024-06-01T00:00:00.000Z\">"
    "<Description><![CDATA[Conveyor control AOI]]></Description>"
    "<Parameters>"
    "<Parameter Name=\"EnableIn\" DataType=\"BOOL\" Usage=\"Input\" Required=\"false\" Visible=\"false\"/>"
    "<Parameter Name=\"EnableOut\" DataType=\"BOOL\" Usage=\"Output\" Required=\"false\" Visible=\"false\"/>"
    "<Parameter Name=\"Speed\" DataType=\"DINT\" Usage=\"Input\" Required=\"true\" Visible=\"true\">"
    "<Description><![CDATA[Target speed]]></Description></Parameter>"
    "<Parameter Name=\"Direction\" DataType=\"BOOL\" Usage=\"Input\" Required=\"false\" Visible=\"true\"/>"
    "<Parameter Name=\"Running\" DataType=\"BOOL\" Usage=\"Output\" Required=\"false\" Visible=\"true\"/>"
    "<Parameter Name=\"MotorRef\" DataType=\"DINT\" Usage=\"InOut\"/>"
    "</Parameters>"
    "<LocalTags><LocalTag Name=\"InternalState\" DataType=\"DINT\" Radix=\"Decimal\"/></LocalTags>"
    "<Routines><Routine Name=\"Logic\" Type=\"RLL\"><RLLContent>"
    "<Rung Number=\"0\" Type=\"N\"><Text><![CDATA[NOP();]]></Text></Rung>"
    "</RLLContent></Routine></Routines>"
    "</AddOnInstructionDefinition>"
    "</AddOnInstructionDefinitions>"
    "<Tags/><Programs/><Tasks/>"
    "</Controller></RSLogix5000Content>"
)


class FakeAOIProject:
    def __init__(self):
        parser = etree.XMLParser(strip_cdata=False)
        self.root = etree.fromstring(AOI_XML.encode(), parser)
        self._controller = self.root.find("Controller")
    @property
    def controller(self): return self._controller


class TestGetAoiInfo:
    def test_basic_info(self):
        proj = FakeAOIProject()
        info = aoi.get_aoi_info(proj, "ConveyorAOI")
        assert info["name"] == "ConveyorAOI"
        assert info["revision"] == "1.0"
        assert info["description"] == "Conveyor control AOI"
    def test_parameters(self):
        proj = FakeAOIProject()
        info = aoi.get_aoi_info(proj, "ConveyorAOI")
        names = [p["name"] for p in info["parameters"]]
        assert "EnableIn" in names
        assert "Speed" in names
        assert "MotorRef" in names
    def test_local_tags(self):
        proj = FakeAOIProject()
        info = aoi.get_aoi_info(proj, "ConveyorAOI")
        local_names = [lt["name"] for lt in info["local_tags"]]
        assert "InternalState" in local_names
    def test_routines(self):
        proj = FakeAOIProject()
        info = aoi.get_aoi_info(proj, "ConveyorAOI")
        routine_names = [r["name"] for r in info["routines"]]
        assert "Logic" in routine_names
    def test_nonexistent_raises(self):
        proj = FakeAOIProject()
        with pytest.raises(ValueError): aoi.get_aoi_info(proj, "FakeAOI")


class TestGetAoiParameters:
    def test_count(self):
        proj = FakeAOIProject()
        params = aoi.get_aoi_parameters(proj, "ConveyorAOI")
        assert len(params) == 6
    def test_speed_details(self):
        proj = FakeAOIProject()
        params = aoi.get_aoi_parameters(proj, "ConveyorAOI")
        speed = [p for p in params if p["name"] == "Speed"][0]
        assert speed["data_type"] == "DINT"
        assert speed["usage"] == "Input"
        assert speed["required"] is True
        assert speed["description"] == "Target speed"
    def test_inout(self):
        proj = FakeAOIProject()
        params = aoi.get_aoi_parameters(proj, "ConveyorAOI")
        motor = [p for p in params if p["name"] == "MotorRef"][0]
        assert motor["usage"] == "InOut"


class TestGenerateAoiCallText:
    def test_basic_call(self):
        proj = FakeAOIProject()
        text = aoi.generate_aoi_call_text(proj, "ConveyorAOI", "Conv01",
            param_map={"Speed": "TargetSpeed", "MotorRef": "Motor01"})
        assert text.startswith("ConveyorAOI(")
        assert text.endswith(");")
        assert "Conv01" in text
        assert "TargetSpeed" in text
    def test_optional_placeholder(self):
        proj = FakeAOIProject()
        text = aoi.generate_aoi_call_text(proj, "ConveyorAOI", "Conv01",
            param_map={"Speed": "100", "MotorRef": "Motor01"})
        assert "?" in text
    def test_missing_inout_raises(self):
        proj = FakeAOIProject()
        with pytest.raises(ValueError, match="missing required"):
            aoi.generate_aoi_call_text(proj, "ConveyorAOI", "Conv01",
                param_map={"Speed": "100"})
    def test_missing_required_raises(self):
        proj = FakeAOIProject()
        with pytest.raises(ValueError, match="missing required"):
            aoi.generate_aoi_call_text(proj, "ConveyorAOI", "Conv01",
                param_map={"MotorRef": "Motor01"})
    def test_nonexistent_raises(self):
        proj = FakeAOIProject()
        with pytest.raises(ValueError): aoi.generate_aoi_call_text(proj, "FakeAOI", "Inst01")


class TestListAoiDependencies:
    def test_no_deps(self):
        proj = FakeAOIProject()
        deps = aoi.list_aoi_dependencies(proj, "ConveyorAOI")
        assert isinstance(deps["data_types"], list)
        assert isinstance(deps["aois"], list)
    def test_nonexistent_raises(self):
        proj = FakeAOIProject()
        with pytest.raises(ValueError): aoi.list_aoi_dependencies(proj, "FakeAOI")
