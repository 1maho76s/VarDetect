from __future__ import annotations

import re
from typing import Any, List, Optional

from .models import VariableAccess
from .source_fallback import _find_global_var_accesses_from_source


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
            return False
    return False


def _get_struct_member_offset(struct_type: str, member_name: str) -> Optional[int]:
    struct_layouts = {
        "SharedState": {"x": 0, "y": 8, "z": 16, "w": 60, "u": 64},
    }
    if struct_type in struct_layouts:
        return struct_layouts[struct_type].get(member_name)
    return None


def _find_variable_accesses_from_ast(ast: dict[str, Any], shared_vars: set[str], source: str, conservative_ptr: bool = False, bitfield_map: Optional[dict[str, dict[str, bool]]] = None, temp_flush: bool = False, engine: str = "tokenize") -> List[VariableAccess]:
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

    source_lines = source.splitlines()

    def _add_access(name: str, line: int, col: int, end_line: int, expr: str, root_name: str, root_expression: str, struct_type: Optional[str] = None, member_name: Optional[str] = None, member_offset: Optional[int] = None, is_write: bool = False, use_temp: bool = False) -> None:
        if line <= 0 or end_line <= 0 or not expr.strip():
            return
        # end_line 校验: 如果 end_line 所在行没有 ';' 也不以 '{' 或 '}' 结尾,
        # 向后找到 ';' 所在行, 确保 flush 插在完整语句之后。
        # 以 '{' 结尾的行是控制流语句 (if/for/while), 不需要 ';'。
        if end_line <= len(source_lines):
            end_stripped = source_lines[end_line - 1].rstrip()
            if ';' not in source_lines[end_line - 1] and not end_stripped.endswith(('{', '}')):
                for fwd in range(end_line, len(source_lines)):
                    if ';' in source_lines[fwd]:
                        end_line = fwd + 1
                        break
                    if source_lines[fwd].strip() in ('{', '}'):
                        break
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
        source_accesses = _find_global_var_accesses_from_source(source, shared_vars, shared_pointer_vars, engine=engine)
        # 去重: 跳过与 AST 路径(含 RecoveryExpr)已检测到的重复访问
        # 同一 line 或同一 end_line 的相同表达式视为重复
        ast_exprs = {(a.line, a.expression) for a in accesses}
        ast_endline_exprs = {(a.end_line, a.expression) for a in accesses}
        for sa in source_accesses:
            if ((sa.line, sa.expression) not in ast_exprs
                    and (sa.end_line, sa.expression) not in ast_endline_exprs):
                accesses.append(sa)
    
    return accesses


