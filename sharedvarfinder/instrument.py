from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, List, Optional

from .models import SharedVariable, VariableAccess
from .clang_ast import _run_clang_ast_dump
from .shared_vars import find_shared_variables_in_text
from .access_detect import _find_variable_accesses_from_ast


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


def instrument_code(source: str, filename: str = "<text>", path: Optional[Path] = None, include_paths: Optional[list[str]] = None, conservative_ptr: bool = False, post_if_flush: bool = False, bitfield_map_path: Optional[str] = None, struct_header_path: Optional[str] = None, temp_flush: bool = False, inter_ptr_flush: bool = False, engine: str = "tokenize") -> str:
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
    accesses = _find_variable_accesses_from_ast(ast, shared_var_names, source, conservative_ptr=conservative_ptr, bitfield_map=bitfield_map, temp_flush=temp_flush, engine=engine)

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
        # 3. 从 ctrl_idx 到 insert_index 括号已闭合 → 条件表达式完整
        # 4. prev_line 不是单独的 "{"
        # 5. next_line 不是 "{" 或 "}"
        ctrl_paren_depth = 0
        if ctrl_idx >= 0:
            for scan_idx in range(ctrl_idx, min(insert_index, len(lines))):
                for ch in lines[scan_idx]:
                    if ch == '(':
                        ctrl_paren_depth += 1
                    elif ch == ')':
                        ctrl_paren_depth -= 1
        if (ctrl_idx >= 0
                and not lines[ctrl_idx].rstrip().endswith('{')
                and ctrl_paren_depth <= 0
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
            # --- 括号/花括号平衡检测 ---
            # 从语句起始行到 insert_index, 检测 () 和 {} 是否闭合。
            # 未闭合说明仍在多行表达式(函数调用、宏调用、if条件、初始化器)中,
            # 继续向下直到平衡, 避免 flush 插入到语句中间。
            advanced_by_continuation = False
            # 向上找语句起始行: 以 ; 或独立 { } 结尾的行, 或函数/控制流开头
            stmt_start = insert_index - 1
            for back_idx in range(insert_index - 2, -1, -1):
                s = lines[back_idx].rstrip()
                if s.endswith((';', '{', '}')):
                    stmt_start = back_idx + 1
                    break
            paren_depth = 0
            for scan_idx in range(stmt_start, min(insert_index, len(lines))):
                for ch in lines[scan_idx]:
                    if ch == '(':
                        paren_depth += 1
                    elif ch == ')':
                        paren_depth -= 1
            if paren_depth > 0:
                while insert_index < len(lines):
                    scan_line = lines[insert_index]
                    for ch in scan_line:
                        if ch == '(':
                            paren_depth += 1
                        elif ch == ')':
                            paren_depth -= 1
                    insert_index += 1
                    advanced_by_continuation = True
                    if paren_depth <= 0 or ';' in scan_line:
                        break
            # 花括号平衡检测 (仅针对初始化器 "= { ... }")
            # 控制流的 { 是合法插入位置, 不需要跳过
            brace_depth = 0
            in_initializer = False
            for scan_idx in range(insert_index - 1, -1, -1):
                for ch in lines[scan_idx]:
                    if ch == '{':
                        brace_depth += 1
                    elif ch == '}':
                        brace_depth -= 1
                if brace_depth > 0:
                    if re.search(r'=\s*\{', lines[scan_idx]):
                        in_initializer = True
                    break
            if brace_depth > 0 and in_initializer:
                while insert_index < len(lines):
                    scan_line = lines[insert_index]
                    for ch in scan_line:
                        if ch == '{':
                            brace_depth += 1
                        elif ch == '}':
                            brace_depth -= 1
                    insert_index += 1
                    advanced_by_continuation = True
                    if brace_depth <= 0 or scan_line.rstrip().endswith('};'):
                        break
            next_line = lines[insert_index] if insert_index < len(lines) else ""
            next_indent = next_line[: len(next_line) - len(next_line.lstrip(" "))]
            # Recalculate prev_line/prev_indent after potential advancement
            prev_line = lines[insert_index - 1] if insert_index > 0 else ""
            prev_indent = prev_line[: len(prev_line) - len(prev_line.lstrip(" "))]
            # If prev_line ends with "};" (initializer close), find the statement
            # that opened the initializer and use its indentation instead.
            if prev_line.rstrip().endswith('};') or prev_line.rstrip() == '};':
                for back_idx in range(insert_index - 2, -1, -1):
                    if re.search(r'=\s*\{', lines[back_idx]):
                        prev_indent = lines[back_idx][: len(lines[back_idx]) - len(lines[back_idx].lstrip(" "))]
                        break
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
                # If prev_line ends with '{', we're inserting inside a block body
                if prev_line.rstrip().endswith('{'):
                    indent = prev_indent + "    "
                else:
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
                    elif re.search(r'=\s*\{', lines[if_scan]):
                        # 跳过花括号初始化器块 (如 HpsPktInfo info = { ... };)
                        init_depth = 0
                        while if_scan < len(lines):
                            for ch in lines[if_scan]:
                                if ch == '{':
                                    init_depth += 1
                                elif ch == '}':
                                    init_depth -= 1
                            if_scan += 1
                            if init_depth <= 0:
                                break
                    elif s != "" and not s.startswith("//") and not s.startswith("/*") and not s.startswith("*") and s != "{":
                        # Non-empty, non-comment, non-SIM_FLUSH line → this is body code,
                        # not condition SIM_FLUSH. Stop looking.
                        break
                    else:
                        for ch in s:
                            if ch == '{':
                                depth += 1
                            elif ch == '}':
                                depth -= 1
                        if_scan += 1
                if cond_flush:
                    # Trace the entire if/else-if/else chain to find the final closing }
                    depth = 1
                    cs = if_scan
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


