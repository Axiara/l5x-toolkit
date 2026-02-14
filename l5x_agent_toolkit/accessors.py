"""
Sub-accessor classes that organise L5XProject methods into logical groups.

Each accessor holds a back-reference to the owning ``L5XProject`` and
provides a coherent slice of the project API.  ``L5XProject`` creates these
in ``__init__`` and delegates attribute lookups via ``__getattr__`` so that
*all* old call-sites (``project.list_programs()``, etc.) keep working.

Usage (new style)::

    project = L5XProject("file.L5X")
    project.tags.list_controller()
    project.programs.list_all()
    project.analysis.find_tag_references("MyTag")

Usage (old style — still works, delegates transparently)::

    project.list_controller_tags()   # → project.tags.list_controller()
    project.list_programs()          # → project.programs.list_all()
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional

from lxml import etree

from .models import (
    RungInfo,
    Scope,
    TagInfo,
    TagReference,
)

if TYPE_CHECKING:
    from .project import L5XProject

logger = logging.getLogger(__name__)


# ===================================================================
# Tag Accessor
# ===================================================================

class TagAccessor:
    """Tag querying, element lookup, and value reading."""

    __slots__ = ("_prj",)

    def __init__(self, project: L5XProject) -> None:
        self._prj = project

    # -- listing -------------------------------------------------------

    def list_controller(self) -> list[dict]:
        """Return list of controller-scope tag info dicts."""
        self._prj._ensure_loaded()
        tags_el = self._prj.controller_tags_element
        if tags_el is None:
            return []
        return self._prj._extract_tag_info_list(tags_el)

    # Backward-compatible alias
    list_controller_tags = list_controller

    def list_program(self, program_name: str) -> list[dict]:
        """Return list of program-scope tag info dicts."""
        prog = self._prj.get_program_element(program_name)
        tags_el = prog.find("Tags")
        if tags_el is None:
            return []
        return self._prj._extract_tag_info_list(tags_el)

    # Backward-compatible alias
    list_program_tags = list_program

    # -- element lookup -------------------------------------------------

    def get_controller_tag_element(self, tag_name: str) -> etree._Element:
        """Return controller-scope Tag element by name."""
        self._prj._ensure_loaded()
        tags_el = self._prj.controller_tags_element
        if tags_el is not None:
            for tag in tags_el.findall("Tag"):
                if tag.get("Name") == tag_name:
                    return tag
        raise KeyError(f"Controller tag '{tag_name}' not found.")

    def get_program_tag_element(
        self, program_name: str, tag_name: str
    ) -> etree._Element:
        """Return program-scope Tag element by name."""
        prog = self._prj.get_program_element(program_name)
        tags_el = prog.find("Tags")
        if tags_el is not None:
            for tag in tags_el.findall("Tag"):
                if tag.get("Name") == tag_name:
                    return tag
        raise KeyError(
            f"Tag '{tag_name}' not found in program '{program_name}'."
        )

    def get_tag_element(
        self,
        tag_name: str,
        scope: str = Scope.CONTROLLER,
        program_name: Optional[str] = None,
    ) -> etree._Element:
        """Generic tag element lookup."""
        if scope == Scope.CONTROLLER:
            return self.get_controller_tag_element(tag_name)
        elif scope == Scope.PROGRAM:
            if not program_name:
                raise ValueError(
                    "program_name is required when scope is 'program'."
                )
            return self.get_program_tag_element(program_name, tag_name)
        else:
            raise ValueError(
                f"Invalid scope '{scope}'. Use 'controller' or 'program'."
            )

    # -- value access ---------------------------------------------------

    def get_value(
        self,
        tag_name: str,
        scope: str = Scope.CONTROLLER,
        program_name: Optional[str] = None,
    ):
        """Read a tag's value from the Decorated data format."""
        tag_el = self.get_tag_element(tag_name, scope, program_name)
        data_el = self._prj._find_decorated_data(tag_el)
        if data_el is None:
            raise ValueError(
                f"Tag '{tag_name}' does not have Decorated format data."
            )
        return self._prj._parse_decorated_data(data_el)

    # Backward-compatible alias
    get_tag_value = get_value

    def get_member_value(
        self,
        tag_name: str,
        member_path: str,
        scope: str = Scope.CONTROLLER,
        program_name: Optional[str] = None,
    ):
        """Read a specific member value from a structured tag."""
        tag_el = self.get_tag_element(tag_name, scope, program_name)
        data_el = self._prj._find_decorated_data(tag_el)
        if data_el is None:
            raise ValueError(
                f"Tag '{tag_name}' does not have Decorated format data."
            )

        parts = member_path.split(".")
        current = data_el

        for part in parts:
            array_match = re.match(r"^(\w+)\[(\d+)\]$", part)
            if array_match:
                member_name = array_match.group(1)
                index = int(array_match.group(2))
                current = self._prj._find_member_element(current, member_name)
                if current is None:
                    raise KeyError(
                        f"Member '{member_name}' not found in path "
                        f"'{member_path}' for tag '{tag_name}'."
                    )
                current = self._prj._find_array_element(current, index)
                if current is None:
                    raise KeyError(
                        f"Array index [{index}] not found in path "
                        f"'{member_path}' for tag '{tag_name}'."
                    )
            else:
                found = self._prj._find_member_element(current, part)
                if found is None:
                    raise KeyError(
                        f"Member '{part}' not found in path '{member_path}' "
                        f"for tag '{tag_name}'."
                    )
                current = found

        return self._prj._parse_decorated_data(current)

    # Backward-compatible alias
    get_tag_member_value = get_member_value

    # -- convenience ----------------------------------------------------

    def find_by_data_type(
        self,
        data_type: str,
        scope: str = "",
        program_name: str = "",
    ) -> list[dict]:
        """Find all tags of a given data type across scopes.

        Returns a list of dicts with ``tag_name``, ``scope``,
        and optionally ``program``.
        """
        self._prj._ensure_loaded()
        results: list[dict] = []
        dt_lower = data_type.lower()

        if scope in ("", Scope.CONTROLLER):
            for t in self.list_controller():
                if t.get("data_type", "").lower() == dt_lower:
                    results.append({
                        "tag_name": t["name"],
                        "scope": Scope.CONTROLLER,
                    })

        if scope in ("", Scope.PROGRAM):
            programs = (
                [program_name] if program_name
                else self._prj.programs.list_all()
            )
            for p in programs:
                for t in self.list_program(p):
                    if t.get("data_type", "").lower() == dt_lower:
                        results.append({
                            "tag_name": t["name"],
                            "scope": Scope.PROGRAM,
                            "program": p,
                        })

        return results

    # Backward-compatible alias
    find_tags_by_data_type = find_by_data_type


# ===================================================================
# Program Accessor
# ===================================================================

class ProgramAccessor:
    """Program, routine, and rung queries."""

    __slots__ = ("_prj",)

    def __init__(self, project: L5XProject) -> None:
        self._prj = project

    # -- listing -------------------------------------------------------

    def list_all(self) -> list[str]:
        """Return list of program names."""
        self._prj._ensure_loaded()
        return [p.get("Name", "") for p in self._prj._all_program_elements()]

    # Backward-compatible alias
    list_programs = list_all

    def list_routines(self, program_name: str) -> list[dict]:
        """Return list of routine info dicts for a program."""
        prog = self._prj.get_program_element(program_name)
        routines_container = prog.find("Routines")
        if routines_container is None:
            return []
        result = []
        for routine in routines_container.findall("Routine"):
            result.append({
                "name": routine.get("Name", ""),
                "type": self._prj._infer_routine_type(routine),
            })
        return result

    # -- element lookup -------------------------------------------------

    def get_program_element(self, program_name: str) -> etree._Element:
        """Return Program XML element by name."""
        self._prj._ensure_loaded()
        programs_el = self._prj.programs_element
        if programs_el is not None:
            for prog in programs_el.findall("Program"):
                if prog.get("Name") == program_name:
                    return prog
        raise KeyError(f"Program '{program_name}' not found.")

    def is_safety_program(self, program_name: str) -> bool:
        """Return True if the named program has Class='Safety'."""
        prog = self.get_program_element(program_name)
        return prog.get("Class", "") == "Safety"

    def get_routine_element(
        self, program_name: str, routine_name: str
    ) -> etree._Element:
        """Return Routine XML element."""
        prog = self.get_program_element(program_name)
        routines_container = prog.find("Routines")
        if routines_container is not None:
            for routine in routines_container.findall("Routine"):
                if routine.get("Name") == routine_name:
                    return routine
        raise KeyError(
            f"Routine '{routine_name}' not found in program '{program_name}'."
        )

    # -- rung access ----------------------------------------------------

    def get_rung_count(self, program_name: str, routine_name: str) -> int:
        """Return the number of rungs in a routine."""
        rungs = self._get_rung_elements(program_name, routine_name)
        return len(rungs)

    def get_rung_text(
        self, program_name: str, routine_name: str, rung_number: int
    ) -> str:
        """Return the instruction text of a specific rung."""
        rung = self._get_rung_by_number(
            program_name, routine_name, rung_number
        )
        text_el = rung.find("Text")
        if text_el is not None and text_el.text:
            return text_el.text.strip()
        return ""

    def get_rung_comment(
        self, program_name: str, routine_name: str, rung_number: int
    ) -> Optional[str]:
        """Return the comment text of a rung, or None."""
        rung = self._get_rung_by_number(
            program_name, routine_name, rung_number
        )
        comment_el = rung.find("Comment")
        if comment_el is not None and comment_el.text:
            return comment_el.text.strip()
        return None

    def get_all_rungs(
        self, program_name: str, routine_name: str
    ) -> list[dict]:
        """Return all rungs in a routine as dicts."""
        rungs = self._get_rung_elements(program_name, routine_name)
        result = []
        for rung in rungs:
            text_el = rung.find("Text")
            text = ""
            if text_el is not None and text_el.text:
                text = text_el.text.strip()

            comment_el = rung.find("Comment")
            comment = None
            if comment_el is not None and comment_el.text:
                comment = comment_el.text.strip()

            result.append({
                "number": int(rung.get("Number", "0")),
                "type": rung.get("Type", "N"),
                "text": text,
                "comment": comment,
            })
        return result

    # -- internal helpers -----------------------------------------------

    def _get_rung_elements(
        self, program_name: str, routine_name: str
    ) -> list[etree._Element]:
        """Return the list of Rung elements for a routine."""
        routine = self.get_routine_element(program_name, routine_name)
        routine_type = self._prj._infer_routine_type(routine)
        if routine_type != "RLL":
            raise ValueError(
                f"Routine '{routine_name}' in program '{program_name}' "
                f"is type '{routine_type}', not RLL. "
                f"Rung access is only available for RLL routines."
            )
        rll_content = routine.find("RLLContent")
        if rll_content is None:
            return []
        return rll_content.findall("Rung")

    def _get_rung_by_number(
        self, program_name: str, routine_name: str, rung_number: int
    ) -> etree._Element:
        """Return a specific Rung element by its Number attribute."""
        rungs = self._get_rung_elements(program_name, routine_name)
        for rung in rungs:
            if int(rung.get("Number", "-1")) == rung_number:
                return rung
        raise KeyError(
            f"Rung {rung_number} not found in routine '{routine_name}' "
            f"of program '{program_name}'. "
            f"Available rungs: 0-{len(rungs) - 1}."
        )


# ===================================================================
# DataType Accessor
# ===================================================================

class DataTypeAccessor:
    """Data type, AOI definition, module, and task queries."""

    __slots__ = ("_prj",)

    def __init__(self, project: L5XProject) -> None:
        self._prj = project

    # -- type lookup ----------------------------------------------------

    def get_data_type_element(self, type_name: str) -> etree._Element:
        """Return DataType element by name."""
        self._prj._ensure_loaded()
        dt_el = self._prj.data_types_element
        if dt_el is not None:
            for dt in dt_el.findall("DataType"):
                if dt.get("Name") == type_name:
                    return dt
        raise KeyError(f"DataType '{type_name}' not found.")

    def get_data_type_definition(self, type_name: str) -> etree._Element:
        """Return a DataType or AOI element by name (searches both)."""
        self._prj._ensure_loaded()
        dt_el = self._prj.data_types_element
        if dt_el is not None:
            for dt in dt_el.findall("DataType"):
                if dt.get("Name") == type_name:
                    return dt
        aoi_el = self._prj.aoi_definitions_element
        if aoi_el is not None:
            for aoi in aoi_el.findall("AddOnInstructionDefinition"):
                if aoi.get("Name") == type_name:
                    return aoi
        raise KeyError(f"DataType or AOI '{type_name}' not found.")

    def get_aoi_element(self, aoi_name: str) -> etree._Element:
        """Return AddOnInstructionDefinition element by name."""
        self._prj._ensure_loaded()
        aoi_el = self._prj.aoi_definitions_element
        if aoi_el is not None:
            for aoi in aoi_el.findall("AddOnInstructionDefinition"):
                if aoi.get("Name") == aoi_name:
                    return aoi
        raise KeyError(f"AOI '{aoi_name}' not found.")

    def get_module_element(self, module_name: str) -> etree._Element:
        """Return Module element by name."""
        self._prj._ensure_loaded()
        modules_el = self._prj.modules_element
        if modules_el is not None:
            for mod in modules_el.findall("Module"):
                if mod.get("Name") == module_name:
                    return mod
        raise KeyError(f"Module '{module_name}' not found.")

    # -- listing -------------------------------------------------------

    def list_modules(self) -> list[dict]:
        """Return list of module info dicts."""
        self._prj._ensure_loaded()
        modules_el = self._prj.modules_element
        if modules_el is None:
            return []
        result = []
        for mod in modules_el.findall("Module"):
            result.append({
                "name": mod.get("Name", ""),
                "catalog_number": mod.get("CatalogNumber", ""),
                "parent": mod.get("ParentModule", ""),
            })
        return result

    def list_aois(self) -> list[dict]:
        """Return list of AOI info dicts."""
        self._prj._ensure_loaded()
        aoi_el = self._prj.aoi_definitions_element
        if aoi_el is None:
            return []
        result = []
        for aoi in aoi_el.findall("AddOnInstructionDefinition"):
            desc = self._prj._get_description_text(aoi)
            result.append({
                "name": aoi.get("Name", ""),
                "revision": aoi.get("Revision", ""),
                "description": desc,
            })
        return result

    def list_udts(self) -> list[dict]:
        """Return list of UDT info dicts."""
        self._prj._ensure_loaded()
        dt_el = self._prj.data_types_element
        if dt_el is None:
            return []
        result = []
        for dt in dt_el.findall("DataType"):
            desc = self._prj._get_description_text(dt)
            members_el = dt.find("Members")
            member_count = (
                len(members_el.findall("Member"))
                if members_el is not None else 0
            )
            result.append({
                "name": dt.get("Name", ""),
                "description": desc,
                "member_count": member_count,
            })
        return result

    def list_tasks(self) -> list[dict]:
        """Return list of task info dicts."""
        self._prj._ensure_loaded()
        tasks_el = self._prj.tasks_element
        if tasks_el is None:
            return []
        result = []
        for task in tasks_el.findall("Task"):
            scheduled = task.find("ScheduledPrograms")
            prog_names = []
            if scheduled is not None:
                for sp in scheduled.findall("ScheduledProgram"):
                    prog_names.append(sp.get("Name", ""))
            result.append({
                "name": task.get("Name", ""),
                "type": task.get("Type", ""),
                "priority": task.get("Priority", ""),
                "rate": task.get("Rate", ""),
                "watchdog": task.get("Watchdog", ""),
                "programs": prog_names,
            })
        return result


# ===================================================================
# Analysis Engine
# ===================================================================

class AnalysisEngine:
    """Cross-reference searches, unused-tag detection, and code scanning."""

    __slots__ = ("_prj",)

    def __init__(self, project: L5XProject) -> None:
        self._prj = project

    def find_tag_references(self, tag_name: str) -> list[dict]:
        """Find all references to a tag across all routines."""
        self._prj._ensure_loaded()
        results: list[dict] = []

        escaped = re.escape(tag_name)
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){escaped}(?=[.\[\],\)\s;]|$)",
            re.IGNORECASE,
        )

        for prog in self._prj._all_program_elements():
            prog_name = prog.get("Name", "")
            routines_container = prog.find("Routines")
            if routines_container is None:
                continue

            for routine in routines_container.findall("Routine"):
                routine_name = routine.get("Name", "")
                routine_type = self._prj._infer_routine_type(routine)

                if routine_type == "RLL":
                    rll_content = routine.find("RLLContent")
                    if rll_content is None:
                        continue
                    for rung in rll_content.findall("Rung"):
                        text_el = rung.find("Text")
                        if text_el is None or not text_el.text:
                            continue
                        rung_text = text_el.text.strip()
                        if pattern.search(rung_text):
                            results.append({
                                "program": prog_name,
                                "routine": routine_name,
                                "rung": int(rung.get("Number", "0")),
                                "text": rung_text,
                            })

                elif routine_type == "ST":
                    st_content = routine.find("STContent")
                    if st_content is None:
                        continue
                    for line_el in st_content.findall("Line"):
                        if line_el.text and pattern.search(
                            line_el.text.strip()
                        ):
                            results.append({
                                "program": prog_name,
                                "routine": routine_name,
                                "line": int(line_el.get("Number", "0")),
                                "text": line_el.text.strip(),
                            })

        return results

    def find_unused_tags(
        self,
        scope: str = Scope.CONTROLLER,
        program_name: Optional[str] = None,
    ) -> list[str]:
        """Find tags that are not referenced in any code."""
        self._prj._ensure_loaded()

        if scope == Scope.CONTROLLER:
            tag_infos = self._prj.tags.list_controller()
        elif scope == Scope.PROGRAM:
            if not program_name:
                raise ValueError(
                    "program_name is required when scope is 'program'."
                )
            tag_infos = self._prj.tags.list_program(program_name)
        else:
            raise ValueError(f"Invalid scope '{scope}'.")

        tag_names = [t["name"] for t in tag_infos]
        all_code_text = self._prj._collect_all_code_text()

        unused: list[str] = []
        for name in tag_names:
            escaped = re.escape(name)
            pattern = re.compile(
                rf"(?<![A-Za-z0-9_]){escaped}(?=[.\[\],\)\s;]|$)",
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
