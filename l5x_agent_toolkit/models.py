"""
Shared data models, enumerations, and typed structures for the L5X toolkit.

Provides:
- ``str``-based enums for scope, parameter usage, routine type, etc.
  These compare equal to plain strings (``Scope.CONTROLLER == "controller"``),
  so existing code that uses bare string literals keeps working.
- Dataclasses for structured returns (TagInfo, RungInfo, etc.) that give
  IDE autocompletion, type-checker coverage, and self-documenting APIs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# ===================================================================
# Enumerations
# ===================================================================

class Scope(str, Enum):
    """Tag / element scope within an L5X project."""
    CONTROLLER = "controller"
    PROGRAM = "program"


class ParameterUsage(str, Enum):
    """AOI parameter direction."""
    INPUT = "Input"
    OUTPUT = "Output"
    INOUT = "InOut"


class RoutineType(str, Enum):
    """Supported routine language types."""
    RLL = "RLL"
    ST = "ST"
    FBD = "FBD"
    SFC = "SFC"


class RungType(str, Enum):
    """Rung classification within an RLL routine."""
    NORMAL = "N"
    DIAGNOSTIC = "D"
    SAFETY = "S"


class ExternalAccess(str, Enum):
    """Tag external access levels."""
    READ_WRITE = "Read/Write"
    READ_ONLY = "Read Only"
    NONE = "None"


# ===================================================================
# Dataclasses — structured returns
# ===================================================================

@dataclass
class TagInfo:
    """Metadata for a single tag.  Returned by tag query operations."""
    name: str
    data_type: str
    scope: str = Scope.CONTROLLER
    program: str = ""
    description: str = ""
    value: Any = None
    members: Optional[dict] = None
    radix: str = ""
    tag_type: str = "Base"
    alias_for: Optional[str] = None
    dimensions: Optional[str] = None
    external_access: str = ExternalAccess.READ_WRITE

    def to_dict(self) -> dict:
        """Serialize to a plain dict (for JSON compatibility)."""
        d: dict[str, Any] = {
            "name": self.name,
            "data_type": self.data_type,
            "scope": self.scope,
            "description": self.description,
            "tag_type": self.tag_type,
        }
        if self.program:
            d["program"] = self.program
        if self.value is not None:
            d["value"] = self.value
        if self.members is not None:
            d["members"] = self.members
        if self.radix:
            d["radix"] = self.radix
        if self.alias_for is not None:
            d["alias_for"] = self.alias_for
        if self.dimensions is not None:
            d["dimensions"] = self.dimensions
        return d


@dataclass
class RungInfo:
    """Metadata for a single rung in an RLL routine."""
    number: int
    type: str = RungType.NORMAL
    text: str = ""
    comment: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "type": self.type,
            "text": self.text,
            "comment": self.comment,
        }


@dataclass
class ParameterBinding:
    """Describes how an AOI parameter is wired in a specific call site."""
    parameter: str
    usage: str
    required: bool
    wired_tag: Optional[str] = None
    value: Any = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "parameter": self.parameter,
            "usage": self.usage,
            "required": self.required,
        }
        if self.wired_tag is not None:
            d["wired_tag"] = self.wired_tag
        if self.value is not None:
            d["value"] = self.value
        return d


@dataclass
class AoiCallInfo:
    """Describes one AOI instruction call found in rung text."""
    aoi_name: str
    instance_tag: str
    rung: int
    bindings: list[ParameterBinding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "aoi_name": self.aoi_name,
            "instance_tag": self.instance_tag,
            "rung": self.rung,
            "bindings": [b.to_dict() for b in self.bindings],
        }


@dataclass
class TagReference:
    """A single reference to a tag found during cross-reference analysis."""
    program: str
    routine: str
    rung: Optional[int] = None
    line: Optional[int] = None
    text: str = ""

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "program": self.program,
            "routine": self.routine,
            "text": self.text,
        }
        if self.rung is not None:
            d["rung"] = self.rung
        if self.line is not None:
            d["line"] = self.line
        return d


@dataclass
class ComparisonGroup:
    """A group of tag instances that share the same member values."""
    key: str
    instance_count: int
    instances: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "instance_count": self.instance_count,
            "instances": self.instances,
        }


@dataclass
class ComparisonResult:
    """Result of a compare_tag_instances operation."""
    data_type: str
    match_members: list[str]
    filter_applied: Optional[dict]
    total_instances: int
    groups_with_duplicates: int
    groups: list[ComparisonGroup] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "data_type": self.data_type,
            "match_members": self.match_members,
            "filter_applied": self.filter_applied,
            "total_instances": self.total_instances,
            "groups_with_duplicates": self.groups_with_duplicates,
            "groups": [g.to_dict() for g in self.groups],
        }
