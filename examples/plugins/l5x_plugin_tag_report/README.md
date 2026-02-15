# L5X Tag Report Plugin

An example plugin for the [L5X Agent Toolkit](https://github.com/Axiara/l5x-toolkit) that demonstrates the plugin architecture while providing genuinely useful reporting tools.

## What It Does

This plugin adds three MCP tools to the L5X Agent Toolkit server:

| Tool | Description |
|------|-------------|
| `export_tags_csv` | Export tags to CSV for documentation, review, or CMMS import |
| `audit_tag_naming` | Check tags against naming convention rules (prefix, length, case) |
| `project_statistics` | Generate a full project health report with tag/program/type stats |

## Installation

```bash
cd examples/plugins/l5x_plugin_tag_report
pip install -e .
```

That's it — the next time the MCP server starts, it will automatically discover and load this plugin. Verify with:

```
> list_plugins
```

## Usage Examples

### Export Tags to CSV

```
> export_tags_csv("/path/to/tags.csv")
Exported 147 tag(s) to: /path/to/tags.csv

> export_tags_csv("/path/to/motor_tags.csv", name_filter="MTR_*")
Exported 12 tag(s) to: /path/to/motor_tags.csv
```

### Audit Naming Conventions

```
> audit_tag_naming()
{
  "tags_checked": 147,
  "total_violations": 3,
  "violations": {
    "double_underscore": [{"tag": "Motor__Speed"}]
  }
}

> audit_tag_naming(rules_json='{"require_prefix": true, "prefixes": ["AI_", "DI_", "DO_", "AO_", "MTR_", "VLV_"]}')
```

### Project Statistics

```
> project_statistics()
{
  "tags": {"controller_count": 85, "total_all_scopes": 147, ...},
  "programs": {"count": 4, "total_rungs": 312, ...},
  "data_types": {"udt_count": 3, "aoi_count": 2, ...},
  ...
}
```

## How This Plugin Works

This plugin serves as a reference implementation for plugin developers. The key parts:

1. **`plugin.py`** — Contains `TagReportPlugin`, a subclass of `L5XPlugin`
2. **`pyproject.toml`** — Declares the `l5x_toolkit.plugins` entry point
3. **No XML manipulation** — Uses the stable `PluginContext` API

See [PLUGINS.md](../../../PLUGINS.md) in the repository root for the full plugin development guide.
