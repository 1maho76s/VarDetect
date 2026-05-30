from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator, List, Optional

from .models import SharedVariable, STORAGE_QUALIFIERS
from .clang_ast import _run_clang_ast_dump

DECLARATION_RE = re.compile(
    r"^\s*(?P<storage>(?:static|extern|inline|constexpr|const|volatile|thread_local)\s+)?"
    r"(?P<type>[\w:\<\>\*\&\s]+?)\s+"
    r"(?P<decls>[^;]+?)\s*;\s*$",
    re.S,
)

NAME_RE = re.compile(
    r"^\s*(?:[\*\&\s]+)?(?P<name>[A-Za-z_]\w*)(?:\s*(?:\[[^\]]*\])*)\s*(?:=.*)?$"
)


def _strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"//.*", "", source)
    return source


def _split_statements(source: str) -> Iterator[tuple[str, int, int]]:
    line = 1
    start_line = 1
    buffer: list[str] = []
    for ch in source:
        buffer.append(ch)
        if ch == "\n":
            line += 1
            content = "".join(buffer).lstrip()
            if content.startswith("#"):
                yield "".join(buffer), start_line, line
                buffer.clear()
                start_line = line
                continue
        if ch in "{};":
            yield "".join(buffer), start_line, line
            buffer.clear()
            start_line = line
    if buffer:
        yield "".join(buffer), start_line, line


def _classify_scope_opening(header: str) -> str:
    header = header.strip()
    if not header:
        return "other"
    header = re.sub(r"\s+", " ", header)
    if header.startswith("namespace") or header.startswith("extern"):
        return "other"
    if header.startswith(("struct ", "class ", "union ", "enum ", "template ")):
        return "other"
    if re.search(r"\b(if|for|while|switch|else|do|try|catch)\b", header):
        return "function"
    if ")" in header and not re.search(r"\b(if|for|while|switch)\b", header):
        return "function"
    return "other"


def _is_function_scope(scope_stack: list[str]) -> bool:
    return any(kind == "function" for kind in scope_stack)


def _split_declarators(decls: str) -> list[str]:
    parts: list[str] = []
    current = []
    depth = 0
    for ch in decls:
        if ch == "<" or ch == "(" or ch == "[":
            depth += 1
        elif ch == ">" or ch == ")" or ch == "]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current.clear()
        else:
            current.append(ch)
    remainder = "".join(current).strip()
    if remainder:
        parts.append(remainder)
    return parts


def _is_shared_var_scope(ancestor_kinds: list[str]) -> bool:
    allowed_scopes = {"TranslationUnitDecl", "NamespaceDecl", "LinkageSpecDecl"}
    return all(kind in allowed_scopes for kind in ancestor_kinds)


def _extract_declaration_from_source(source: str, loc: dict[str, Any]) -> str:
    if not loc:
        return ""
    line = loc.get("line")
    if not isinstance(line, int):
        return ""
    lines = source.splitlines()
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()
    return ""


def _is_const_or_constexpr_var_decl(node: dict[str, Any]) -> bool:
    qual_type = ""
    if isinstance(node.get("type"), dict):
        qual_type = node["type"].get("qualType", "") or node["type"].get("typeQualType", "")
    if not qual_type:
        qual_type = node.get("typeQualType", "")
    if isinstance(qual_type, str) and re.search(r"\b(?:const|constexpr)\b", qual_type):
        return True
    if node.get("isConstexpr") or node.get("isConstInit"):
        return True
    return False


def _find_shared_variables_from_ast(ast: dict[str, Any], source: str, filename: str) -> List[SharedVariable]:
    variables: list[SharedVariable] = []

    def walk(node: Any, ancestors: list[dict[str, Any]]) -> None:
        if isinstance(node, dict):
            kind = node.get("kind")
            if kind == "VarDecl" and not node.get("isImplicit", False):
                loc = node.get("loc") or {}
                if loc.get("includedFrom") is not None:
                    pass
                else:
                    ancestor_kinds = [ancestor.get("kind") for ancestor in ancestors if isinstance(ancestor, dict)]
                    if _is_shared_var_scope(ancestor_kinds) and not _is_const_or_constexpr_var_decl(node):
                        line = loc.get("line") if isinstance(loc.get("line"), int) else 0
                        storage = node.get("storageClass") or ""
                        kind_name = "global"
                        if storage == "static":
                            kind_name = "static"
                        elif storage == "extern":
                            kind_name = "extern"
                        elif storage == "thread_local":
                            kind_name = "thread_local"
                        variables.append(
                            SharedVariable(
                                name=node.get("name", ""),
                                kind=kind_name,
                                file=filename,
                                line=line,
                                declaration=_extract_declaration_from_source(source, loc),
                            )
                        )
            for child in node.get("inner", []):
                walk(child, ancestors + [node])
        elif isinstance(node, list):
            for child in node:
                walk(child, ancestors)

    walk(ast, [])
    return variables


def _find_shared_variables_in_text_scanner(source: str, filename: str = "<text>") -> List[SharedVariable]:
    source = _strip_comments(source)
    scope_stack: list[str] = []
    variables: list[SharedVariable] = []

    for statement, start_line, _end_line in _split_statements(source):
        trimmed = statement.strip()
        if not trimmed:
            continue
        if trimmed.endswith("{"):
            header = trimmed[: trimmed.rfind("{")]
            scope_stack.append(_classify_scope_opening(header))
            continue
        if trimmed == "}":
            if scope_stack:
                scope_stack.pop()
            continue
        if not trimmed.endswith(";") or _is_function_scope(scope_stack):
            continue
        if trimmed.startswith("typedef ") or trimmed.startswith("using "):
            continue
        if trimmed.startswith("#"):
            continue

        declaration = trimmed
        if re.search(r"\b(?:const|constexpr)\b", declaration):
            continue
        match = DECLARATION_RE.match(declaration)
        if not match:
            continue

        decls = match.group("decls")
        if "(" in decls and "(*)" not in decls:
            continue

        storage = match.group("storage") or ""
        kind = "global"
        if storage.strip().startswith("static"):
            kind = "static"
        elif storage.strip().startswith("extern"):
            kind = "extern"
        elif storage.strip().startswith("thread_local"):
            kind = "thread_local"

        for declarator in _split_declarators(decls):
            name_match = NAME_RE.match(declarator)
            if not name_match:
                continue
            variables.append(
                SharedVariable(
                    name=name_match.group("name"),
                    kind=kind,
                    file=filename,
                    line=start_line,
                    declaration=declaration.strip(),
                )
            )

    return variables


def find_shared_variables_in_text(source: str, filename: str = "<text>", path: Optional[Path] = None, include_paths: Optional[list[str]] = None) -> List[SharedVariable]:
    ast = _run_clang_ast_dump(source, filename, path=path, include_paths=include_paths)
    if ast is not None:
        variables = _find_shared_variables_from_ast(ast, source, filename)
        if variables:
            return variables
    return _find_shared_variables_in_text_scanner(source, filename)
