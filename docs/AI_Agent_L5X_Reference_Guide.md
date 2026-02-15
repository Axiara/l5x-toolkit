# AI Agent L5X Toolkit Reference Guide

## 1. Introduction

This document is your complete reference for using the L5X Agent Toolkit MCP server. The toolkit provides 31 validated, batch-capable tools for reading and modifying Rockwell Automation Studio 5000 L5X project files. You will use these tools to manipulate PLC (Programmable Logic Controller) programs for industrial automation systems.

### The Cardinal Rule

**Never generate raw L5X XML.** Every modification to an L5X project must be made through the MCP tools provided by this server. The tools handle all XML structural requirements, data format synchronization, element ordering, and CDATA encoding. Generating raw XML will produce files that crash Studio 5000 or silently corrupt data.

### Required Workflow

Every session follows this pattern:

1. **Always call `load_project` first** before any other tool.
2. Perform your query and modification operations.
3. **Always call `validate_project` before `save_project`** to catch structural errors.
4. Call `save_project` to write the modified file.

If validation returns errors, fix them before saving. Warnings are advisory but errors are fatal.

---

## 2. L5X File Structure Overview

An L5X file is an XML document that represents a Rockwell Automation PLC project. Understanding its structure helps you use the tools correctly.

### Root Element

```xml
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="37.01"
    TargetType="Controller" TargetName="ProjectName" ...>
```

The `TargetType` indicates what the file contains: `Controller` (full project), `AddOnInstructionDefinition` (AOI export), `DataType` (UDT export), `Module` (hardware template), or `Rung` (code snippet).

### Controller Element and Child Ordering

The `<Controller>` element is the main container. Its children MUST appear in this exact order. Studio 5000 will reject files with incorrect ordering:

1. `RedundancyInfo`
2. `Security`
3. `SafetyInfo`
4. `DataTypes` -- UDT definitions
5. `Modules` -- I/O hardware
6. `AddOnInstructionDefinitions` -- AOI definitions
7. `Tags` -- Controller-scope tags
8. `Programs` -- Programs containing routines and program-scope tags
9. `Tasks` -- Task scheduling configuration
10. `CST`
11. `WallClockTime`
12. `Trends`
13. `DataLogs`
14. `TimeSynchronize`
15. `EthernetPorts`
16. `OpcUaInfo`

The toolkit enforces this ordering automatically when inserting new elements.

### Programs, Routines, and Rungs

```
Programs / Program / Routines / Routine / RLLContent / Rung / Text
```

Each Program contains Tags (program-scope) and Routines. Each RLL Routine contains an `RLLContent` element with `Rung` children. Each Rung has a `Text` child containing the instruction text and optionally a `Comment` child.

### Tag Data Elements

Every tag carries its value in two synchronized formats:

- **`<Data Format="L5K">`** -- Compact text: `0` for scalars, `[0,0,0]` for structures, `[0,0,0,0,0]` for arrays.
- **`<Data Format="Decorated">`** -- Verbose XML with named members, types, and radixes.

These two formats MUST stay in sync. The toolkit handles this automatically when you use `update_tags`. Never attempt to update one format without the other.

If L5K data is out of sync with Decorated data, use `strip_l5k_data` to remove the L5K data. Studio 5000 will regenerate it from the Decorated format on import.

---

## 3. Data Types

### Base (Atomic) Types

| Type | Size (bits) | Default Radix | Default Value | Description |
|------|-------------|---------------|---------------|-------------|
| `BOOL` | 1 | Decimal | `0` | Boolean (0 or 1) |
| `SINT` | 8 | Decimal | `0` | Signed 8-bit integer (-128 to 127) |
| `USINT` | 8 | Decimal | `0` | Unsigned 8-bit integer (0 to 255) |
| `INT` | 16 | Decimal | `0` | Signed 16-bit integer |
| `UINT` | 16 | Decimal | `0` | Unsigned 16-bit integer |
| `DINT` | 32 | Decimal | `0` | Signed 32-bit integer (most common) |
| `UDINT` | 32 | Decimal | `0` | Unsigned 32-bit integer |
| `LINT` | 64 | Decimal | `0` | Signed 64-bit integer |
| `REAL` | 32 | Float | `0.0` | 32-bit floating point |
| `LREAL` | 64 | Float | `0.0` | 64-bit floating point |
| `STRING` | variable | ASCII | `''` | Character string (LEN + DATA structure) |

### Built-In Structure Types

**TIMER** -- Used with TON, TOF, RTO instructions:

| Member | Type | Description |
|--------|------|-------------|
| `PRE` | DINT | Preset value (milliseconds) |
| `ACC` | DINT | Accumulated value |
| `EN` | BOOL | Enable bit |
| `TT` | BOOL | Timer timing bit |
| `DN` | BOOL | Done bit |

L5K default: `[0,0,0]` (PRE, ACC, and a packed DINT for EN/TT/DN).

**COUNTER** -- Used with CTU, CTD instructions:

| Member | Type | Description |
|--------|------|-------------|
| `PRE` | DINT | Preset count |
| `ACC` | DINT | Accumulated count |
| `CU` | BOOL | Count up enable |
| `CD` | BOOL | Count down enable |
| `DN` | BOOL | Done bit |
| `OV` | BOOL | Overflow |
| `UN` | BOOL | Underflow |

L5K default: `[0,0,0]`.

**CONTROL** -- Used with file/array instructions:

| Member | Type | Description |
|--------|------|-------------|
| `LEN` | DINT | Length |
| `POS` | DINT | Position |
| `EN` | BOOL | Enable |
| `EU` | BOOL | Enable unload |
| `DN` | BOOL | Done |
| `EM` | BOOL | Empty |
| `ER` | BOOL | Error |
| `UL` | BOOL | Unload |
| `IN` | BOOL | Inhibit |
| `FD` | BOOL | Found |

L5K default: `[0,0,0]`.

### STRING Type

STRING is a built-in structure with two members:
- `LEN` (DINT) -- Current string length
- `DATA` (SINT[82]) -- Character data array (82 bytes)

Custom string types (e.g., `STRING_32`) follow the same pattern with different DATA array sizes.

### User-Defined Types (UDTs)

UDTs are custom structures defined in the project's `DataTypes` section. Key points:

- Members can be any type: base types, other UDTs, arrays, or BIT type.
- BOOL members use a special bit-packing pattern: a hidden SINT backing member (named with `ZZZZZZZZZZ` prefix) stores the actual bits, and visible `BIT` members reference it via `Target` and `BitNumber` attributes.
- The toolkit's `get_entity_info` (with `include='members'`) returns only visible members (excludes hidden backing fields).
- UDTs that reference other UDTs create dependency chains. Dependencies must be defined before the types that use them.

### Add-On Instructions (AOIs)

AOIs are reusable logic blocks with defined interfaces. When you create a tag of an AOI type, it becomes an "instance tag" that holds the AOI's internal state. Key concepts:

- **Parameters**: Input, Output, and InOut -- define the AOI's interface.
- **EnableIn/EnableOut**: System parameters handled implicitly; excluded from rung call arguments.
- **Local Tags**: Internal storage not accessible outside the AOI.
- **Instance Tag**: A tag whose DataType is the AOI name; holds all parameter and local tag values.

---

## 4. RLL Rung Syntax

This is the most critical section. Rung instruction text is a domain-specific language (DSL) used by Rockwell to encode Relay Ladder Logic. Every rung you create or modify must conform exactly to this syntax.

### Fundamental Rules

1. Every rung MUST end with a semicolon: `;`
2. An empty rung is simply: `;`
3. Instructions chain directly without spaces between them.
4. Arguments are enclosed in parentheses, separated by commas.
5. Instruction format: `INSTRUCTION(arg1,arg2,...)`

### Series Logic (AND)

Instructions in sequence represent series (AND) logic. Each condition must be true for the next to evaluate:

```
XIC(StartPB)XIC(SafetyOK)OTE(MotorRun);
```

This means: IF StartPB is ON AND SafetyOK is ON THEN energize MotorRun.

### Parallel Logic (OR Branches)

Square brackets define parallel paths. A comma separates each path. A space appears before the comma:

```
[XIC(StartPB) ,XIC(AutoStart) ]OTE(MotorRun);
```

This means: IF StartPB is ON OR AutoStart is ON THEN energize MotorRun.

Branch syntax rules:
- `[` opens a branch group
- ` ,` (space then comma) separates parallel paths
- `]` closes the branch group
- Branches can be nested

### Nested Branches

```
[XIC(Cond_A) ,[XIC(Cond_B) ,XIC(Cond_C) ] ]OTE(Output);
```

This means: IF Cond_A OR (Cond_B OR Cond_C) THEN energize Output.

### Common Instructions

**Bit Instructions:**

| Instruction | Args | Description |
|-------------|------|-------------|
| `XIC(tag)` | 1 | Examine if closed (NO contact) |
| `XIO(tag)` | 1 | Examine if open (NC contact) |
| `OTE(tag)` | 1 | Output energize (coil) |
| `OTL(tag)` | 1 | Output latch (set) |
| `OTU(tag)` | 1 | Output unlatch (reset) |
| `ONS(tag)` | 1 | One-shot |

**Timer/Counter Instructions:**

| Instruction | Args | Description |
|-------------|------|-------------|
| `TON(timer,preset,acc)` | 3 | Timer On Delay |
| `TOF(timer,preset,acc)` | 3 | Timer Off Delay |
| `RTO(timer,preset,acc)` | 3 | Retentive Timer On |
| `CTU(counter,preset,acc)` | 3 | Count Up |
| `CTD(counter,preset,acc)` | 3 | Count Down |
| `RES(timer_or_counter)` | 1 | Reset timer/counter |

**Compare Instructions:**

| Instruction | Args | Description |
|-------------|------|-------------|
| `EQU(srcA,srcB)` | 2 | Equal |
| `NEQ(srcA,srcB)` | 2 | Not equal |
| `GRT(srcA,srcB)` | 2 | Greater than |
| `GEQ(srcA,srcB)` | 2 | Greater than or equal |
| `LES(srcA,srcB)` | 2 | Less than |
| `LEQ(srcA,srcB)` | 2 | Less than or equal |
| `LIM(low,test,high)` | 3 | Limit test |

**Math Instructions:**

| Instruction | Args | Description |
|-------------|------|-------------|
| `ADD(srcA,srcB,dest)` | 3 | Add |
| `SUB(srcA,srcB,dest)` | 3 | Subtract |
| `MUL(srcA,srcB,dest)` | 3 | Multiply |
| `DIV(srcA,srcB,dest)` | 3 | Divide |
| `MOD(srcA,srcB,dest)` | 3 | Modulo |
| `NEG(src,dest)` | 2 | Negate |
| `ABS(src,dest)` | 2 | Absolute value |
| `CPT(dest,expression)` | 2 | Compute |

**Move/Copy Instructions:**

| Instruction | Args | Description |
|-------------|------|-------------|
| `MOV(src,dest)` | 2 | Move |
| `COP(src,dest,length)` | 3 | Copy |
| `FLL(src,dest,length)` | 3 | Fill |
| `CLR(dest)` | 1 | Clear |

**Program Flow Instructions:**

| Instruction | Args | Description |
|-------------|------|-------------|
| `JSR(routine,param1,...)` | 1+ | Jump to subroutine |
| `SBR(param1,...)` | 0+ | Subroutine entry (receives params) |
| `RET(param1,...)` | 0+ | Return from subroutine |
| `JMP(label)` | 1 | Jump to label |
| `LBL(label)` | 1 | Label |
| `NOP()` | 0 | No operation |
| `AFI()` | 0 | Always false instruction |
| `TND()` | 0 | Temporary end |
| `MCR()` | 0 | Master control reset |

**AOI Calls:**

```
AOI_Name(InstanceTag,Param1,Param2,...);
```

AOI calls look like regular instructions. The first argument is always the instance tag. Subsequent arguments correspond to the AOI's visible parameters (excluding EnableIn/EnableOut) in definition order.

### Tag References in Rungs

| Pattern | Example | Description |
|---------|---------|-------------|
| Simple tag | `MyTag` | Direct reference |
| Member access | `MyTimer.DN` | Structure member |
| Array index | `MyArray[0]` | Array element |
| Indirect index | `MyArray[IndexTag]` | Tag-based index |
| Combined | `MyArray[0].Member.Sub` | Nested access |

### Literal Values in Arguments

| Format | Example | Description |
|--------|---------|-------------|
| Decimal integer | `1000`, `0`, `-5` | Standard integers |
| Float | `3.14`, `0.0` | Floating-point values |
| Hex | `16#FF00` | Hexadecimal with `16#` prefix |
| Binary | `2#1010_0011` | Binary with `2#` prefix |
| Octal | `8#77` | Octal with `8#` prefix |

The `?` character is used as a placeholder for optional parameters (typically timer/counter display values).

### Complete Rung Examples

**1. Simple XIC/OTE (start button energizes motor):**
```
XIC(StartPB)OTE(MotorRun);
```

**2. Start/Stop Seal-In Circuit:**
```
[XIC(StartPB) ,XIC(MotorRun) ]XIC(StopPB)OTE(MotorRun);
```

**3. Timer On Delay (1-second delay):**
```
XIC(EnableTimer)TON(DelayTimer,1000,0);
```

**4. Counter with reset:**
```
XIC(CountPulse)CTU(PartCounter,100,0);
```

**5. Compare and branch (if temperature > 150 OR manual override, activate cooling):**
```
[GRT(Temperature,150) ,XIC(ManualCool) ]OTE(CoolingValve);
```

**6. Nested parallel branches:**
```
[XIC(Auto) ,[XIC(Manual) ,XIC(Override) ] ]XIC(SafetyOK)OTE(DriveEnable);
```

**7. AOI call with parameters:**
```
MDR_Transport_AOI(Conv_A0010_Controller,Conv_A0010_Z1,Conv_A0010_Z2,Conv_A0010_Z3);
```

**8. JSR (Jump to Subroutine) with parameters:**
```
JSR(FaultHandler,FaultCode,FaultMessage);
```

**9. Math operation (calculate speed setpoint):**
```
MUL(SpeedPct,MaxSpeed,SpeedSetpoint);
```

**10. Array access in rung:**
```
XIC(ZoneEnable[CurrentZone])MOV(ZoneSpeed[CurrentZone],DriveSpeedRef);
```

**11. Complex multi-branch with timer and compare:**
```
[XIC(Sensor1)XIC(Sensor2) ,[GRT(Position,100) ,XIC(LimitSw) ] ]TON(TransportTimer,5000,0);
```

**12. One-shot with latch/unlatch:**
```
XIC(TriggerInput)ONS(TriggerOneShot)OTL(ProcessActive);
```

**13. Multiple output branches (one input condition driving parallel outputs):**
```
XIC(MasterEnable)[OTE(Output1) ,OTE(Output2) ,OTE(Output3) ];
```

---

## 5. Tag Operations Guide

### Creating Tags

Use `manage_tags` with the `create` action:

```json
[{"action": "create", "name": "MotorSpeed", "data_type": "DINT", "description": "Speed setpoint RPM"}]
```

Available fields for create operations:

| Field | Required | Description |
|-----------|----------|-------------|
| `name` | Yes | Tag name (see naming rules below) |
| `data_type` | Yes | Any base type, built-in structure, UDT, or AOI name |
| `scope` | No | `"controller"` (default) or `"program"` |
| `program_name` | If scope=program | Name of the program |
| `dimensions` | No | Array dimensions: `"10"` or `"3,4"` |
| `description` | No | Description text |
| `radix` | No | Display radix override |

### Tag Naming Rules

- Maximum 40 characters
- Must start with a letter (A-Z, a-z) or underscore (`_`)
- May contain letters, digits (0-9), and underscores
- Regex pattern: `^[A-Za-z_][A-Za-z0-9_]*$`
- Names are case-insensitive for uniqueness checks

### Setting Tag Values

Use `update_tags` to set values and descriptions on one or more tags.

For **scalar tags** (DINT, REAL, BOOL, etc.):
```json
[{"name": "MyCounter", "value": "42"}]
```

For **structured or array tags**, use the `members` field with member paths:
```json
[{"name": "MyTimer", "members": {"PRE": "5000"}}]
```

| Path Pattern | Example | Target |
|--------------|---------|--------|
| Member name | `PRE` | Timer preset |
| Nested member | `Status.Active` | Nested structure member |
| Array index | `[0]` | First array element |
| Array + member | `[2].EN` | Enable bit of 3rd array element |
| Multi-dim index | `[1,2]` | Element at row 1, column 2 |

### Tag Scopes

- **Controller scope**: Visible to all programs. Use `scope="controller"`.
- **Program scope**: Visible only within the program. Use `scope="program"` with `program_name`.

### Batch Tag Creation

Use `manage_tags` with multiple create operations in a single call:
```json
[
  {"action": "create", "name": "Zone1_Timer", "data_type": "TIMER", "description": "Zone 1 transport timer"},
  {"action": "create", "name": "Zone1_Speed", "data_type": "DINT", "description": "Zone 1 speed setpoint"},
  {"action": "create", "name": "Zone1_Enable", "data_type": "BOOL", "description": "Zone 1 enable flag"}
]
```

### Other Tag Actions

`manage_tags` supports these additional actions in the same batch:

| Action | Description | Key Fields |
|--------|-------------|------------|
| `delete` | Remove a tag | `name` |
| `rename` | Rename with reference updates | `name`, `new_name`, `update_references` (default true) |
| `copy` | Copy a tag | `name`, `new_name`, `to_scope`, `to_program_name` |
| `move` | Move between scopes | `name`, `to_scope`, `to_program` |
| `create_alias` | Create an alias tag | `name`, `alias_for`, `description` |

---

## 6. Program and Routine Operations Guide

### Creating Programs

`create_program` automatically creates a `MainRoutine` (RLL type) inside the new program. Every program needs at least one routine.

```
create_program(name="TransportLine1", description="Transport line 1 control")
```

### Creating Additional Routines

```
create_routine(program_name="TransportLine1", routine_name="FaultHandler", routine_type="RLL")
```

Valid routine types: `RLL` (Relay Ladder Logic), `ST` (Structured Text), `FBD` (Function Block Diagram), `SFC` (Sequential Function Chart). The toolkit primarily supports RLL.

### Adding Rungs

Use `manage_rungs` with the `add` action:

```json
[{"action": "add", "text": "XIC(StartPB)OTE(MotorRun);", "comment": "Start pushbutton energizes motor"}]
```

Position: `-1` or omit to append to end; `0` inserts at beginning; any non-negative integer inserts at that index.

### Modifying Existing Rungs

Use `manage_rungs` with the `modify` action:

```json
[{"action": "modify", "rung_number": 3, "text": "XIC(NewStartPB)OTE(MotorRun);"}]
```

You can modify just the text, just the comment, or both in the same operation.

### The Duplicate Pattern

The `duplicate` action in `manage_rungs` is THE key pattern for bulk operations. It duplicates a rung and replaces tag names according to a substitution map:

```json
[{
  "action": "duplicate",
  "rung_number": 0,
  "substitutions": {"Conv_A0010": "Conv_A0020", "Z1": "Z3", "Z2": "Z4"},
  "comment": "Zone 2 transport logic"
}]
```

Substitutions use word-boundary-safe replacement to prevent partial matches (e.g., replacing `Tag1` will not affect `Tag10`).

When batching multiple rung operations, use the rung indices as they were **before any operations in the batch**. The toolkit automatically adjusts indices for insertions and deletions within the same batch.

### Scheduling Programs to Tasks

Programs must be scheduled to a task to execute:

```
schedule_program(task_name="MainTask", program_name="TransportLine1")
```

### JSR (Jump to Subroutine) Pattern

To call a subroutine from MainRoutine, add a JSR rung:

```json
[{"action": "add", "text": "JSR(FaultHandler,FaultCode,FaultMsg);", "comment": "Call fault handler subroutine"}]
```

---

## 7. AOI Operations Guide

### What AOIs Are

Add-On Instructions (AOIs) are reusable, encapsulated logic blocks -- similar to functions in programming. They have a defined parameter interface (inputs, outputs, in/out), internal local tags, and one or more routines containing the logic.

### Importing an AOI

```
import_component(file_path="C:/Templates/AOIs/MDR_Transport_AOI_v1.L5X", conflict_resolution="skip")
```

This automatically:
- Imports the AOI definition
- Imports any dependent UDTs found in the source file
- Imports any dependent AOIs found in the source file
- Updates the `EditedDate` to the current UTC time (required for Studio 5000 acceptance)
- Detects conflicts with existing definitions and handles them according to the `conflict_resolution` setting

### Querying AOI Parameters

Before calling an AOI in a rung, query its parameters to understand the interface:

```
get_entity_info(entity="aoi", name="MDR_Transport_AOI", include="parameters")
```

Returns each parameter's name, data type, usage (Input/Output/InOut), required flag, and description.

### Calling an AOI in a Rung

The call format is: `AOI_Name(InstanceTag,Param1,Param2,...);`

The instance tag is always the first argument. Subsequent arguments correspond to visible parameters (excluding EnableIn/EnableOut) in definition order.

### Common Pattern: Import, Create Instance, Add Call

1. Import the AOI: `import_component(file_path="...")`
2. Query its parameters: `get_entity_info(entity="aoi", name="MyAOI", include="parameters")`
3. Create the instance tag: `manage_tags` with `{"action": "create", "name": "MyAOI_Inst", "data_type": "MyAOI"}`
4. Add the call rung: `manage_rungs` with `{"action": "add", "text": "MyAOI(MyAOI_Inst,Input1,Output1);"}`

---

## 8. UDT Operations Guide

### What UDTs Are

User-Defined Types (UDTs) are custom data structures. They define a named collection of members, each with its own data type. UDTs are used to create organized, reusable tag structures.

### Importing a UDT

```
import_component(file_path="C:/Templates/UDTs/PalletDataTracking_DataType.L5X", conflict_resolution="skip")
```

This automatically handles transitive dependency chains: if UDT_A references UDT_B which references UDT_C, all three are imported in the correct order.

### Querying UDT Members

For visible members only (excludes hidden SINT backing fields for BIT-packed BOOLs):

```
get_entity_info(entity="udt", name="PalletDataTracking", include="members")
```

### Creating Tags of UDT Type

After importing a UDT, create tags of that type:

```json
[{"action": "create", "name": "Pallet_Data", "data_type": "PalletDataTracking", "description": "Pallet tracking data"}]
```

The toolkit generates correct default values for both L5K and Decorated formats, including all nested members.

---

## 9. Module Operations Guide

### Importing Modules from Templates

Modules have complex internal structures (connection configuration, I/O data, communication settings). Always import from a template rather than creating from scratch:

```
import_component(
    file_path="C:/Templates/Modules/FieldIO/5069_IB16.L5X",
    module_name="InputModule_Slot3",
    parent_module="Local",
    module_slot="3"
)
```

### Setting Addresses

For Ethernet modules (IP address):

```
import_component(file_path="...", module_name="ENBT_Remote1", parent_module="Local", module_address="192.168.1.100")
```

For backplane modules (slot number):

```
import_component(file_path="...", module_name="IO_Card_Slot5", parent_module="Local", module_slot="5")
```

### Module Hierarchy

Modules form a tree. The `Local` module represents the controller/chassis. Child modules reference their parent via `ParentModule` and `ParentModPortId`. The Local module cannot be deleted.

---

## 10. Validation Guide

### Always Validate Before Saving

```
validate_project()
```

### What Validation Checks

The validator performs 9 categories of checks:

1. **Structural** -- Root element is `RSLogix5000Content`, Controller exists, required child elements present, child element ordering matches the canonical L5X sequence.
2. **References** -- Tags referenced in rung text exist in the appropriate scope; data types used by tags are defined.
3. **Naming** -- No duplicate names within the same scope; all names conform to L5X naming rules (characters, length, starting character).
4. **Dependencies** -- AOIs/UDTs used by tags have definitions; parent module references are valid.
5. **Modules** -- Local module exists; parent references valid; no slot conflicts (multiple modules in the same slot).
6. **Tasks** -- At least one task defined; all scheduled programs exist; at most one continuous task.
7. **Rungs** -- All instruction text is semicolon-terminated; brackets are matched; parentheses are matched.
8. **AOI Timestamps** -- All AOI definitions have an `EditedDate` attribute.
9. **Data Formats** -- Both L5K and Decorated formats present on all non-alias tags.

### Interpreting Results

- **Errors** (fatal): Will cause Studio 5000 to reject the file or produce incorrect behavior. These MUST be fixed before saving.
- **Warnings** (non-fatal): May cause unexpected behavior. Review and fix when possible, but the file may still import.

### Common Validation Errors and Fixes

| Error | Fix |
|-------|-----|
| "Rung text must end with a semicolon" | Add `;` to the end of your instruction text |
| "Unmatched opening bracket" | Check bracket pairing in branch structures |
| "Tag uses undefined data type" | Import the required UDT or AOI first |
| "Duplicate name in scope" | Rename one of the conflicting items |
| "Schedules program which does not exist" | Create the program or fix the task reference |
| "Controller child element ordering" | This indicates a toolkit bug; report it |

---

## 11. Common Workflows

### a. Add a New Conveyor Zone

```
Step 1: load_project(file_path="C:/Projects/Plant.L5X")

Step 2: manage_tags with operations:
        [{"action": "create", "name": "Conv_A0010_Controller",
          "data_type": "MDR_Transport_AOI",
          "description": "Conveyor A0010 controller"},
         {"action": "create", "name": "Conv_A0010_Z1",
          "data_type": "DINT",
          "description": "Zone 1 speed"}]

Step 3: create_program(name="Conv_A0010", description="Conveyor A0010 control")

Step 4: manage_rungs with operations:
        [{"action": "add",
          "text": "MDR_Transport_AOI(Conv_A0010_Controller,Conv_A0010_Z1,Conv_A0010_Z2,Conv_A0010_Z3);",
          "comment": "Main transport AOI call"}]

Step 5: schedule_program(task_name="MainTask", program_name="Conv_A0010")

Step 6: validate_project()

Step 7: save_project(file_path="C:/Projects/Plant_Modified.L5X")
```

### b. Duplicate Logic for 10 Similar Devices

```
Step 1: Add the base rung with manage_rungs (action: add) for device 1.

Step 2: For devices 2 through 10, use manage_rungs with the duplicate action.

  manage_rungs with operations:
  [{"action": "duplicate", "rung_number": 0,
    "substitutions": {"Dev_001": "Dev_002", "Timer_001": "Timer_002"}}]

  manage_rungs with operations:
  [{"action": "duplicate", "rung_number": 0,
    "substitutions": {"Dev_001": "Dev_003", "Timer_001": "Timer_003"}}]

  ... and so on for each device.

  Note: When batching multiple duplications in a single manage_rungs call,
  use the original rung indices -- the toolkit automatically adjusts for
  insertions within the same batch.
```

### c. Import an AOI and Wire It Up

```
Step 1: import_component(file_path="C:/Templates/AOIs/VacuumControl_AOI.L5X")

Step 2: get_entity_info(entity="aoi", name="VacuumControl_AOI", include="parameters")

Step 3: manage_tags with operations:
        [{"action": "create", "name": "Vacuum_Station1",
          "data_type": "VacuumControl_AOI",
          "description": "Station 1 vacuum control"}]

Step 4: manage_rungs with operations:
        [{"action": "add",
          "text": "VacuumControl_AOI(Vacuum_Station1,VacRequest,VacSensor,VacValve);",
          "comment": "Station 1 vacuum control AOI call"}]
```

### d. Rename a Tag Across the Project

```
manage_tags with operations:
[{"action": "rename", "name": "OldMotorTag", "new_name": "Motor_Station1",
  "update_references": true}]
```

This updates the tag definition AND all rung text references across all programs.

### e. Add a New I/O Module

```
import_component(
    file_path="C:/Templates/Modules/FieldIO/5069_IB16.L5X",
    module_name="DI_Station2",
    parent_module="Local",
    module_slot="5")
```

### f. Modify Timer Presets in Bulk

```
Step 1: query_project(entity="tags", scope="controller")

Step 2: Use update_tags with multiple updates in a single call:
        [{"name": "TransportTimer_Z1", "members": {"PRE": "5000"}},
         {"name": "TransportTimer_Z2", "members": {"PRE": "5000"}},
         {"name": "TransportTimer_Z3", "members": {"PRE": "5000"}}]
```

---

## 12. Critical Rules and Gotchas

### Absolute Rules (Breaking These Corrupts the File)

1. **Rung text MUST end with a semicolon.** Missing semicolons cause import failures.

2. **Never generate raw XML.** Always use the MCP tools. The L5K and Decorated data formats, CDATA encoding, element ordering, and dozens of other details make hand-written XML nearly certain to fail.

3. **Always validate before saving.** Call `validate_project` and resolve all errors before `save_project`.

4. **Tag names: max 40 characters.** Must start with a letter or underscore. Only letters, digits, and underscores. The pattern is `^[A-Za-z_][A-Za-z0-9_]*$`.

5. **Controller child element ordering matters.** DataTypes before Modules before AddOnInstructionDefinitions before Tags before Programs before Tasks. The toolkit enforces this, but if you ever see an ordering error in validation, it is serious.

6. **L5K and Decorated data must stay in sync.** If they differ, Studio 5000 may crash or silently use the wrong values. The toolkit handles this automatically through `update_tags`. If you suspect data is out of sync, use `strip_l5k_data` to remove L5K data and let Studio 5000 regenerate it on import.

7. **AOI EditedDate must be current.** When importing or modifying an AOI, its `EditedDate` attribute must be updated to a recent UTC timestamp or Studio 5000 silently skips the import. The toolkit handles this automatically via `import_component`.

### Important Considerations

8. **Import dependencies before dependents.** Import UDTs before creating tags that use them. Import AOIs before creating instance tags. The toolkit import functions handle embedded dependencies automatically, but you must import the files in the right order if dependencies span multiple files.

9. **String types are structures.** STRING is not a simple scalar. It has LEN (DINT) and DATA (SINT[82]) members. Use `update_tags` with the `members` field to modify string tag members.

10. **BOOL members in UDTs use bit-packing.** Visible BOOL members in UDTs are actually BIT references into hidden SINT backing fields. The toolkit handles this in data format generation.

11. **Program-scope tags are only visible within that program.** Controller-scope tags are globally visible. If a rung references a tag that exists in the wrong scope, validation will flag it as a warning.

12. **Rung indices shift after insertions and deletions.** When batching multiple rung operations in `manage_rungs`, use the original indices -- the toolkit adjusts automatically within the same batch. Between separate `manage_rungs` calls, use `get_all_rungs` to re-check indices if needed.

13. **Routine type is immutable after creation.** You cannot change an RLL routine to ST or vice versa. Create a new routine with the correct type.

14. **Safety content is read-only.** Never modify safety scripts, safety tags, or safety signatures programmatically.

15. **Module ConfigData is opaque.** Module configuration contains binary/hex blobs specific to each catalog number. Always import modules from templates; never generate ConfigData from scratch.

---

## Appendix: MCP Tool Quick Reference

### Project Management
| Tool | Description |
|------|-------------|
| `load_project` | Load an L5X file (CALL FIRST) |
| `save_project` | Save to L5X file (validate first) |
| `format_project` | Pretty-print project XML with consistent indentation |
| `strip_l5k_data` | Remove L5K data, keeping Decorated format only |
| `get_project_summary` | Project metadata and counts |

### Query Tools
| Tool | Description |
|------|-------------|
| `query_project` | Query programs, tags, modules, AOIs, UDTs, tasks (with filtering and pagination) |
| `get_entity_info` | Detailed info for a tag, AOI, UDT, or rung (with optional value/references/parameters/members) |
| `get_all_rungs` | All rungs in a routine (with pagination) |

### Tag Operations
| Tool | Description |
|------|-------------|
| `manage_tags` | Batch tag CRUD: create, delete, rename, copy, move, create_alias |
| `update_tags` | Batch set values, member values, and descriptions |

### Program and Routine Operations
| Tool | Description |
|------|-------------|
| `create_program` | New program with MainRoutine |
| `delete_program` | Remove program and unschedule |
| `create_routine` | New routine in a program |
| `manage_rungs` | Batch rung operations: add, delete, modify, duplicate |
| `schedule_program` | Assign program to task |
| `unschedule_program` | Remove program from task |

### Import and Export Operations
| Tool | Description |
|------|-------------|
| `import_component` | Import AOI, UDT, module, program, routine, or rung from L5X file |
| `analyze_import` | Dry-run conflict analysis for an import file |
| `create_export_shell` | Create an empty export shell (rung, routine, or program) |
| `export_component` | Export a component to a standalone L5X file |

### Alarm Management
| Tool | Description |
|------|-------------|
| `manage_alarms` | Create, configure, and inspect alarm tags |
| `manage_alarm_definitions` | Manage DatatypeAlarmDefinitions for UDTs/AOIs |
| `list_alarms` | List all alarm tags and conditions |

### Analysis and Cross-Reference
| Tool | Description |
|------|-------------|
| `find_tag_references` | Find where a tag is used in rung text |
| `get_scope_references` | All tags and AOI calls within a program/routine scope |
| `find_references` | Batch reverse-lookup for tags, AOIs, or UDTs |
| `get_tag_values` | Batch tag value retrieval with member expansion |
| `compare_tag_instances` | Find duplicate structured tag instances |

### Validation and Utilities
| Tool | Description |
|------|-------------|
| `validate_project` | Run all validation checks |
| `analyze_rung_text` | Validate, extract tags from, or substitute tags in rung text |
| `detect_conflicts` | Find tag shadowing, unused tags, scope duplicates |
