from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SUPPORTED_EXTENSIONS = {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp"}

STORAGE_QUALIFIERS = {
    "static",
    "extern",
    "inline",
    "constexpr",
    "const",
    "volatile",
    "thread_local",
}


@dataclass
class SharedVariable:
    name: str
    kind: str
    file: str
    line: int
    declaration: str


@dataclass
class VariableAccess:
    name: str
    line: int
    column: int
    end_line: int
    is_write: bool
    expression: str
    root_name: str
    root_expression: str
    struct_type: Optional[str] = None
    member_name: Optional[str] = None
    member_offset: Optional[int] = None
    use_temp: bool = False
    use_inter_ptr: bool = False
