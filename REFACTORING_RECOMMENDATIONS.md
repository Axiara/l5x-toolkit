# MCP Tool Call Refactoring Recommendations

## Executive Summary

The current MCP server exposes **59 individual tools** across 9 sections. Many follow
one-entity-one-verb patterns that force AI clients to make multiple sequential
round-trips for tasks that naturally group together. This document identifies the
redundancy patterns, proposes consolidated endpoints, and provides concrete
parameter signatures that would reduce overall tool count from **59 → ~25** while
increasing flexibility.

---

## 1. Identified Anti-Patterns

### 1.1 Fragmented List/Query Tools (8 tools → 2)

**Current state — 8 separate listing tools:**

| Tool | What it returns |
|------|----------------|
| `list_programs()` | program names |
| `list_routines(program_name)` | routines in a program |
| `list_controller_tags()` | controller-scope tags |
| `list_program_tags(program_name)` | program-scope tags |
| `list_modules()` | I/O modules |
| `list_aois()` | AOI definitions |
| `list_udts()` | UDT definitions |
| `list_tasks()` | task schedule |

**Problem:** An AI exploring a project calls 4-6 of these sequentially just to
understand the project layout. Each round-trip costs latency and token budget.
`list_controller_tags` and `list_program_tags` are the same operation split
across two tools purely by scope.

**Recommendation — collapse into `query_project`:**

```python
@mcp.tool()
def query_project(
    entity: str,               # "programs" | "routines" | "tags" | "modules"
                               # | "aois" | "udts" | "tasks" | "all"
    scope: str = "",           # "controller" | "program" | "" (both)
    program_name: str = "",    # filter to specific program
    name_filter: str = "",     # glob/regex pattern filter on names
) -> str:
```

- `entity="all"` returns the full project inventory in one call (programs,
  tags, modules, AOIs, UDTs, tasks) — the single most common first action.
- `entity="tags", scope="controller"` replaces `list_controller_tags`.
- `entity="tags", scope="program", program_name="MainProgram"` replaces
  `list_program_tags`.
- `name_filter` lets the AI narrow results without downloading everything.

A second unified tool handles individual entity detail:

```python
@mcp.tool()
def get_entity_info(
    entity: str,       # "tag" | "aoi" | "udt" | "module" | "rung"
    name: str,         # entity name (or rung number as string)
    scope: str = "controller",
    program_name: str = "",
    routine_name: str = "",
    include: str = "",  # comma-separated: "parameters", "members",
                        # "references", "value", "alarm_conditions"
) -> str:
```

This replaces **7 separate tools**:
- `get_tag_info` → `get_entity_info(entity="tag", name="MyTag")`
- `get_tag_member_value` → `get_entity_info(entity="tag", name="T1", include="value")`
  with member path encoded in name (`T1.PRE`)
- `find_tag` → `get_entity_info(entity="tag", name="MyTag", scope="")`
  (empty scope = search all)
- `get_aoi_info` → `get_entity_info(entity="aoi", name="MyAOI")`
- `get_aoi_parameters` → `get_entity_info(entity="aoi", name="MyAOI", include="parameters")`
- `get_udt_info` → `get_entity_info(entity="udt", name="MyUDT")`
- `get_udt_members` → `get_entity_info(entity="udt", name="MyUDT", include="members")`

**Tool call reduction:** When an AI needs AOI info + parameters, current cost
is 2 calls; proposed cost is 1 call with `include="parameters"`.

---

### 1.2 Tag CRUD Splintering (12 tools → 3)

**Current state — 12 tag-focused tools:**

| Tool | Operation |
|------|-----------|
| `create_tag` | create single tag |
| `delete_tag` | delete single tag |
| `rename_tag` | rename single tag |
| `set_tag_value` | set scalar value |
| `set_tag_member_value` | set member value |
| `set_tag_description` | set description only |
| `copy_tag` | deep copy |
| `move_tag` | move between scopes |
| `batch_create_tags` | create multiple |
| `create_alias_tag` | create alias |
| `find_tag` | search all scopes |
| `get_tag_member_value` | read a member |

**Problem:** Setting a tag's value AND description requires 2 calls.
Creating 5 tags individually requires 5 calls (or learning about the separate
`batch_create_tags` tool). copy and move are near-identical flows.

**Recommendation — 3 consolidated tools:**

#### a) `manage_tags` — batch CRUD

```python
@mcp.tool()
def manage_tags(
    operations_json: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Execute one or more tag operations atomically.

    operations_json: JSON array of operation objects. Each object has:
      - "action": "create" | "delete" | "rename" | "copy" | "move" | "create_alias"
      - Plus action-specific fields:
        create:  {name, data_type, dimensions?, description?, radix?, tag_class?}
        delete:  {name}
        rename:  {name, new_name, update_references?}
        copy:    {name, new_name, to_scope?, to_program_name?}
        move:    {name, to_scope, to_program?}
        create_alias: {name, alias_for, description?}

    Example: [
        {"action": "create", "name": "Motor1_Run", "data_type": "BOOL"},
        {"action": "create", "name": "Motor1_Flt", "data_type": "BOOL"},
        {"action": "rename", "name": "OldTag", "new_name": "NewTag"}
    ]
    """
```

This replaces: `create_tag`, `delete_tag`, `rename_tag`, `copy_tag`,
`move_tag`, `batch_create_tags`, `create_alias_tag` (7 tools → 1).

#### b) `update_tags` — batch value/description/member updates

```python
@mcp.tool()
def update_tags(
    updates_json: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Set values, member values, and descriptions on one or more tags.

    updates_json: JSON array of update objects. Each object has:
      - "name": tag name
      - "value"?: new scalar value (auto-typed from data_type)
      - "description"?: new description text
      - "members"?: dict of {member_path: value} for structured/array tags

    Example: [
        {"name": "Timer1", "members": {"PRE": "5000"}, "description": "Main cycle timer"},
        {"name": "MotorSpeed", "value": "1750"}
    ]
    """
```

This replaces: `set_tag_value`, `set_tag_member_value`, `set_tag_description`
(3 tools → 1). Setting a timer's PRE and description in one call instead of two.

#### c) Keep `find_tag` and `get_tag_member_value` folded into `get_entity_info`

Already covered in Section 1.1. Net elimination: 2 more tools.

---

### 1.3 Rung Operation Fragmentation (6 tools → 2)

**Current state — 6 rung-specific tools:**

| Tool | Operation |
|------|-----------|
| `add_rung` | insert one rung |
| `delete_rung` | delete one rung |
| `modify_rung_text` | change instruction text |
| `set_rung_comment` | change comment only |
| `duplicate_rung_with_substitution` | clone + tag replace |
| `get_all_rungs` | read all rungs |

**Problem:** Building a 10-rung routine requires 10 sequential `add_rung` calls.
Modifying a rung's text and comment is 2 calls. Deleting 5 obsolete rungs is
5 calls.

**Recommendation:**

#### a) `manage_rungs` — batch rung operations

```python
@mcp.tool()
def manage_rungs(
    program_name: str,
    routine_name: str,
    operations_json: str,
) -> str:
    """Execute one or more rung operations on a routine.

    operations_json: JSON array of operation objects. Each has:
      - "action": "add" | "delete" | "modify" | "duplicate"
      - Plus action-specific fields:
        add:       {text, comment?, position?}        (-1 or omit to append)
        delete:    {rung_number}
        modify:    {rung_number, text?, comment?}      (set either or both)
        duplicate: {rung_number, substitutions, comment?}

    Example: [
        {"action": "add", "text": "XIC(Start)OTE(Run);", "comment": "Start logic"},
        {"action": "add", "text": "TON(Delay,1000,0);"},
        {"action": "modify", "rung_number": 0, "comment": "Updated comment"}
    ]
    """
```

This replaces: `add_rung`, `delete_rung`, `modify_rung_text`,
`set_rung_comment`, `duplicate_rung_with_substitution` (5 tools → 1).

#### b) Keep `get_all_rungs` as-is (already returns bulk data efficiently)

---

### 1.4 Alarm Tool Proliferation (10 tools → 3)

**Current state — 10 alarm-specific tools:**

| Tool | Purpose |
|------|---------|
| `create_alarm_digital_tag` | single ALARM_DIGITAL |
| `batch_create_alarm_digital_tags` | multiple ALARM_DIGITAL |
| `get_alarm_digital_info` | inspect alarm tag |
| `configure_alarm_digital_tag` | update alarm tag |
| `list_alarms` | list all alarm tags |
| `list_alarm_definitions` | list datatype alarm defs |
| `get_tag_alarm_conditions` | inspect conditions on tag |
| `configure_tag_alarm_condition` | update condition on tag |
| `create_alarm_definition` | create datatype alarm def |
| `remove_alarm_definition` | remove datatype alarm def |

**Problem:** This is the most granular section. An AI configuring alarms for a
new zone may need 4-5 tool calls (create alarm tag, configure it, add
condition, check it, list to verify). The create/configure pair for
`ALARM_DIGITAL` is a common 2-call sequence.

**Recommendation:**

#### a) `manage_alarms` — unified alarm tag operations

```python
@mcp.tool()
def manage_alarms(
    operations_json: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Create, configure, and inspect alarm tags.

    operations_json: JSON array of operation objects. Each has:
      - "action": "create_digital" | "configure_digital" | "get_info"
                  | "get_conditions" | "configure_condition"
      - Plus action-specific fields:
        create_digital:     {name, message, severity?, description?, ack_required?, latched?}
        configure_digital:  {name, severity?, message?, ack_required?, latched?}
        get_info:           {name}
        get_conditions:     {name}
        configure_condition:{tag_name, condition_name, severity?, on_delay?,
                             off_delay?, used?, ack_required?, message?}

    Example: [
        {"action": "create_digital", "name": "Alarm_Conv1", "message": "Conveyor 1 Fault", "severity": 750},
        {"action": "create_digital", "name": "Alarm_Conv2", "message": "Conveyor 2 Fault", "severity": 750}
    ]
    """
```

This replaces: `create_alarm_digital_tag`, `batch_create_alarm_digital_tags`,
`get_alarm_digital_info`, `configure_alarm_digital_tag`,
`get_tag_alarm_conditions`, `configure_tag_alarm_condition` (6 tools → 1).

#### b) `manage_alarm_definitions` — datatype-level alarm defs

```python
@mcp.tool()
def manage_alarm_definitions(
    action: str,             # "list" | "create" | "remove" | "get"
    data_type_name: str = "",
    members_json: str = "",
) -> str:
```

This replaces: `list_alarm_definitions`, `create_alarm_definition`,
`remove_alarm_definition` (3 tools → 1).

#### c) Keep `list_alarms` as-is (it's already a broad query tool)

---

### 1.5 Import Redundancy (3 + 2 → 1)

**Current state — 5 import tools:**

| Tool | What it imports |
|------|----------------|
| `import_aoi` | AOI from file |
| `import_udt` | UDT from file |
| `import_module` | Module from template |
| `analyze_import` | Dry-run conflict check |
| `import_component` | General import with conflict resolution |

**Problem:** `import_aoi`, `import_udt`, and `import_module` are legacy
single-purpose tools that overlap with the more capable `import_component`
which already auto-detects component type. The AI has to know which tool to
use for which file type.

**Recommendation — keep 2 tools:**

```python
# Keep import_component as-is (already handles all types)
# Keep analyze_import as-is (useful dry-run)
# Remove: import_aoi, import_udt, import_module
```

The only addition needed is an optional `module_config` parameter on
`import_component` for the module-specific fields (parent_module, address,
slot):

```python
@mcp.tool()
def import_component(
    file_path: str,
    conflict_resolution: str = "report",
    target_program: str = "",
    target_routine: str = "",
    rung_position: int = -1,
    # New: module-specific overrides
    module_name: str = "",
    parent_module: str = "Local",
    address: str = "",
    slot: str = "",
) -> str:
```

**Tool call reduction:** 5 → 2 (3 tools removed).

---

### 1.6 Export Shell Creation (3 tools → 1)

**Current state — 3 create-export-shell tools:**

| Tool | Creates |
|------|---------|
| `create_rung_export` | empty Rung export |
| `create_routine_export` | empty Routine export |
| `create_program_export` | empty Program export |

**Problem:** Nearly identical code paths. The AI must select the right variant.

**Recommendation — 1 parameterized tool:**

```python
@mcp.tool()
def create_export_shell(
    export_type: str,          # "rung" | "routine" | "program"
    program_name: str = "ExportedProgram",
    routine_name: str = "MainRoutine",
    routine_type: str = "RLL",
) -> str:
```

---

### 1.7 Export Extraction (6 tools → 1)

**Current state — 6 export tools:**

| Tool | Exports |
|------|---------|
| `export_rung` | specific rungs |
| `export_routine` | full routine |
| `export_program` | full program |
| `export_tag` | single tag |
| `export_udt` | UDT definition |
| `export_aoi` | AOI definition |

**Problem:** Six nearly identical "find entity, write to file" flows. The AI
needs to know which tool maps to which entity type.

**Recommendation — 1 parameterized tool:**

```python
@mcp.tool()
def export_component(
    component_type: str,       # "rung" | "routine" | "program" | "tag" | "udt" | "aoi"
    name: str = "",            # entity name (or comma-separated rung numbers)
    program_name: str = "",
    routine_name: str = "",
    scope: str = "controller",
    file_path: str = "",
    include_tags: bool = True,
) -> str:
```

---

### 1.8 Overlapping AOI/UDT Info Tools (4 tools → 0, folded into get_entity_info)

Already addressed in Section 1.1. The pairs `get_aoi_info`/`get_aoi_parameters`
and `get_udt_info`/`get_udt_members` become single calls through the `include`
parameter on `get_entity_info`.

---

### 1.9 Rung Utility Scattering (3 tools — keep but merge 2)

**Current state:**

| Tool | Purpose |
|------|---------|
| `validate_rung_syntax` | check syntax |
| `substitute_tags_in_rung` | tag name replacement |
| `extract_tag_references_from_rung` | list referenced tags |

These are stateless text-processing utilities. They could be merged into a
single `analyze_rung_text` tool:

```python
@mcp.tool()
def analyze_rung_text(
    rung_text: str,
    action: str = "validate",    # "validate" | "extract_tags" | "substitute"
    substitutions_json: str = "",
) -> str:
```

**Tool call reduction:** 3 → 1.

---

## 2. Consolidated Tool Inventory

| # | Proposed Tool | Replaces | Tools Eliminated |
|---|---------------|----------|-----------------|
| 1 | `load_project` | (keep as-is) | 0 |
| 2 | `save_project` | (keep as-is) | 0 |
| 3 | `format_project` | (keep as-is) | 0 |
| 4 | `strip_l5k_data` | (keep as-is) | 0 |
| 5 | `get_project_summary` | (keep as-is) | 0 |
| 6 | `query_project` | 8 list_* tools | 7 |
| 7 | `get_entity_info` | get_tag_info, find_tag, get_tag_member_value, get_aoi_info, get_aoi_parameters, get_udt_info, get_udt_members | 7 |
| 8 | `manage_tags` | create/delete/rename/copy/move_tag, batch_create_tags, create_alias_tag | 7 |
| 9 | `update_tags` | set_tag_value, set_tag_member_value, set_tag_description | 3 |
| 10 | `find_tag_references` | (keep as-is) | 0 |
| 11 | `create_program` | (keep as-is) | 0 |
| 12 | `delete_program` | (keep as-is) | 0 |
| 13 | `create_routine` | (keep as-is) | 0 |
| 14 | `manage_rungs` | add/delete/modify_rung, set_rung_comment, duplicate_rung_with_substitution | 5 |
| 15 | `get_all_rungs` | (keep as-is) | 0 |
| 16 | `schedule_program` | (keep as-is) | 0 |
| 17 | `unschedule_program` | (keep as-is) | 0 |
| 18 | `import_component` | import_aoi, import_udt, import_module + existing import_component | 3 |
| 19 | `analyze_import` | (keep as-is) | 0 |
| 20 | `manage_alarms` | 6 alarm CRUD tools | 6 |
| 21 | `manage_alarm_definitions` | list/create/remove_alarm_definition | 3 |
| 22 | `list_alarms` | (keep as-is) | 0 |
| 23 | `validate_project` | (keep as-is) | 0 |
| 24 | `analyze_rung_text` | validate_rung_syntax, substitute_tags_in_rung, extract_tag_references_from_rung | 3 |
| 25 | `create_export_shell` | create_rung/routine/program_export | 3 |
| 26 | `export_component` | export_rung/routine/program/tag/udt/aoi | 6 |

**Total: 59 → 26 tools (33 eliminated, 56% reduction)**

---

## 3. Priority Ranking

Ranked by impact (frequency of multi-call patterns × tokens saved per consolidation):

| Priority | Refactoring | Tool Reduction | Impact |
|----------|-------------|---------------|--------|
| **P0** | `query_project` (unified listing) | 8 → 1 | Every session starts with 4-6 list calls |
| **P0** | `manage_tags` (batch CRUD) | 7 → 1 | Tag creation is the highest-volume operation |
| **P1** | `manage_rungs` (batch rung ops) | 5 → 1 | Building routines = many sequential add_rung calls |
| **P1** | `get_entity_info` (unified detail) | 7 → 1 | Eliminates AOI info + params being 2 calls |
| **P1** | `update_tags` (batch updates) | 3 → 1 | value + description is a common 2-call pair |
| **P2** | `export_component` (unified export) | 6 → 1 | Lower frequency but simple consolidation |
| **P2** | `manage_alarms` (unified alarms) | 6 → 1 | Alarm setup always involves multiple tools |
| **P2** | `create_export_shell` (unified shell) | 3 → 1 | Low frequency, easy win |
| **P3** | `import_component` expansion | 3 → 0 | Already mostly consolidated |
| **P3** | `manage_alarm_definitions` | 3 → 1 | Low frequency |
| **P3** | `analyze_rung_text` | 3 → 1 | Utility tools, low frequency |

---

## 4. Migration Strategy

### Phase 1: Additive (non-breaking)
1. Add new consolidated tools alongside existing ones.
2. Mark old tools with deprecation notices in docstrings.
3. Update `instructions` string to guide AI clients toward new tools.

### Phase 2: Deprecation period
1. Old tools log warnings to stderr when called.
2. AI instructions explicitly say "prefer `manage_tags` over `create_tag`."

### Phase 3: Removal
1. Remove deprecated tools after confirming no active clients use them.
2. Final tool count: ~26.

### Backward Compatibility Notes
- All JSON-array parameter patterns (`operations_json`, `updates_json`) accept
  single-element arrays, so single-operation calls remain just as easy.
- Scope/program_name parameter conventions stay consistent.
- Return format (JSON strings) stays consistent.

---

## 5. Implementation Details for Top-Priority Refactors

### 5.1 `query_project` Implementation Sketch

```python
@mcp.tool()
def query_project(
    entity: str = "all",
    scope: str = "",
    program_name: str = "",
    name_filter: str = "",
) -> str:
    """Query project contents — programs, tags, modules, AOIs, UDTs, tasks.

    Replaces list_programs, list_routines, list_controller_tags,
    list_program_tags, list_modules, list_aois, list_udts, list_tasks.

    Args:
        entity: What to list. One of: 'all', 'programs', 'routines',
                'tags', 'modules', 'aois', 'udts', 'tasks'.
                'all' returns a combined inventory.
        scope: For tags: 'controller', 'program', or '' (both).
               For routines: ignored (use program_name).
        program_name: Filter to a specific program (for tags and routines).
        name_filter: Optional glob pattern to filter names (e.g. 'Motor*').
    """
    prj = _require_project()
    result = {}

    entities = [entity] if entity != "all" else [
        "programs", "tags", "modules", "aois", "udts", "tasks"
    ]

    for ent in entities:
        if ent == "programs":
            result["programs"] = prj.list_programs()
        elif ent == "routines":
            result["routines"] = prj.list_routines(program_name)
        elif ent == "tags":
            tags = []
            if scope in ("controller", ""):
                tags.extend(prj.list_controller_tags())
            if scope in ("program", ""):
                if program_name:
                    tags.extend(prj.list_program_tags(program_name))
                elif scope == "":
                    for p in prj.list_programs():
                        tags.extend(prj.list_program_tags(p))
            result["tags"] = tags
        elif ent == "modules":
            result["modules"] = prj.list_modules()
        elif ent == "aois":
            result["aois"] = prj.list_aois()
        elif ent == "udts":
            result["udts"] = prj.list_udts()
        elif ent == "tasks":
            result["tasks"] = prj.list_tasks()

    # Apply name_filter if provided
    if name_filter:
        import fnmatch
        for key in result:
            if isinstance(result[key], list):
                result[key] = [
                    item for item in result[key]
                    if fnmatch.fnmatch(
                        item.get("name", item) if isinstance(item, dict) else item,
                        name_filter
                    )
                ]

    return json.dumps(result, indent=2)
```

### 5.2 `manage_tags` Implementation Sketch

```python
@mcp.tool()
def manage_tags(
    operations_json: str,
    scope: str = "controller",
    program_name: str = "",
) -> str:
    """Execute one or more tag operations in sequence.

    Args:
        operations_json: JSON array of operation objects.
        scope: Default scope for all operations (can be overridden per-op).
        program_name: Default program for all operations (can be overridden per-op).
    """
    prj = _require_project()
    ops = json.loads(operations_json)
    results = []

    for i, op in enumerate(ops):
        action = op["action"]
        op_scope = op.get("scope", scope)
        op_prog = op.get("program_name", program_name) or None

        try:
            if action == "create":
                _tags.create_tag(
                    prj, op["name"], op["data_type"],
                    scope=op_scope, program_name=op_prog,
                    dimensions=op.get("dimensions"),
                    description=op.get("description"),
                    radix=op.get("radix"),
                    tag_class=op.get("tag_class"),
                )
                results.append({"index": i, "status": "ok", "action": "create", "name": op["name"]})
            elif action == "delete":
                _tags.delete_tag(prj, op["name"], scope=op_scope, program_name=op_prog)
                results.append({"index": i, "status": "ok", "action": "delete", "name": op["name"]})
            elif action == "rename":
                _tags.rename_tag(
                    prj, op["name"], op["new_name"],
                    scope=op_scope, program_name=op_prog,
                    update_references=op.get("update_references", True),
                )
                results.append({"index": i, "status": "ok", "action": "rename",
                                "old": op["name"], "new": op["new_name"]})
            elif action == "copy":
                _tags.copy_tag(
                    prj, op["name"], op["new_name"],
                    source_scope=op_scope,
                    source_program=op_prog,
                    dest_scope=op.get("to_scope", op_scope),
                    dest_program=op.get("to_program_name", op_prog),
                )
                results.append({"index": i, "status": "ok", "action": "copy", "name": op["name"]})
            elif action == "move":
                _tags.move_tag(
                    prj, op["name"],
                    from_scope=op_scope,
                    from_program=op_prog,
                    to_scope=op["to_scope"],
                    to_program=op.get("to_program"),
                )
                results.append({"index": i, "status": "ok", "action": "move", "name": op["name"]})
            elif action == "create_alias":
                _tags.create_alias_tag(
                    prj, op["name"], op["alias_for"],
                    scope=op_scope, program_name=op_prog,
                    description=op.get("description"),
                )
                results.append({"index": i, "status": "ok", "action": "create_alias", "name": op["name"]})
            else:
                results.append({"index": i, "status": "error", "message": f"Unknown action: {action}"})
        except Exception as e:
            results.append({"index": i, "status": "error", "action": action, "message": str(e)})

    succeeded = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "error")
    return json.dumps({"succeeded": succeeded, "failed": failed, "details": results}, indent=2)
```

### 5.3 `manage_rungs` Implementation Sketch

```python
@mcp.tool()
def manage_rungs(
    program_name: str,
    routine_name: str,
    operations_json: str,
) -> str:
    """Execute one or more rung operations on a routine in sequence.

    Operations are processed in order. Rung numbers in later operations
    reflect the state AFTER prior operations in the same batch.

    Args:
        program_name: Program containing the routine.
        routine_name: Name of the RLL routine.
        operations_json: JSON array of operation objects.
    """
    prj = _require_project()
    ops = json.loads(operations_json)
    results = []

    for i, op in enumerate(ops):
        action = op["action"]
        try:
            if action == "add":
                pos = op.get("position")
                pos = pos if pos is not None and pos >= 0 else None
                _programs.add_rung(
                    prj, program_name, routine_name,
                    op["text"],
                    comment=op.get("comment"),
                    position=pos,
                )
                results.append({"index": i, "status": "ok", "action": "add"})
            elif action == "delete":
                _programs.delete_rung(prj, program_name, routine_name, op["rung_number"])
                results.append({"index": i, "status": "ok", "action": "delete"})
            elif action == "modify":
                rn = op["rung_number"]
                if "text" in op:
                    _programs.modify_rung_text(prj, program_name, routine_name, rn, op["text"])
                if "comment" in op:
                    _programs.set_rung_comment(prj, program_name, routine_name, rn, op["comment"])
                results.append({"index": i, "status": "ok", "action": "modify"})
            elif action == "duplicate":
                subs = op.get("substitutions", {})
                _programs.duplicate_rung_with_substitution(
                    prj, program_name, routine_name, op["rung_number"],
                    subs, new_comment=op.get("comment"),
                )
                results.append({"index": i, "status": "ok", "action": "duplicate"})
            else:
                results.append({"index": i, "status": "error", "message": f"Unknown action: {action}"})
        except Exception as e:
            results.append({"index": i, "status": "error", "action": action, "message": str(e)})

    succeeded = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "error")
    return json.dumps({"succeeded": succeeded, "failed": failed, "details": results}, indent=2)
```

---

## 6. Expected Impact on AI Client Behavior

### Before (typical 10-tag conveyor zone setup):

```
1. list_programs()                        # what's here?
2. list_controller_tags()                 # existing tags?
3. list_udts()                            # available types?
4. create_tag("Conv1_Run", "BOOL")        # tag 1
5. create_tag("Conv1_Speed", "DINT")      # tag 2
6. create_tag("Conv1_Flt", "BOOL")        # tag 3
7. set_tag_description("Conv1_Run", ...)  # describe tag 1
8. set_tag_description("Conv1_Speed", ...)# describe tag 2
9. set_tag_description("Conv1_Flt", ...)  # describe tag 3
10. add_rung("Main", "MainRoutine", ...)  # rung 1
11. add_rung("Main", "MainRoutine", ...)  # rung 2
12. add_rung("Main", "MainRoutine", ...)  # rung 3
13. create_alarm_digital_tag("Alarm_Conv1", ...) # alarm
14. save_project()
```
**14 tool calls**

### After (same task):

```
1. query_project(entity="all")              # full inventory
2. manage_tags(operations_json=[            # all 3 tags + descriptions
     {"action": "create", "name": "Conv1_Run", "data_type": "BOOL", "description": "..."},
     {"action": "create", "name": "Conv1_Speed", "data_type": "DINT", "description": "..."},
     {"action": "create", "name": "Conv1_Flt", "data_type": "BOOL", "description": "..."}
   ])
3. manage_rungs("Main", "MainRoutine",      # all 3 rungs
     operations_json=[
       {"action": "add", "text": "...", "comment": "..."},
       {"action": "add", "text": "...", "comment": "..."},
       {"action": "add", "text": "...", "comment": "..."}
     ])
4. manage_alarms(operations_json=[           # alarm tag
     {"action": "create_digital", "name": "Alarm_Conv1", ...}
   ])
5. save_project()
```
**5 tool calls (64% reduction)**

---

## 7. Design Principles Applied

1. **Batch-first**: Every mutation tool accepts an array of operations.
   Single operations are arrays of length 1 — no special-casing.
2. **Scope as a default, not a constraint**: Common parameters like `scope`
   and `program_name` are set at the tool level but overridable per-operation.
3. **Action dispatch over tool proliferation**: One tool with an `action`
   discriminator is cheaper than N tools with identical parameter shapes.
4. **Include-based detail levels**: Instead of N tools returning subsets
   of the same entity's data, one tool with `include` flags.
5. **Keep read and write separate**: Query tools remain separate from
   mutation tools to preserve clarity and safety.
