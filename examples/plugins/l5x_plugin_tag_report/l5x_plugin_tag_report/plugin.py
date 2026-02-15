"""
Tag Report Plugin for L5X Agent Toolkit.

Provides three genuinely useful MCP tools that every automation engineer
will benefit from:

1. **export_tags_csv** — Export all tags (or a filtered subset) to a CSV
   file.  Useful for documentation, review meetings, or importing into
   spreadsheets and CMMS systems.

2. **audit_tag_naming** — Check tag names against configurable naming
   convention rules (prefix patterns, length limits, reserved words).
   Helps enforce plant-wide naming standards.

3. **project_statistics** — Generate a detailed breakdown of the project:
   tag-type distribution, program sizes, data-type usage, and more.
   Great for project health checks and handover documentation.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from collections import Counter
from typing import Optional

from l5x_agent_toolkit.plugin import L5XPlugin, PluginContext


class TagReportPlugin(L5XPlugin):
    """Adds tag reporting, CSV export, and naming audit tools."""

    name = "Tag Report"
    version = "1.0.0"
    description = (
        "Export tags to CSV, audit naming conventions, and generate "
        "project statistics."
    )

    def register_tools(self, ctx: PluginContext) -> None:

        # ---------------------------------------------------------------
        # Tool 1: export_tags_csv
        # ---------------------------------------------------------------
        @ctx.mcp.tool()
        def export_tags_csv(
            file_path: str,
            scope: str = "all",
            program_name: str = "",
            name_filter: str = "",
            include_values: bool = True,
        ) -> str:
            """Export tags to a CSV file for documentation or spreadsheet use.

            Creates a CSV with columns: Name, DataType, Scope, Program,
            Description, Value, TagType, Radix, Dimensions, ExternalAccess.

            Args:
                file_path: Destination path for the CSV file.
                scope: "controller", "program", or "all" (default "all").
                program_name: Required when scope is "program".
                name_filter: Optional glob pattern (e.g. "Motor*") to filter
                    tag names.
                include_values: Whether to include the Value column
                    (default True).

            Returns:
                Summary of how many tags were exported and the file path.
            """
            import fnmatch

            prj = ctx.get_project()
            tags = []

            # Collect controller tags
            if scope in ("controller", "all"):
                for t in prj.tags.list_controller():
                    t["scope"] = "controller"
                    t["program"] = ""
                    tags.append(t)

            # Collect program tags
            if scope in ("program", "all"):
                programs_to_scan = []
                if program_name:
                    programs_to_scan = [program_name]
                elif scope == "all":
                    programs_to_scan = prj.programs.list_all()

                for prog in programs_to_scan:
                    for t in prj.tags.list_program(prog):
                        t["scope"] = "program"
                        t["program"] = prog
                        tags.append(t)

            # Apply name filter
            if name_filter:
                tags = [
                    t for t in tags
                    if fnmatch.fnmatch(t.get("name", ""), name_filter)
                ]

            if not tags:
                return "No tags matched the filter criteria."

            # Write CSV
            columns = [
                "Name", "DataType", "Scope", "Program", "Description",
                "TagType", "Radix", "Dimensions", "ExternalAccess",
            ]
            if include_values:
                columns.insert(5, "Value")

            dest = os.path.abspath(file_path)
            with open(dest, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                for t in tags:
                    row = [
                        t.get("name", ""),
                        t.get("data_type", ""),
                        t.get("scope", ""),
                        t.get("program", ""),
                        t.get("description", ""),
                        t.get("tag_type", "Base"),
                        t.get("radix", ""),
                        t.get("dimensions", ""),
                        t.get("external_access", ""),
                    ]
                    if include_values:
                        row.insert(5, t.get("value", ""))
                    writer.writerow(row)

            return (
                f"Exported {len(tags)} tag(s) to: {dest}\n"
                f"Columns: {', '.join(columns)}"
            )

        # ---------------------------------------------------------------
        # Tool 2: audit_tag_naming
        # ---------------------------------------------------------------
        @ctx.mcp.tool()
        def audit_tag_naming(
            rules_json: str = "",
            scope: str = "all",
            program_name: str = "",
        ) -> str:
            """Audit tag names against naming convention rules.

            Checks every tag name against a set of configurable rules and
            reports violations.  Useful for enforcing plant-wide standards.

            Args:
                rules_json: Optional JSON object with rule overrides.
                    Supported keys:
                    - "max_length" (int): Maximum tag name length
                      (default 40).
                    - "require_prefix" (bool): If true, names must start
                      with a known prefix (default false).
                    - "prefixes" (list[str]): Allowed prefixes when
                      require_prefix is true (e.g. ["AI_", "DI_", "DO_",
                      "AO_", "MTR_", "VLV_"]).
                    - "no_lowercase" (bool): If true, flag names
                      containing lowercase letters (default false).
                    - "no_double_underscore" (bool): Flag names with
                      consecutive underscores (default true).
                    - "reserved_words" (list[str]): Names that should
                      not be used (case-insensitive).
                scope: "controller", "program", or "all".
                program_name: Required when scope is "program".

            Returns:
                JSON report with violations grouped by rule.
            """
            prj = ctx.get_project()

            # Parse rules
            defaults = {
                "max_length": 40,
                "require_prefix": False,
                "prefixes": [],
                "no_lowercase": False,
                "no_double_underscore": True,
                "reserved_words": [],
            }
            if rules_json:
                try:
                    overrides = json.loads(rules_json)
                    defaults.update(overrides)
                except json.JSONDecodeError as e:
                    return f"Error: Invalid rules_json — {e}"
            rules = defaults

            # Collect tags
            tags = []
            if scope in ("controller", "all"):
                for t in prj.tags.list_controller():
                    tags.append({
                        "name": t["name"],
                        "scope": "controller",
                        "program": "",
                    })
            if scope in ("program", "all"):
                programs_to_scan = (
                    [program_name] if program_name
                    else prj.programs.list_all()
                )
                for prog in programs_to_scan:
                    for t in prj.tags.list_program(prog):
                        tags.append({
                            "name": t["name"],
                            "scope": "program",
                            "program": prog,
                        })

            # Run checks
            violations: dict[str, list] = {
                "too_long": [],
                "missing_prefix": [],
                "has_lowercase": [],
                "double_underscore": [],
                "reserved_word": [],
            }

            reserved_lower = {w.lower() for w in rules["reserved_words"]}
            prefixes = [p.upper() for p in rules["prefixes"]]

            for t in tags:
                name = t["name"]
                loc = (
                    f"{t['program']}.{name}" if t["program"]
                    else name
                )

                if len(name) > rules["max_length"]:
                    violations["too_long"].append({
                        "tag": loc,
                        "length": len(name),
                        "max": rules["max_length"],
                    })

                if rules["require_prefix"] and prefixes:
                    if not any(name.upper().startswith(p) for p in prefixes):
                        violations["missing_prefix"].append({
                            "tag": loc,
                            "expected_prefixes": rules["prefixes"],
                        })

                if rules["no_lowercase"] and re.search(r"[a-z]", name):
                    violations["has_lowercase"].append({"tag": loc})

                if rules["no_double_underscore"] and "__" in name:
                    violations["double_underscore"].append({"tag": loc})

                if name.lower() in reserved_lower:
                    violations["reserved_word"].append({"tag": loc})

            # Summary
            total_violations = sum(len(v) for v in violations.values())
            result = {
                "tags_checked": len(tags),
                "total_violations": total_violations,
                "rules_applied": {
                    k: v for k, v in rules.items()
                    if k != "reserved_words" or v
                },
                "violations": {
                    k: v for k, v in violations.items() if v
                },
            }
            if not result["violations"]:
                result["message"] = "All tags pass naming convention checks."

            return json.dumps(result, indent=2)

        # ---------------------------------------------------------------
        # Tool 3: project_statistics
        # ---------------------------------------------------------------
        @ctx.mcp.tool()
        def project_statistics() -> str:
            """Generate detailed project statistics and health metrics.

            Provides a comprehensive breakdown useful for project reviews,
            handover documentation, and health checks:

            - Tag counts by scope and data type
            - Program sizes (rung counts per routine)
            - Data type usage distribution
            - AOI and UDT inventory
            - Rung complexity estimates (instruction counts)

            Returns:
                JSON object with categorised statistics.
            """
            prj = ctx.get_project()
            stats: dict = {}

            # -- Tag distribution --
            ctrl_tags = prj.tags.list_controller()
            type_counter: Counter = Counter()
            for t in ctrl_tags:
                type_counter[t.get("data_type", "UNKNOWN")] += 1

            prog_tag_counts: dict[str, int] = {}
            prog_tag_types: Counter = Counter()
            for prog in prj.programs.list_all():
                ptags = prj.tags.list_program(prog)
                prog_tag_counts[prog] = len(ptags)
                for t in ptags:
                    prog_tag_types[t.get("data_type", "UNKNOWN")] += 1

            stats["tags"] = {
                "controller_count": len(ctrl_tags),
                "program_tag_counts": prog_tag_counts,
                "total_program_tags": sum(prog_tag_counts.values()),
                "total_all_scopes": (
                    len(ctrl_tags) + sum(prog_tag_counts.values())
                ),
                "controller_type_distribution": dict(
                    type_counter.most_common()
                ),
                "program_type_distribution": dict(
                    prog_tag_types.most_common()
                ),
            }

            # -- Program/routine sizes --
            program_details = []
            total_rungs = 0
            for prog in prj.programs.list_all():
                routines = prj.programs.list_routines(prog)
                prog_info = {"name": prog, "routines": []}
                for r in routines:
                    rung_count = 0
                    if r.get("type") == "RLL":
                        try:
                            rung_list = prj.programs.list_rungs(
                                prog, r["name"],
                            )
                            rung_count = len(rung_list)
                        except Exception:
                            rung_count = -1  # could not read
                    prog_info["routines"].append({
                        "name": r["name"],
                        "type": r.get("type", "?"),
                        "rung_count": rung_count,
                    })
                    total_rungs += max(rung_count, 0)
                program_details.append(prog_info)

            stats["programs"] = {
                "count": len(program_details),
                "total_rungs": total_rungs,
                "details": program_details,
            }

            # -- Data types --
            udts = prj.types.list_udts()
            aois = prj.types.list_aois()
            stats["data_types"] = {
                "udt_count": len(udts),
                "udts": [u.get("name", "") for u in udts],
                "aoi_count": len(aois),
                "aois": [a.get("name", "") for a in aois],
            }

            # -- Modules --
            modules = prj.types.list_modules()
            stats["modules"] = {
                "count": len(modules),
                "names": [m.get("name", "") for m in modules],
            }

            # -- Tasks --
            tasks = prj.types.list_tasks()
            stats["tasks"] = {
                "count": len(tasks),
                "details": tasks,
            }

            return json.dumps(stats, indent=2)
