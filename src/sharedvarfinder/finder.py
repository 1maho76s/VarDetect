from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Optional

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

DECLARATION_RE = re.compile(
    r"^\s*(?P<storage>(?:static|extern|inline|constexpr|const|volatile|thread_local)\s+)?"
    r"(?P<type>[\w:\<\>\*\&\s]+?)\s+"
    r"(?P<decls>[^;]+?)\s*;\s*$",
    re.S,
)

NAME_RE = re.compile(
    r"^\s*(?:[\*\&\s]+)?(?P<name>[A-Za-z_]\w*)(?:\s*(?:\[[^\]]*\])*)\s*(?:=.*)?$"
)

@dataclass
class SharedVariable:
    name: str
    kind: str
    file: str
    line: int
    declaration: str


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


def _language_and_std_from_filename(filename: str, source: Optional[str] = None) -> tuple[str, str]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".c":
        return "c", "-std=c11"
    if suffix in {".cpp", ".cc", ".cxx", ".c++", ".cp"}:
        return "c++", "-std=c++17"
    if suffix in {".hpp", ".hh", ".hxx"}:
        return "c++", "-std=c++17"
    if suffix == ".h":
        if source and re.search(r"\b(namespace|class|template|constexpr|decltype|using|operator|virtual|public|private|protected)\b", source):
            return "c++", "-std=c++17"
        return "c", "-std=c11"
    return "c", "-std=c11"


def _run_clang_ast_dump(source: str, filename: str, path: Optional[Path] = None, include_paths: Optional[list[str]] = None, struct_header: Optional[str] = None) -> Optional[dict[str, Any]]:
    language, std_flag = _language_and_std_from_filename(filename, source)
    clang = "clang++" if language == "c++" else "clang"
    clang_path = shutil.which(clang)
    if clang_path is None:
        return None

    needs_cleanup = False
    if path is not None and path.exists():
        input_path = str(path)
    else:
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=Path(filename).suffix or ".c",
            delete=False,
            encoding="utf-8",
        )
        tmp.write(source)
        tmp.flush()
        tmp.close()
        input_path = tmp.name
        needs_cleanup = True

    try:
        include_args = []
        if language == "c++":
            include_args = ["-I/usr/include", "-I/usr/include/c++/11", "-I/usr/include/x86_64-linux-gnu/c++/11"]
        else:
            include_args = ["-I/usr/include", "-I/usr/include/x86_64-linux-gnu"]
        if include_paths:
            include_args = [f"-I{p}" for p in include_paths] + include_args
        struct_header_args = []
        if struct_header and os.path.isfile(struct_header):
            struct_header_args = ["-include", struct_header]
        result = subprocess.run(
            [
                clang_path,
                "-Xclang",
                "-ast-dump=json",
                "-fsyntax-only",
                "-x",
                language,
                std_flag,
            ] + struct_header_args + include_args + [
                input_path,
            ],
            capture_output=True,
            text=True,
        )
        if not result.stdout:
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
    finally:
        if needs_cleanup:
            try:
                os.unlink(input_path)
            except OSError:
                pass


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
                    pass  # Skip variables from included files
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


@dataclass
class VariableAccess:
    name: str
    line: int
    column: int
    end_line: int
    is_write: bool
    expression: str  # the full expression like "g", "s.x", "*p"
    root_name: str
    root_expression: str
    struct_type: Optional[str] = None  # for struct member accesses
    member_name: Optional[str] = None  # for struct member accesses
    member_offset: Optional[int] = None  # byte offset in struct
    use_temp: bool = False  # use SIM_FLUSH_TEMP instead of SIM_FLUSH
    use_inter_ptr: bool = False  # use SIM_FLUSH_INTER_PTR for intermediate pointer reads


def _find_variable_accesses_from_ast(ast: dict[str, Any], shared_vars: set[str], source: str, conservative_ptr: bool = False, bitfield_map: Optional[dict[str, dict[str, bool]]] = None, temp_flush: bool = False) -> List[VariableAccess]:
    accesses: list[VariableAccess] = []

    source_bytes = source.encode("utf-8")

    def byte_offset_to_char_index(offset: int) -> int:
        if offset <= 0:
            return 0
        return len(source_bytes[:offset].decode("utf-8", errors="ignore"))

    def offset_to_line_col(offset: int, source: str) -> tuple[int, int]:
        char_index = byte_offset_to_char_index(offset)
        lines = source[:char_index].splitlines(keepends=True)
        line = len(lines)
        col = len(lines[-1]) if lines else 0
        return line, col

    assignment_ops = {"=", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "^=", "|="}

    def _get_loc_value(loc: dict[str, Any], key: str) -> Optional[int]:
        if not isinstance(loc, dict):
            return None
        value = loc.get(key)
        if isinstance(value, int):
            return value
        for child_key in ("expansionLoc", "spellingLoc"):
            child = loc.get(child_key)
            if isinstance(child, dict):
                child_value = child.get(key)
                if isinstance(child_value, int):
                    return child_value
        return None

    def _is_included_from(loc: dict[str, Any]) -> bool:
        if not isinstance(loc, dict):
            return False
        if loc.get("includedFrom") is not None:
            return True
        for child_key in ("spellingLoc", "expansionLoc"):
            child = loc.get(child_key)
            if isinstance(child, dict) and child.get("includedFrom") is not None:
                return True
        return False

    def range_text(rng: dict[str, Any]) -> str:
        begin = rng.get("begin", {})
        end = rng.get("end", {})
        start = _get_loc_value(begin, "offset") or 0
        end_offset = _get_loc_value(end, "offset") or 0
        tok_len = _get_loc_value(end, "tokLen") or 0
        if start < 0 or end_offset < start:
            return ""
        start_char = byte_offset_to_char_index(start)
        end_char = byte_offset_to_char_index(end_offset + tok_len)
        if end_char <= start_char:
            return ""
        return source[start_char:end_char].strip()

    def _is_source_node(node: dict[str, Any]) -> bool:
        loc = node.get("loc") or {}
        if _is_included_from(loc):
            return False
        rng = node.get("range", {})
        begin = rng.get("begin", {})
        if _is_included_from(begin):
            return False
        offset = _get_loc_value(begin, "offset")
        if not isinstance(offset, int) or offset <= 0:
            return False
        return True

    def _root_decl_name(node: dict[str, Any]) -> str:
        if not isinstance(node, dict):
            return ""
        kind = node.get("kind", "")
        if kind == "DeclRefExpr":
            return node.get("referencedDecl", {}).get("name", "")
        # 可穿透的节点类型列表:
        # - MemberExpr/ArraySubscriptExpr: 成员访问/数组下标, 递归找 base
        # - UnaryOperator: 解引用 (*p) 或取地址 (&x)
        # - ImplicitCastExpr/CStyleCastExpr/ParenExpr/CastExpr: 类型转换/括号
        # - RecoveryExpr: Clang 无法解析类型时的恢复节点
        # - StmtExpr/CompoundStmt/DeclStmt/VarDecl: GNU 语句表达式 ({...}),
        #   如 container_of / list_entry 宏展开后的完整 AST 结构:
        #   RecoveryExpr → StmtExpr → CompoundStmt → DeclStmt → VarDecl(__mptr)
        #     → RecoveryExpr → ParenExpr → DeclRefExpr(link)
        #   需要穿透 VarDecl 才能到达内部的 DeclRefExpr
        _PASSTHROUGH_KINDS = {"MemberExpr", "ArraySubscriptExpr", "UnaryOperator",
                    "ImplicitCastExpr", "CStyleCastExpr", "ParenExpr", "CastExpr",
                    "RecoveryExpr", "CXXDependentScopeMemberExpr",
                    "StmtExpr", "CompoundStmt", "DeclStmt", "VarDecl"}
        if kind in _PASSTHROUGH_KINDS:
            if kind in ("MemberExpr", "CXXDependentScopeMemberExpr") and isinstance(node.get("base"), dict):
                root = _root_decl_name(node["base"])
                if root:
                    return root
            if kind == "ArraySubscriptExpr" and isinstance(node.get("base"), dict):
                root = _root_decl_name(node["base"])
                if root:
                    return root
            # Check subExpr first (UnaryOperator, and some cast nodes use it)
            if isinstance(node.get("subExpr"), dict):
                root = _root_decl_name(node["subExpr"])
                if root:
                    return root
            for child in node.get("inner", []):
                if isinstance(child, dict):
                    root = _root_decl_name(child)
                    if root:
                        return root
        return ""

    def _root_expression(node: dict[str, Any]) -> str:
        if not isinstance(node, dict):
            return ""
        kind = node.get("kind", "")
        if kind == "DeclRefExpr":
            return range_text(node.get("range", {})) or node.get("referencedDecl", {}).get("name", "")
        if kind in ("MemberExpr", "CXXDependentScopeMemberExpr"):
            if isinstance(node.get("base"), dict):
                return _root_expression(node["base"])
            for child in node.get("inner", []):
                if isinstance(child, dict):
                    root = _root_expression(child)
                    if root:
                        return root
            return range_text(node.get("range", {}))
        if kind == "ArraySubscriptExpr":
            if isinstance(node.get("base"), dict):
                return _root_expression(node["base"])
            for child in node.get("inner", []):
                if isinstance(child, dict):
                    root = _root_expression(child)
                    if root:
                        return root
            return range_text(node.get("range", {}))
        if kind == "UnaryOperator":
            if isinstance(node.get("subExpr"), dict):
                return _root_expression(node["subExpr"])
            for child in node.get("inner", []):
                if isinstance(child, dict):
                    root = _root_expression(child)
                    if root:
                        return root
            return range_text(node.get("range", {}))
        if kind in {"ImplicitCastExpr", "CStyleCastExpr", "ParenExpr", "CastExpr", "RecoveryExpr"}:
            if isinstance(node.get("subExpr"), dict):
                root = _root_expression(node["subExpr"])
                if root:
                    return root
            for child in node.get("inner", []):
                if isinstance(child, dict):
                    root = _root_expression(child)
                    if root:
                        return root
            return range_text(node.get("range", {}))
        return range_text(node.get("range", {}))

    id_map: dict[str, dict[str, Any]] = {}
    shared_pointer_vars: set[str] = set()

    def build_id_map(node: Any) -> None:
        if isinstance(node, dict):
            nid = node.get("id")
            if isinstance(nid, str):
                id_map[nid] = node
            for child in node.get("inner", []):
                build_id_map(child)
        elif isinstance(node, list):
            for child in node:
                build_id_map(child)

    def _declref_name(node: dict[str, Any]) -> str:
        decl = node.get("referencedDecl", {})
        return decl.get("name", "")

    def _is_shared_pointer_declref(node: dict[str, Any]) -> bool:
        if node.get("kind") != "DeclRefExpr":
            return False
        return _declref_name(node) in shared_pointer_vars

    def _is_shared_declref(node: dict[str, Any]) -> bool:
        if node.get("kind") != "DeclRefExpr":
            return False
        decl = node.get("referencedDecl", {})
        return decl.get("name", "") in shared_vars

    def _is_shared_address_of(node: dict[str, Any]) -> bool:
        """检查节点是否是对共享变量的\"取地址\"或直接引用共享内存。
        
        递归穿透类型转换、括号等, 最终判断底层是否是:
        - 共享变量的 DeclRefExpr (如 g_shared)
        - 共享指针变量的 DeclRefExpr (如 tp, 来自函数参数)
        - 共享指针变量的成员访问 (如 tp->peer, tp->xclose_state[dn])
        """
        if not isinstance(node, dict):
            return False
        kind = node.get("kind")
        if kind == "DeclRefExpr":
            return _is_shared_declref(node) or _is_shared_pointer_declref(node)
        if kind == "UnaryOperator" and node.get("opcode") == "&":
            subexpr = node.get("subExpr")
            if isinstance(subexpr, dict):
                return _is_shared_address_of(subexpr)
            for child in node.get("inner", []):
                if isinstance(child, dict) and _is_shared_address_of(child):
                    return True
            return False
        # 成员访问: tp->peer, tp->xclose_state 等, 递归检查 base
        if kind in ("MemberExpr", "CXXDependentScopeMemberExpr"):
            if isinstance(node.get("base"), dict):
                return _is_shared_address_of(node["base"])
            for child in node.get("inner", []):
                if isinstance(child, dict) and _is_shared_address_of(child):
                    return True
            return False
        # 数组下标: tp->xclose_state[dn] 等, 递归检查 base
        if kind == "ArraySubscriptExpr":
            if isinstance(node.get("base"), dict):
                return _is_shared_address_of(node["base"])
            for child in node.get("inner", []):
                if isinstance(child, dict) and _is_shared_address_of(child):
                    return True
            return False
        if kind in {"ImplicitCastExpr", "ParenExpr", "CastExpr", "CStyleCastExpr"}:
            if isinstance(node.get("subExpr"), dict) and _is_shared_address_of(node["subExpr"]):
                return True
            for child in node.get("inner", []):
                if isinstance(child, dict) and _is_shared_address_of(child):
                    return True
            return False
        return False

    def _collect_shared_pointer_aliases(node: Any) -> None:
        """数据流传播分析: 收集所有从共享变量派生的局部指针变量。
        
        工作原理 (前向传播, 单次遍历):
        1. VarDecl + 指针类型 + 初始化器来自共享内存 → 加入 shared_pointer_vars
        2. BinaryOperator 赋值 = 右侧来自共享内存 → 加入 shared_pointer_vars
        
        后续在 AST 遍历中, 对 shared_pointer_vars 中的变量做 *p 或 p->member
        解引用时, 会像直接访问共享变量一样插入 SIM_FLUSH。
        
        注意: 只追踪指针类型的局部变量。非指针局部变量 (如 int x = tp->counter)
        的值拷贝到本地栈上, 不需要 flush。
        """
        if isinstance(node, dict):
            kind = node.get("kind")
            if kind == "VarDecl":
                name = node.get("name", "")
                typ = node.get("type", {}).get("qualType", "")
                # 只追踪指针类型的局部变量 (非指针类型的值在本地栈上, 无需追踪)
                if "*" in typ:
                    init = node.get("init")
                    # Case 1: init 在 inner 列表中, 且是 UnaryOperator(&)
                    #         如 struct T *p = &shared_var;
                    found_in_inner = False
                    for child in node.get("inner", []):
                        if isinstance(child, dict) and child.get("kind") == "UnaryOperator" and _is_shared_address_of(child):
                            shared_pointer_vars.add(name)
                            found_in_inner = True
                            break
                    if found_in_inner:
                        pass  # already added
                    # Case 2: init 直接是取地址表达式
                    #         如 int *p = &g_shared;
                    elif isinstance(init, dict) and init.get("kind") == "UnaryOperator" and _is_shared_address_of(init):
                        shared_pointer_vars.add(name)
                    # Case 3: init 的 inner 表达式来自共享指针变量的成员/解引用
                    #         如 struct T *peer = tp->peer.psp;
                    #         通过 _root_decl_name 找到根变量 tp, 检查是否在 shared_pointer_vars 中
                    else:
                        added = False
                        for child in node.get("inner", []):
                            if isinstance(child, dict) and child.get("kind") not in ("ParmVarDecl",):
                                root_name = _root_decl_name(child)
                                if root_name and root_name in shared_pointer_vars:
                                    shared_pointer_vars.add(name)
                                    added = True
                                    break
                        if not added:
                            # Case 4: init 是字符串, 且该字符串恰好是共享指针变量名
                            #         如 struct T *ctx = (typeof(ctx))c; clang 把 init 存为 "c"
                            if isinstance(init, str) and init in shared_pointer_vars:
                                shared_pointer_vars.add(name)
                            # Case 5: 深度搜索 DeclRefExpr — 处理 container_of/list_entry 宏
                            #   这些宏展开后 AST 结构很深:
                            #   RecoveryExpr → StmtExpr → CompoundStmt → DeclStmt
                            #     → VarDecl(__mptr) → RecoveryExpr → ParenExpr → DeclRefExpr(link)
                            #   _root_decl_name 可能因为中间节点类型不在穿透列表中而失败,
                            #   所以这里做一次暴力深度搜索, 找到所有 DeclRefExpr 引用的变量名,
                            #   如果其中任何一个在 shared_pointer_vars 中, 则当前变量也是共享指针。
                            elif not added:
                                # Case 5: 源码文本回退 — 处理 container_of/list_entry 宏
                                #   当 Clang 无法完整解析宏展开 (CompoundStmt 为空) 时,
                                #   回退到源码文本扫描: 从 VarDecl 所在行的源码中提取标识符,
                                #   检查是否有已知的共享指针变量名出现在初始化表达式中。
                                #   典型场景:
                                #     struct ase_tcp_port *sp = list_entry(link, ...);
                                #   源码中 "link" 出现在 sp 的声明行, 且 link ∈ shared_pointer_vars
                                #   → sp 也应被追踪。
                                _line_num = 0
                                # 尝试多种方式获取行号 (Clang AST 中 line 字段可能被省略)
                                _loc = node.get("loc", {})
                                _line_num = _loc.get("line", 0)
                                if not _line_num:
                                    # 从 range.end.expansionLoc 获取 (宏展开位置)
                                    _range = node.get("range", {})
                                    for _endpoint in ("end", "begin"):
                                        _ep = _range.get(_endpoint, {})
                                        if "expansionLoc" in _ep:
                                            _line_num = _ep["expansionLoc"].get("line", 0)
                                            if _line_num:
                                                break
                                        _line_num = _ep.get("line", 0)
                                        if _line_num:
                                            break
                                if not _line_num and _loc.get("offset"):
                                    # 通过 byte offset 计算行号
                                    _offset = _loc["offset"]
                                    _line_num = source[:_offset].count('\n') + 1
                                _src_lines = source.splitlines()
                                if _line_num and 1 <= _line_num <= len(_src_lines):
                                    import re
                                    _src_line = _src_lines[_line_num - 1]
                                    # 提取源码行中所有 C 标识符
                                    _identifiers = set(re.findall(r'\b[a-zA-Z_]\w*\b', _src_line))
                                    # 如果声明行中出现了已知的共享指针变量名,
                                    # 说明当前变量从共享内存派生, 加入追踪集合
                                    if _identifiers & shared_pointer_vars:
                                        shared_pointer_vars.add(name)
            elif kind == "BinaryOperator" and node.get("opcode") == "=":
                # 赋值语句: 左侧是指针变量, 右侧来自共享内存
                # 如: struct T *p = NULL; ... p = tp->peer;
                # 注意: 不要求左侧变量已在 shared_pointer_vars 中,
                #       因为变量可能在声明时未初始化 (或初始化为 NULL),
                #       后续才被赋值为共享指针。
                children = [c for c in node.get("inner", []) if isinstance(c, dict)]
                if len(children) >= 2:
                    lhs, rhs = children[0], children[1]
                    lhs_name = _declref_name(lhs) if lhs.get("kind") == "DeclRefExpr" else ""
                    # 同时用 _is_shared_address_of (处理地址/成员访问) 
                    # 和 _root_decl_name (处理非指针成员链) 检查右侧
                    if lhs_name and (
                        _is_shared_address_of(rhs)
                        or _root_decl_name(rhs) in shared_pointer_vars
                    ):
                        shared_pointer_vars.add(lhs_name)
            for child in node.get("inner", []):
                _collect_shared_pointer_aliases(child)
        elif isinstance(node, list):
            for child in node:
                _collect_shared_pointer_aliases(child)

    build_id_map(ast)

    def _collect_conservative_ptr_params(node: Any) -> None:
        """When conservative_ptr=True, treat all pointer typed function parameters as
        potential shared-variable pointers so that *param accesses are instrumented."""
        if isinstance(node, dict):
            kind = node.get("kind", "")
            if kind == "ParmVarDecl":
                name = node.get("name", "")
                typ = node.get("type", {}).get("qualType", "")
                # Only add params from the source file, not from system includes
                loc = node.get("loc") or {}
                if "*" in typ and name and not _is_included_from(loc):
                    shared_pointer_vars.add(name)
            for child in node.get("inner", []):
                _collect_conservative_ptr_params(child)
        elif isinstance(node, list):
            for child in node:
                _collect_conservative_ptr_params(child)

    if conservative_ptr:
        _collect_conservative_ptr_params(ast)

    _collect_shared_pointer_aliases(ast)

    def _get_enclosing_end_line(ancestors: list[dict[str, Any]], default_line: int) -> int:
        for parent in reversed(ancestors):
            if parent.get("kind") in {"CallExpr", "CXXOperatorCallExpr", "BinaryOperator", "CompoundAssignOperator"}:
                rng = parent.get("range", {})
                end = rng.get("end", {})
                line = _get_loc_value(end, "line")
                return line if isinstance(line, int) else default_line
        return default_line

    def _add_access(name: str, line: int, col: int, end_line: int, expr: str, root_name: str, root_expression: str, struct_type: Optional[str] = None, member_name: Optional[str] = None, member_offset: Optional[int] = None, is_write: bool = False, use_temp: bool = False) -> None:
        if line <= 0 or end_line <= 0 or not expr.strip():
            return
        accesses.append(VariableAccess(name, line, col, end_line, is_write, expr, root_name, root_expression, struct_type, member_name, member_offset, use_temp))

    def _node_has_shared_reference(node: dict[str, Any], ancestors: Optional[list[dict[str, Any]]] = None) -> bool:
        if ancestors is None:
            ancestors = []
        if not isinstance(node, dict):
            return False
        kind = node.get("kind", "")
        if kind == "DeclRefExpr":
            if _is_shared_declref(node):
                return True
            if _is_shared_pointer_declref(node):
                return any(
                    (parent.get("kind") == "UnaryOperator" and parent.get("opcode") == "*")
                    or (parent.get("kind") in ("MemberExpr", "CXXDependentScopeMemberExpr") and parent.get("isArrow"))
                    # CXXDependentScopeMemberExpr with isArrow=False may still be
                    # a pointer dereference — clang uses it when types are unknown
                    # and collapses intermediate -> members (e.g. tp->peer.out
                    # becomes CXXDependentScopeMemberExpr(.out) directly over tp)
                    or parent.get("kind") == "CXXDependentScopeMemberExpr"
                    for parent in ancestors
                )
            return False
        if kind in ("MemberExpr", "CXXDependentScopeMemberExpr"):
            base = node.get("base")
            if isinstance(base, dict) and _node_has_shared_reference(base, ancestors + [node]):
                return True
            for child in node.get("inner", []):
                if isinstance(child, dict) and _node_has_shared_reference(child, ancestors + [node]):
                    return True
            return False
        if kind == "UnaryOperator":
            subexpr = node.get("subExpr")
            if isinstance(subexpr, dict) and _node_has_shared_reference(subexpr, ancestors + [node]):
                return True
            for child in node.get("inner", []):
                if isinstance(child, dict) and _node_has_shared_reference(child, ancestors + [node]):
                    return True
            return False
        if kind in {"ImplicitCastExpr", "CStyleCastExpr", "ParenExpr", "CastExpr", "RecoveryExpr", "BinaryOperator", "CompoundAssignOperator", "CXXOperatorCallExpr", "CallExpr", "ArraySubscriptExpr", "CXXDependentScopeMemberExpr"}:
            for child in node.get("inner", []):
                if isinstance(child, dict) and _node_has_shared_reference(child, ancestors + [node]):
                    return True
            return False
        return False

    def _member_expr_access_expression(node: dict[str, Any]) -> str:
        ref = node.get("referencedMemberDecl", {})
        ref_kind = None
        if isinstance(ref, dict):
            ref_kind = ref.get("kind")
        elif isinstance(ref, str) and ref in id_map:
            ref_kind = id_map[ref].get("kind")
        if ref_kind in {"CXXMethodDecl", "FunctionDecl", "CXXConstructorDecl", "CXXDestructorDecl"}:
            base = node.get("base")
            if isinstance(base, dict):
                return node_expression(base)
            for child in node.get("inner", []):
                if isinstance(child, dict):
                    return node_expression(child)
        result = range_text(node.get("range", {}))
        return result

    def node_expression(node: dict[str, Any]) -> str:
        kind = node.get("kind", "")
        if kind == "DeclRefExpr":
            decl = node.get("referencedDecl", {})
            return decl.get("name", "") or node.get("name", "")

        if kind == "MemberExpr" or kind == "CXXDependentScopeMemberExpr":
            # Use range_text: more reliable than reconstructing from base
            # (RecoveryExpr bases collapse intermediate member chains)
            return range_text(node.get("range", {}))

        if kind == "UnaryOperator":
            opcode = node.get("opcode", "")
            subexpr = None
            if isinstance(node.get("subExpr"), dict):
                subexpr = node_expression(node["subExpr"])
            else:
                for child in node.get("inner", []):
                    if isinstance(child, dict):
                        subexpr = node_expression(child)
                        break
            if not subexpr:
                return range_text(node.get("range", {}))
            if opcode == "&":
                return subexpr
            if node.get("isPostfix"):
                return f"{subexpr}{opcode}"
            return f"{opcode}{subexpr}"

        if kind == "ArraySubscriptExpr":
            return range_text(node.get("range", {}))

        if kind in {"ImplicitCastExpr", "CStyleCastExpr", "ParenExpr", "CastExpr", "RecoveryExpr"}:
            if isinstance(node.get("subExpr"), dict):
                return node_expression(node["subExpr"])
            for child in node.get("inner", []):
                if isinstance(child, dict):
                    return node_expression(child)
            return range_text(node.get("range", {}))

        if kind in {"BinaryOperator", "CompoundAssignOperator", "CXXOperatorCallExpr", "CallExpr"}:
            for child in node.get("inner", []):
                if isinstance(child, dict):
                    return node_expression(child)
            return range_text(node.get("range", {}))

        # Fallback to source range
        return range_text(node.get("range", {}))

    def walk(node: Any, ancestors: list[dict[str, Any]], in_return_stmt: bool = False, in_function: bool = False, is_in_assignment: bool = False) -> None:
        # print(f"rootname: {_root_decl_name(node) if isinstance(node, dict) else ''}, expr: {node_expression(node) if isinstance(node, dict) else ''}, kind: {node.get('kind') if isinstance(node, dict) else ''}, in_return_stmt: {in_return_stmt}, in_function: {in_function}, is_in_assignment: {is_in_assignment}")
        if isinstance(node, dict):
            kind = node.get("kind")
            current_in_function = in_function or (kind == "FunctionDecl" and not _is_included_from(node.get("loc", {})))
            current_in_return = in_return_stmt
            if kind == "ReturnStmt":
                current_in_return = True
            if kind == "FunctionDecl":
                # Skip functions defined in included headers (e.g. static inline in .h files)
                if _is_included_from(node.get("loc", {})):
                    return
                # Walk the body with in_function=True
                body = node.get("inner", [])
                for child in body:
                    if isinstance(child, dict) and child.get("kind") == "CompoundStmt":
                        walk(child, ancestors + [node], current_in_return, True, is_in_assignment)
                    else:
                        walk(child, ancestors + [node], current_in_return, current_in_function, is_in_assignment)
            elif kind == "VarDecl" and not current_in_return and current_in_function:
                init = node.get("init")
                if isinstance(init, dict):
                    walk(init, ancestors + [node], current_in_return, current_in_function, True)
                for child in node.get("inner", []):
                    if child is not init and isinstance(child, dict):
                        walk(child, ancestors + [node], current_in_return, current_in_function, True)
            elif ((kind == "BinaryOperator" and node.get("opcode") in assignment_ops)
                  or kind == "CompoundAssignOperator"
                  or kind == "CXXOperatorCallExpr") and not current_in_return and current_in_function:
                # Instrument LHS for all assignment operators (=, +=, |=, etc.)
                if kind == "BinaryOperator" and node.get("opcode") == "=":
                    children = [c for c in node.get("inner", []) if isinstance(c, dict)]
                    if len(children) >= 2:
                        lhs, rhs = children[0], children[1]
                        lhs_is_shared = lhs.get("kind") in {"MemberExpr", "UnaryOperator", "ArraySubscriptExpr", "CXXDependentScopeMemberExpr"} and _node_has_shared_reference(lhs, ancestors + [node]) and _is_source_node(lhs)
                        rhs_has_shared = _node_has_shared_reference(rhs, ancestors + [node])
                        if lhs_is_shared or (rhs_has_shared and lhs.get("kind") in {"MemberExpr", "UnaryOperator", "ArraySubscriptExpr", "CXXDependentScopeMemberExpr"} and _is_source_node(lhs)):
                            rng = lhs.get("range", {})
                            begin = rng.get("begin", {})
                            end = rng.get("end", {})
                            offset = _get_loc_value(begin, "offset") or 0
                            line, col = offset_to_line_col(offset, source)
                            end_line = _get_enclosing_end_line(ancestors + [node], _get_loc_value(end, "line") if isinstance(_get_loc_value(end, "line"), int) else line)
                            expr = node_expression(lhs)
                            _add_access("", line, col, end_line, expr, _root_decl_name(lhs), _root_expression(lhs), is_write=True)
                elif kind == "CompoundAssignOperator":
                    # For compound assignments (|=, +=, etc.), the LHS may be
                    # collapsed by clang (e.g. tp->se->exp_evts becomes tp->se).
                    # Use range_text on the full operator and extract LHS.
                    full_text = range_text(node.get("range", {}))
                    if full_text:
                        m = re.match(r'^(.+?)\s*(\|=|\+=|-=|\*=|/=|%=|<<=|>>=|&=|\^=)', full_text)
                        if m:
                            lhs_text = m.group(1).strip()
                            # Check if LHS text contains any shared pointer variable
                            if any(pn in lhs_text for pn in shared_pointer_vars):
                                rng = node.get("range", {})
                                begin = rng.get("begin", {})
                                offset = _get_loc_value(begin, "offset") or 0
                                line, col = offset_to_line_col(offset, source)
                                if line > 0:
                                    end_rng = node.get("range", {}).get("end", {})
                                    end_line = _get_enclosing_end_line(ancestors + [node], _get_loc_value(end_rng, "line") if isinstance(_get_loc_value(end_rng, "line"), int) else line)
                                    root = lhs_text.split('->')[0].split('.')[0].strip()
                                    _add_access("", line, col, end_line, lhs_text, root, root, is_write=True)
                for child in node.get("inner", []):
                    if isinstance(child, dict):
                        walk(child, ancestors + [node], current_in_return, current_in_function, True)
            elif kind == "DeclRefExpr":
                # Direct shared variable reference
                if _is_shared_declref(node) and not current_in_return and current_in_function:
                    # Ignore DeclRefExpr nodes that are part of larger shared member, unary, or array expressions
                    if not any(parent.get("kind") in {"MemberExpr", "UnaryOperator", "ArraySubscriptExpr"} for parent in ancestors):
                        rng = node.get("range", {})
                        begin = rng.get("begin", {})
                        end = rng.get("end", {})
                        offset = _get_loc_value(begin, "offset") or 0
                        line, col = offset_to_line_col(offset, source)
                        end_line = _get_enclosing_end_line(ancestors, _get_loc_value(end, "line") if isinstance(_get_loc_value(end, "line"), int) else line)
                        if _is_source_node(node):
                            is_write = _is_write_access(node, ancestors)
                            _add_access(node.get("referencedDecl", {}).get("name", ""), line, col, end_line, node.get("referencedDecl", {}).get("name", ""), _root_decl_name(node), _root_expression(node), is_write=is_write)
            elif kind in ("MemberExpr", "CXXDependentScopeMemberExpr"):
                # Member access on a shared object (or dependent-scope member for unknown types)
                if _node_has_shared_reference(node, ancestors) and not current_in_return and current_in_function:
                    rng = node.get("range", {})
                    begin = rng.get("begin", {})
                    end = rng.get("end", {})
                    offset = _get_loc_value(begin, "offset") or 0
                    line, col = offset_to_line_col(offset, source) if offset > 0 else (0, 0)
                    if line <= 0:
                        # Try ancestor range
                        for anc in reversed(ancestors):
                            arng = anc.get("range", {})
                            ab = arng.get("begin", {})
                            ao = _get_loc_value(ab, "offset") or 0
                            if ao > 0:
                                line, col = offset_to_line_col(ao, source)
                                rng = arng
                                break
                    if line <= 0:
                        line = _get_loc_value(begin, "line") or 0
                        if line > 0:
                            col = _get_loc_value(begin, "col") or 0
                    if line <= 0:
                        # Cannot determine position — still add access with enclosing line
                        for anc in reversed(ancestors):
                            arng = anc.get("range", {})
                            ab = arng.get("begin", {})
                            al = _get_loc_value(ab, "line") or 0
                            if al > 0:
                                line = al
                                col = _get_loc_value(ab, "col") or 0
                                rng = arng
                                break
                    end_line = _get_enclosing_end_line(ancestors + [node], _get_loc_value(end, "line") if isinstance(_get_loc_value(end, "line"), int) else line)
                    if line <= 0:
                        line = end_line  # fallback: use enclosing statement line
                    expr = _member_expr_access_expression(node)
                    member_name = node.get("name") or node.get("member") or ""
                    root_name = _root_decl_name(node)
                    root_expr = _root_expression(node)
                    # Infer struct type from root_name
                    struct_type = None
                    member_offset = None
                    if root_name == "g_shared":
                        struct_type = "SharedState"
                        member_offset = _get_struct_member_offset(struct_type, member_name)
                    else:
                        # Try to infer struct type from the first inner DeclRefExpr's type
                        for child in node.get("inner", []):
                            if isinstance(child, dict) and child.get("kind") in ("DeclRefExpr", "MemberExpr"):
                                base_type = child.get("type", {}).get("qualType", "")
                                m = re.match(r'(?:const\s+|volatile\s+)*(?:struct\s+)?(\w+)', base_type)
                                if m:
                                    struct_type = m.group(1)
                                    break
                    # Skip bit-field members (can't take address of bit-field)
                    if (bitfield_map and struct_type and member_name
                            and struct_type in bitfield_map
                            and bitfield_map[struct_type].get(member_name)):
                        # Bit-field – skip
                        pass
                    else:
                        _add_access("", line, col, end_line, expr, root_name, root_expr, struct_type, member_name, member_offset, is_write=_is_write_access(node, ancestors))
                for child in node.get("inner", []):
                    if isinstance(child, dict):
                        walk(child, ancestors + [node], current_in_return, current_in_function, is_in_assignment)
            elif kind == "UnaryOperator" and node.get("opcode") in {"*", "&"}:
                # Dereference or address-of on a shared pointer or variable
                if _node_has_shared_reference(node, ancestors) and not current_in_return and current_in_function and _is_source_node(node):
                    is_write = _is_write_access(node, ancestors)
                    rng = node.get("range", {})
                    begin = rng.get("begin", {})
                    end = rng.get("end", {})
                    offset = _get_loc_value(begin, "offset") or 0
                    line, col = offset_to_line_col(offset, source)
                    end_line = _get_enclosing_end_line(ancestors + [node], _get_loc_value(end, "line") if isinstance(_get_loc_value(end, "line"), int) else line)
                    expr = node_expression(node)
                    _add_access("", line, col, end_line, expr, _root_decl_name(node), _root_expression(node), is_write=is_write)
                subexpr = node.get("subExpr")
                if isinstance(subexpr, dict):
                    walk(subexpr, ancestors + [node], current_in_return, current_in_function, is_in_assignment)
            elif kind == "ArraySubscriptExpr":
                if _node_has_shared_reference(node, ancestors) and not current_in_return and current_in_function and _is_source_node(node):
                    is_write = _is_write_access(node, ancestors)
                    rng = node.get("range", {})
                    begin = rng.get("begin", {})
                    end = rng.get("end", {})
                    offset = _get_loc_value(begin, "offset") or 0
                    line, col = offset_to_line_col(offset, source)
                    end_line = _get_enclosing_end_line(ancestors + [node], _get_loc_value(end, "line") if isinstance(_get_loc_value(end, "line"), int) else line)
                    expr = node_expression(node)
                    _add_access("", line, col, end_line, expr, _root_decl_name(node), _root_expression(node), is_write=is_write)
                for child in node.get("inner", []):
                    if isinstance(child, dict):
                        walk(child, ancestors + [node], current_in_return, current_in_function, is_in_assignment)
            elif kind == "RecoveryExpr":
                # Clang error-recovery node: AST children are lost.
                # 仅当 temp_flush 未开启时在此做正则扫描 (temp_flush 开启时由源码级回退统一处理)。
                # temp_flush 关闭时也不扫描——用户不想要不确定的插桩。
                # 即: RecoveryExpr 正则扫描已完全由 temp_flush 源码回退取代。
                for child in node.get("inner", []):
                    if isinstance(child, dict):
                        walk(child, ancestors + [node], current_in_return, current_in_function, is_in_assignment)
            else:
                for child in node.get("inner", []):
                    walk(child, ancestors + [node], current_in_return, current_in_function, is_in_assignment)
        elif isinstance(node, list):
            for child in node:
                walk(child, ancestors, in_return_stmt, in_function, is_in_assignment)

    walk(ast, [], False, False, False)
    
    # Source-level fallback: scan for shared variable member accesses
    # that clang missed (types unresolvable). These use SIM_FLUSH_TEMP.
    if temp_flush:
        source_accesses = _find_global_var_accesses_from_source(source, shared_vars, shared_pointer_vars)
        # 去重: 跳过与 AST 路径(含 RecoveryExpr)已检测到的重复访问
        # 同一 line 或同一 end_line 的相同表达式视为重复
        ast_exprs = {(a.line, a.expression) for a in accesses}
        ast_endline_exprs = {(a.end_line, a.expression) for a in accesses}
        for sa in source_accesses:
            if ((sa.line, sa.expression) not in ast_exprs
                    and (sa.end_line, sa.expression) not in ast_endline_exprs):
                accesses.append(sa)
    
    return accesses


def _node_contains(target: dict[str, Any], node: Any) -> bool:
    if target is node:
        return True
    if isinstance(target, dict):
        for child in target.get("inner", []):
            if isinstance(child, dict) and _node_contains(child, node):
                return True
    elif isinstance(target, list):
        for child in target:
            if isinstance(child, dict) and _node_contains(child, node):
                return True
    return False


def _is_write_access(node: dict[str, Any], ancestors: list[dict[str, Any]]) -> bool:
    # Determine whether this node is in a write position.
    for ancestor in reversed(ancestors):
        kind = ancestor.get("kind")
        if kind == "BinaryOperator":
            opcode = ancestor.get("opcode", "")
            if opcode in {"=", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "^=", "|="}:
                children = [c for c in ancestor.get("inner", []) if isinstance(c, dict)]
                if children and _node_contains(children[0], node):
                    return True
        elif kind == "CompoundAssignOperator":
            children = [c for c in ancestor.get("inner", []) if isinstance(c, dict)]
            if children and _node_contains(children[0], node):
                return True
        elif kind == "UnaryOperator" and ancestor.get("opcode") in {"++", "--"}:
            subexpr = ancestor.get("subExpr")
            if subexpr is not None and _node_contains(subexpr, node):
                return True
        elif kind == "CXXOperatorCallExpr":
            children = [c for c in ancestor.get("inner", []) if isinstance(c, dict)]
            if len(children) > 1 and _node_contains(children[1], node):
                return True
        elif kind == "CallExpr":
            # Function call arguments are not writes unless they are operator calls.
            return False
    return False


def _get_struct_member_offset(struct_type: str, member_name: str) -> Optional[int]:
    """Get the byte offset of a struct member. Requires manual definition."""
    struct_layouts = {
        "SharedState": {
            "x": 0,
            "y": 8,
            "z": 16,
            "w": 60,
            "u": 64,
        },
    }
    if struct_type in struct_layouts:
        return struct_layouts[struct_type].get(member_name)
    return None


def _find_global_var_accesses_from_source(source: str, shared_var_names: set[str], shared_pointer_vars: set[str]) -> list[VariableAccess]:
    """Source-level fallback: scan for shared variable member accesses
    that clang missed due to unresolvable types. Uses SIM_FLUSH_TEMP."""
    accesses: list[VariableAccess] = []
    lines = source.splitlines()
    
    all_vars = shared_var_names | shared_pointer_vars
    
    # Patterns for detecting writes: var->member [=| compound_assign] value
    # 注意: 末尾的 =(?!=) 使用负前瞻排除 == (比较运算符)
    _ASSIGN_RE = re.compile(r'^\s*(\w+(?:(?:->|\.)\w+)+)\s*(\|=|\+=|-=|\*=|/=|%=|<<=|>>=|&=|\^=|=(?!=))')
    
    for var_name in sorted(all_vars, key=len, reverse=True):
        for line_idx, line in enumerate(lines, start=1):
            # Check for write: var->member = ... or var->member |= ...
            m = _ASSIGN_RE.match(line)
            if m:
                lhs = m.group(1).strip()
                # Verify LHS starts with a known shared var name
                if lhs.startswith(var_name) and (lhs == var_name or lhs[len(var_name)] in ('-', '.')):
                    col = m.start(1) + 1  # column of the LHS expression
                    accesses.append(VariableAccess(
                        name=var_name, line=line_idx, column=col,
                        end_line=line_idx, is_write=True,
                        expression=lhs, root_name=var_name,
                        root_expression=lhs, use_temp=True,
                    ))
            
            # Find all ->member chains (reads) starting with the var name.
            # Skip matches where the var name is preceded by '->' or '.'
            # (e.g. info->str->len should not match str->len)
            for m in re.finditer(r'\b' + re.escape(var_name) + r'((?:(?:->|\.)\s*\w+)+)', line):
                # Check that the var name is not a member of another expression
                if m.start() > 0 and line[m.start() - 1] in ('>', '.'):
                    continue
                expr = var_name + m.group(1)
                col = m.start() + 1
                # Check if this is already captured as a write
                already = any(a.expression == expr and a.line == line_idx and a.is_write for a in accesses)
                if not already:
                    accesses.append(VariableAccess(
                        name=var_name, line=line_idx, column=col,
                        end_line=line_idx, is_write=False,
                        expression=expr, root_name=var_name,
                        root_expression=expr, use_temp=True,
                    ))

    return accesses


def _expand_inter_ptr_prefixes(expr: str) -> list[str]:
    """For 'a->b->c->d', return ['a->b', 'a->b->c'] (intermediate pointer prefixes)."""
    positions = []
    i = 0
    while i < len(expr):
        if expr[i:i+2] == '->':
            positions.append(i)
            i += 2
        else:
            i += 1
    if len(positions) < 2:
        return []
    result = []
    for idx in range(len(positions) - 1):
        end = positions[idx] + 2
        while end < len(expr) and (expr[end].isalnum() or expr[end] == '_'):
            end += 1
        result.append(expr[:end])
    return result


def instrument_code(source: str, filename: str = "<text>", path: Optional[Path] = None, include_paths: Optional[list[str]] = None, conservative_ptr: bool = False, post_if_flush: bool = False, bitfield_map_path: Optional[str] = None, struct_header_path: Optional[str] = None, temp_flush: bool = False, inter_ptr_flush: bool = False) -> str:
    # Find shared variables
    variables = find_shared_variables_in_text(source, filename, path, include_paths=include_paths)
    shared_var_names = {var.name for var in variables}

    # Load bitfield map (struct -> member -> is_bitfield)
    bitfield_map: dict[str, dict[str, bool]] = {}
    if bitfield_map_path:
        try:
            with open(bitfield_map_path, "r", encoding="utf-8") as f:
                bitfield_map = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    # Get AST
    ast = _run_clang_ast_dump(source, filename, path, include_paths=include_paths, struct_header=struct_header_path)
    if ast is None:
        # Fallback: no instrumentation
        return source

    # Find all accesses (includes source-level fallback for unresolvable types)
    accesses = _find_variable_accesses_from_ast(ast, shared_var_names, source, conservative_ptr=conservative_ptr, bitfield_map=bitfield_map, temp_flush=temp_flush)

    # Remove duplicate accesses by line/column/expression
    unique_keys = set()
    unique_accesses: list[VariableAccess] = []
    for access in accesses:
        key = (access.line, access.column, access.expression)
        if key not in unique_keys:
            unique_keys.add(key)
            unique_accesses.append(access)

    # Post-filter: remove bit-field member accesses (cannot take address)
    if bitfield_map:
        filtered: list[VariableAccess] = []
        for access in unique_accesses:
            expr = access.expression
            # Check if this is a member access on a known bit-field struct
            is_bf = False
            for stype, members in bitfield_map.items():
                for mname, is_bitfield in members.items():
                    if is_bitfield and (expr.endswith("." + mname) or expr.endswith("->" + mname)):
                        # Verify it's really this struct type by checking the root/type
                        if access.struct_type == stype or access.root_name:
                            is_bf = True
                            break
                if is_bf:
                    break
            if not is_bf:
                filtered.append(access)
        unique_accesses = filtered

    # Filter out raw struct accesses (no struct_type/offset) when an annotated
    # one exists at the same (line, column, root_name).
    annotated_positions = {(a.line, a.column, a.root_name) for a in unique_accesses if a.struct_type is not None}
    unique_accesses = [a for a in unique_accesses
                       if a.struct_type is not None or (a.line, a.column, a.root_name) not in annotated_positions]

    # Keep all accesses (both reads and writes).
    # unique_accesses = [access for access in unique_accesses if access.is_write]

    # Filter out garbage expressions (from RecoveryExpr with large ranges)
    unique_accesses = [a for a in unique_accesses
                       if a.expression.strip()
                       and not a.expression.startswith(('(', '{', '}', ';'))
                       and ' ' not in a.expression.strip()
                       and len(a.expression.strip()) >= 2]

    # Remove temp accesses that duplicate existing non-temp ones on the SAME line
    # 或者 end_line 相同 (即插入位置相同, temp 版本冗余)
    non_temp_line_exprs = {(a.line, a.expression) for a in unique_accesses if not a.use_temp}
    non_temp_endline_exprs = {(a.end_line, a.expression) for a in unique_accesses if not a.use_temp}
    unique_accesses = [a for a in unique_accesses
                       if not a.use_temp
                       or ((a.line, a.expression) not in non_temp_line_exprs
                           and (a.end_line, a.expression) not in non_temp_endline_exprs)]

    # Remove partial -> chain expressions when a longer full chain exists at the same position.
    # e.g. ctx->np_st->snd_buf_anomaly supersedes np_st->snd_buf_anomaly and ctx->np_st.
    pos_group: dict[tuple[int, int, int], list[VariableAccess]] = {}
    for a in unique_accesses:
        key = (a.line, a.column, a.end_line)
        pos_group.setdefault(key, []).append(a)
    filtered = []
    for accesses_at_pos in pos_group.values():
        if len(accesses_at_pos) <= 1:
            filtered.extend(accesses_at_pos)
            continue
        # Remove expressions that are a suffix of a longer one at the same position.
        # e.g. np_st->snd_buf_anomaly is a suffix of ctx->np_st->snd_buf_anomaly → remove shorter
        # But tp->client is NOT a suffix of tp->sess_ctx->sess_id → keep both
        keep = []
        for a in accesses_at_pos:
            is_suffix = any(
                a is not other and other.expression.endswith(a.expression)
                for other in accesses_at_pos
            )
            if not is_suffix:
                keep.append(a)
        if keep:
            filtered.extend(keep)
        else:
            # If all are suffixes of each other (shouldn't happen), keep longest
            filtered.append(max(accesses_at_pos, key=lambda a: len(a.expression)))
    unique_accesses = filtered

    # Merge accesses that share the same root shared object at the same insertion point, considering cacheline boundaries.
    merged_by_root: dict[str, list[VariableAccess]] = {}
    for access in sorted(unique_accesses, key=lambda a: (a.end_line, a.line, a.column)):
        root_key = access.root_name or access.root_expression
        if root_key not in merged_by_root:
            merged_by_root[root_key] = []
        merged_by_root[root_key].append(access)

    final_accesses: list[VariableAccess] = []
    for root_key, accesses_list in merged_by_root.items():
        # --- array cacheline merge ---
        # Merge array accesses that fall into the same cacheline on the same statement.
        if any("[" in a.expression and "]" in a.expression for a in accesses_list):
            _CACHELINE_ELEMS = 16  # 16 ints = 64 bytes
            array_groups: dict[tuple[int, int], VariableAccess] = {}
            for access in accesses_list:
                expr = access.expression
                if "[" in expr and "]" in expr:
                    try:
                        idx_str = expr.split("[")[1].split("]")[0]
                        idx = int(idx_str)
                    except ValueError:
                        continue
                    cacheline_base = (idx // _CACHELINE_ELEMS) * _CACHELINE_ELEMS
                    group_key = (access.end_line, cacheline_base)
                    if group_key not in array_groups:
                        # Collect all accesses to this (end_line, cacheline) for correct is_write
                        group_writes = any(
                            a.is_write for a in accesses_list
                            if "[" in a.expression and "]" in a.expression
                            and a.end_line == access.end_line
                        )
                        array_groups[group_key] = VariableAccess(
                            name=access.name,
                            line=access.line,
                            column=access.column,
                            end_line=access.end_line,
                            is_write=group_writes,
                            expression=f"{root_key}[{cacheline_base}]",
                            root_name=access.root_name,
                            root_expression=access.root_expression,
                        )
            final_accesses.extend(array_groups.values())
            # Also keep any non-indexed accesses (shouldn't happen for arrays, but be safe)
            for access in accesses_list:
                if not ("[" in access.expression and "]" in access.expression):
                    final_accesses.append(access)

        # --- struct cacheline merge ---
        # Merge struct member accesses that fall into the same 64B cacheline on the same statement.
        elif accesses_list and any(a.struct_type for a in accesses_list):
            struct_type = next((a.struct_type for a in accesses_list if a.struct_type), None)
            cacheline_merged: dict[tuple[int, int], VariableAccess] = {}
            for access in accesses_list:
                if access.member_offset is not None:
                    cacheline_base = (access.member_offset // 64) * 64
                    group_key = (access.end_line, cacheline_base)
                    if group_key not in cacheline_merged:
                        # Collect all annotated accesses to this (end_line, cacheline)
                        candidates = [a for a in accesses_list
                                      if a.member_offset is not None
                                      and a.end_line == access.end_line
                                      and (a.member_offset // 64) * 64 == cacheline_base]
                        # Prefer a write access as the representative expression
                        best = max(candidates, key=lambda a: (a.is_write, a.line, a.column))
                        cacheline_merged[group_key] = VariableAccess(
                            name=best.name,
                            line=best.line,
                            column=best.column,
                            end_line=best.end_line,
                            is_write=any(a.is_write for a in candidates),
                            expression=best.expression,
                            root_name=best.root_name,
                            root_expression=best.root_expression,
                            struct_type=struct_type,
                            member_name=best.member_name,
                            member_offset=cacheline_base,
                        )
            final_accesses.extend(cacheline_merged.values())
            # Also keep raw accesses without offset info (shouldn't remain after dedup, but be safe)
            for access in accesses_list:
                if access.member_offset is None:
                    final_accesses.append(access)

        # --- scalar / fallback: keep all accesses individually ---
        else:
            final_accesses.extend(accesses_list)

    # Generate intermediate pointer flushes for chained -> accesses
    if inter_ptr_flush:
        existing_endline_exprs = {(a.end_line, a.expression) for a in final_accesses}
        inter_ptr_accesses: list[VariableAccess] = []
        for access in final_accesses:
            for prefix in _expand_inter_ptr_prefixes(access.expression):
                if (access.end_line, prefix) not in existing_endline_exprs:
                    inter_ptr_accesses.append(VariableAccess(
                        name=access.name, line=access.line, column=access.column,
                        end_line=access.end_line, is_write=False,
                        expression=prefix, root_name=access.root_name,
                        root_expression=access.root_expression, use_inter_ptr=True,
                    ))
                    existing_endline_exprs.add((access.end_line, prefix))
        final_accesses.extend(inter_ptr_accesses)

    accesses = sorted(final_accesses, key=lambda a: (a.line, a.column), reverse=True)

    lines = source.splitlines()
    inserts = []
    for access in accesses:
        if 1 <= access.end_line <= len(lines):
            if access.use_inter_ptr:
                flush_call = f"SIM_FLUSH_INTER_PTR(&({access.expression}));"
            elif access.use_temp:
                flush_call = f"SIM_FLUSH_TEMP(&({access.expression}));"
            else:
                flush_call = f"SIM_FLUSH(&({access.expression}));"
            inserts.append((access.end_line, flush_call))
    inserts.sort(key=lambda x: x[0], reverse=True)
    for line_num, flush in inserts:
        insert_index = max(0, min(line_num, len(lines)))
        prev_line = lines[insert_index - 1] if insert_index > 0 else ""
        next_line = lines[insert_index] if insert_index < len(lines) else ""
        prev_indent = prev_line[: len(prev_line) - len(prev_line.lstrip(" "))]
        next_indent = next_line[: len(next_line) - len(next_line.lstrip(" "))]

        # Detect if/for/while whose body is a bare statement (no braces)
        #
        # 反向扫描, 从当前插入点向上查找控制流语句 (if/for/while)。
        # 中间遇到不以 && || ( , ) 结尾的非空行 → 不在任何控制流范围内, 放弃。
        _CTRL_RE = re.compile(r'(?:if|for|while)\s*\(')
        ctrl_idx = insert_index - 1
        while ctrl_idx >= 0:
            stripped = lines[ctrl_idx].strip()
            if _CTRL_RE.search(stripped):
                break
            # 以 && || ( , ) 结尾 → 可能是跨行表达式/条件的中间行, 继续向上
            if stripped and not stripped.endswith(('&&', '||', '(', ',', ')')):
                ctrl_idx = -1
                break
            ctrl_idx -= 1
        wrapped_bare_body = False
        # 判断是否需要为"裸函数体"(无花括号的单行 body) 自动包裹花括号。
        # 必须同时满足:
        # 1. ctrl_idx >= 0  → 找到了控制流语句
        # 2. if 行不以 '{' 结尾 → 不是 K&R 风格
        # 3. if 行不以 '&&' / '||' 结尾 → 条件表达式完整, 下一行是函数体。
        #    ⚠ 如果以 &&/|| 结尾, 条件跨行了, 下一行是条件续行, 不能当裸 body!
        # 4. prev_line 不是单独的 "{"
        # 5. next_line 不是 "{" 或 "}"
        if (ctrl_idx >= 0
                and not lines[ctrl_idx].rstrip().endswith('{')
                and not lines[ctrl_idx].rstrip().endswith(('&&', '||'))
                and prev_line.strip() != "{"
                and next_line.strip() not in ("{", "}")):
            # Wrap the bare body in braces and put the flush inside.
            wrapped_bare_body = True
            ctrl_indent = lines[ctrl_idx][: len(lines[ctrl_idx]) - len(lines[ctrl_idx].lstrip())]
            body_indent = ctrl_indent + "    "
            body_line = lines.pop(insert_index)
            trailing_flushes: list[str] = []
            while (insert_index < len(lines)
                   and lines[insert_index].strip().startswith("SIM_FLUSH")):
                trailing_flushes.append(lines.pop(insert_index))
            for tf in reversed(trailing_flushes):
                lines.insert(insert_index, body_indent + tf.lstrip())
            lines.insert(insert_index, body_indent + body_line.lstrip())
            lines.insert(insert_index, body_indent + flush)
            lines.insert(insert_index, ctrl_indent + "{")
            close_pos = insert_index + 3 + len(trailing_flushes)
            lines.insert(close_pos, ctrl_indent + "}")
            continue

        if not wrapped_bare_body:
            # --- 跨行表达式/条件续行处理 ---
            # 上一行以逗号或左括号结尾 → 函数调用/表达式续行, 跳过
            advanced_by_continuation = False
            while (insert_index < len(lines)
                   and insert_index > 0
                   and lines[insert_index - 1].rstrip().endswith((',', '('))
                   and insert_index < len(lines)
                   and lines[insert_index].strip()
                   and not lines[insert_index].strip().startswith(('{', '}', 'if', 'for', 'while', 'SIM_FLUSH'))):
                insert_index += 1
                advanced_by_continuation = True
            # 同理, 跳过 && / || 续行。处理多行 if 条件:
            #   if (cond1 &&
            #       cond2 &&        ← 以 && 结尾, 下一行仍是条件
            #       cond3) {        ← 条件结束, flush 应插在 { 之后
            # 不跳过的话 SIM_FLUSH 会插入到 cond2 和 cond3 之间, 破坏语法。
            while (insert_index < len(lines)
                   and insert_index > 0
                   and lines[insert_index - 1].rstrip().endswith(('&&', '||'))
                   and insert_index < len(lines)
                   and lines[insert_index].strip()
                   and not lines[insert_index].strip().startswith(('{', '}', 'if', 'for', 'while', 'SIM_FLUSH'))):
                insert_index += 1
                advanced_by_continuation = True
            next_line = lines[insert_index] if insert_index < len(lines) else ""
            next_indent = next_line[: len(next_line) - len(next_line.lstrip(" "))]
            # 下一行是单独的花括号 → flush 插入到花括号内部
            # 下一行形如 "... ) {" (多行条件末尾) → 同样处理, flush 放 { 之后
            # 注意: 仅当前面经过了续行跳过 (说明处于多行条件上下文) 时,
            #       才将 ") {" 模式视为条件末尾。否则 "if (...) {" 是独立语句,
            #       SIM_FLUSH 应插在它之前而非内部。
            if next_line.strip() == "{":
                insert_index += 1
                indent = next_indent + "    "
            elif advanced_by_continuation and next_line.strip().endswith('{') and ')' in next_line:
                insert_index += 1
                indent = next_indent + "    "
            elif next_line.strip().startswith(("}", "else", "case", "default")):
                indent = prev_indent
            elif next_line and next_indent != prev_indent:
                indent = next_indent
            else:
                indent = prev_indent
            lines.insert(insert_index, indent + flush)

    # --- Post-processing: else / else-if branch instrumentation ---
    # After all flushes are inserted, scan for if-conditions whose flush
    # should also appear in every else-if and else branch of the chain.
    _ELSE_RE = re.compile(r'\belse\b')
    _IF_COND_FLUSH_RE = re.compile(
        r'^(\s*)if\s*\(.+\)\s*\{?\s*$'
    )
    i = 0
    while i < len(lines):
        m = _IF_COND_FLUSH_RE.match(lines[i])
        if m:
            # Find the SIM_FLUSH that belongs to this if condition.
            # It is the first SIM_FLUSH inside this if block.
            if_open = lines[i]
            ctrl_indent = m.group(1)
            depth = 1 if if_open.rstrip().endswith('{') else 0
            if_scan = i + 1
            cond_flush = ""
            while if_scan < len(lines) and depth >= 0:
                s = lines[if_scan].strip()
                if s.startswith("SIM_FLUSH"):
                    cond_flush = lines[if_scan].lstrip()
                    break
                for ch in s:
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                if_scan += 1
            # Find the matching closing } of this if block
            if cond_flush:
                depth = 1
                close_scan = if_scan + 1
                while close_scan < len(lines):
                    s = lines[close_scan].strip()
                    for ch in s:
                        if ch == '{':
                            depth += 1
                        elif ch == '}':
                            depth -= 1
                            if depth == 0:
                                break
                    if depth <= 0:
                        break
                    close_scan += 1
                # If the closing } also contains 'else', process it from here;
                # otherwise advance to the next line.
                if close_scan < len(lines) and not _ELSE_RE.search(lines[close_scan]):
                    close_scan += 1
                # Process else / else-if chain
                while close_scan < len(lines):
                    s = lines[close_scan].strip()
                    if _ELSE_RE.search(lines[close_scan]):
                        else_line = lines[close_scan]
                        else_ind = else_line[: len(else_line) - len(else_line.lstrip())]
                        body_ind = else_ind + "    "
                        # Find body start and skip if already instrumented
                        if else_line.rstrip().endswith('{'):
                            bs = close_scan + 1
                        elif close_scan + 1 < len(lines) and lines[close_scan + 1].strip() == '{':
                            bs = close_scan + 2
                        else:
                            bs = close_scan + 1
                        already_has = (bs < len(lines) and lines[bs].strip().startswith("SIM_FLUSH"))
                        if already_has:
                            # Body already has condition flush; just find closing } and continue
                            depth = 1
                            cs = bs + 1
                            while cs < len(lines):
                                for ch in lines[cs].strip():
                                    if ch == '{': depth += 1
                                    elif ch == '}':
                                        depth -= 1
                                        if depth == 0: break
                                if depth <= 0: break
                                cs += 1
                            close_scan = cs
                            if close_scan < len(lines) and not _ELSE_RE.search(lines[close_scan]):
                                close_scan += 1
                            continue
                        if else_line.rstrip().endswith('{') or (
                            close_scan + 1 < len(lines) and lines[close_scan + 1].strip() == '{'
                        ):
                            # Braced body — already handled above via already_has
                            pass
                        else:
                            # Bare else/else-if body — need to wrap
                            bs = close_scan + 1
                            if bs < len(lines) and lines[bs].strip():
                                body = lines.pop(bs)
                                trailing = []
                                while (bs < len(lines)
                                       and lines[bs].strip().startswith("SIM_FLUSH")):
                                    trailing.append(lines.pop(bs))
                                # Insert in reverse order so final order is:
                                # {, cond_flush, body, trailing_flush..., }
                                # Insert trailing first (they go at end)
                                for tf in reversed(trailing):
                                    lines.insert(bs, body_ind + tf.lstrip())
                                lines.insert(bs, body_ind + body.lstrip())
                                lines.insert(bs, body_ind + cond_flush)
                                lines.insert(bs, else_ind + '{')
                                close_p = bs + 3 + len(trailing)
                                lines.insert(close_p, else_ind + '}')
                                if else_line.rstrip().endswith('{'):
                                    lines[close_scan] = else_line.rstrip()[:-1].rstrip()
                                continue
                        if bs < len(lines):
                            lines.insert(bs, body_ind + cond_flush)
                        # Find closing } of this else block
                        depth = 1
                        cs = bs + 1
                        while cs < len(lines):
                            for ch in lines[cs].strip():
                                if ch == '{':
                                    depth += 1
                                elif ch == '}':
                                    depth -= 1
                                    if depth == 0:
                                        break
                            if depth <= 0:
                                break
                            cs += 1
                        close_scan = cs
                    else:
                        break
                    close_scan += 1
        i += 1

    # --- Post-processing: post-if-block flush ---
    # 将 if 条件中共享变量的 SIM_FLUSH 移动到整个 if/else-if/else 链的
    # 闭合 } 之后, 而不是留在函数体内部。
    if post_if_flush:
        # 匹配 if 行开头。不要求 ) 与 if 在同一行 (以支持跨行条件)。
        _IF_RE = re.compile(r'^(\s*)if\s*\(.+')
        i = 0
        while i < len(lines):
            m = _IF_RE.match(lines[i])
            if m:
                ctrl_indent = m.group(1)
                if_open = lines[i]
                depth = 1 if if_open.rstrip().endswith('{') else 0
                if_scan = i + 1
                cond_flush = ""
                # 对于跨行 if 条件 (如 if (a &&\n    b) {), { 不在 if 行上。
                # 向前扫描找到 { 的位置, 正确设置 depth。
                if depth == 0:
                    while if_scan < len(lines):
                        s = lines[if_scan].strip()
                        if s == "{":
                            # 独立的 { 行
                            depth = 1
                            if_scan += 1
                            break
                        if s.endswith('{') and ')' in s:
                            # 条件末尾的 ") {" — 同一行上条件结束且带花括号
                            depth = 1
                            if_scan += 1
                            break
                        if_scan += 1
                while if_scan < len(lines) and depth > 0:
                    s = lines[if_scan].strip()
                    if s.startswith("SIM_FLUSH"):
                        cond_flush = lines[if_scan].lstrip()
                        if_scan += 1
                        # 收集所有连续的 SIM_FLUSH 行 (都属于条件中的共享变量访问)
                        while if_scan < len(lines):
                            ns = lines[if_scan].strip()
                            if ns.startswith("SIM_FLUSH"):
                                cond_flush += "\n" + lines[if_scan].lstrip()
                                if_scan += 1
                            elif ns == "":
                                if_scan += 1
                            else:
                                break
                        break
                    elif s != "" and not s.startswith("//") and not s.startswith("/*") and not s.startswith("*") and s != "{":
                        # Non-empty, non-comment, non-SIM_FLUSH line → this is body code,
                        # not condition SIM_FLUSH. Stop looking.
                        break
                    for ch in s:
                        if ch == '{':
                            depth += 1
                        elif ch == '}':
                            depth -= 1
                    if_scan += 1
                if cond_flush:
                    # Trace the entire if/else-if/else chain to find the final closing }
                    depth = 1
                    cs = if_scan + 1
                    while cs < len(lines):
                        for ch in lines[cs].strip():
                            if ch == '{':
                                depth += 1
                            elif ch == '}':
                                depth -= 1
                                if depth == 0:
                                    break
                        if depth <= 0:
                            break
                        cs += 1
                    # cs now points to the closing } of the if-block.
                    # Check for else/else-if chain and advance past it.
                    _ELSE_RE2 = re.compile(r'\belse\b')
                    while cs < len(lines):
                        if _ELSE_RE2.search(lines[cs]):
                            # else on same line as } (e.g. "} else") or standalone
                            pass
                        elif cs + 1 < len(lines) and _ELSE_RE2.search(lines[cs + 1]):
                            cs += 1
                        else:
                            break
                        # Trace this else/else-if block to its closing }
                        depth = lines[cs].count('{')  # { on the else line itself
                        cs += 1
                        # If the next line is a standalone {, count and skip it
                        if cs < len(lines) and lines[cs].strip() == "{":
                            depth += 1
                            cs += 1
                        # If no explicit { yet, scan forward for { on condition
                        # continuation lines (e.g. "} else if (a &&\n    b) {").
                        # If { found, skip past it; otherwise this is a bare body.
                        if depth == 0:
                            found_brace = False
                            look = cs
                            while look < len(lines):
                                s = lines[look].strip()
                                # Standalone { or ") {" — brace found
                                if s == "{" or ('{' in s and ')' in s):
                                    depth = 1
                                    cs = look + 1
                                    found_brace = True
                                    break
                                # Lines ending with &&/|| are condition continuations
                                # Other non-empty, non-comment lines → body, stop
                                if s and not s.startswith(('//', '/*')) and not s.endswith(('&&', '||')):
                                    break
                                look += 1
                            if not found_brace:
                                depth = 1
                        while cs < len(lines):
                            for ch in lines[cs].strip():
                                if ch == '{':
                                    depth += 1
                                elif ch == '}':
                                    depth -= 1
                                    if depth == 0:
                                        break
                            if depth <= 0:
                                break
                            cs += 1
                    # cs now points to the closing } (either if block or function).
                    # Insert after the } (or before if at very end of file).
                    # cond_flush 可能包含多行 (用 \n 分隔), 逐行插入
                    flush_lines = cond_flush.split("\n")
                    insert_pos = cs + 1 if cs + 1 < len(lines) else cs
                    for fl in flush_lines:
                        lines.insert(insert_pos, ctrl_indent + fl)
                        insert_pos += 1
            i += 1

    # Ensure sim_mem.h is included
    _SIM_MEM_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]sim_mem\.h[>"]')
    if not any(_SIM_MEM_INCLUDE_RE.match(line) for line in lines):
        lines.insert(0, '#include "sim_mem.h"')

    return "\n".join(lines)


def _iter_source_files(paths: Iterable[str], recursive: bool = True) -> Iterator[Path]:
    for path in paths:
        p = Path(path)
        if p.is_dir():
            if recursive:
                yield from (f for f in p.rglob("*") if f.suffix.lower() in SUPPORTED_EXTENSIONS)
            else:
                yield from (f for f in p.iterdir() if f.suffix.lower() in SUPPORTED_EXTENSIONS)
        elif p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
            yield p


def find_shared_variables(paths: Iterable[str], recursive: bool = True) -> List[SharedVariable]:
    variables: list[SharedVariable] = []
    for path in _iter_source_files(paths, recursive=recursive):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        variables.extend(find_shared_variables_in_text(text, str(path), path=path))
    return variables


def dump_shared_variables(variables: List[SharedVariable]) -> str:
    return json.dumps([
        {
            "name": var.name,
            "kind": var.kind,
            "file": var.file,
            "line": var.line,
            "declaration": var.declaration,
        }
        for var in variables
    ], indent=2)
