from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional

from .models import VariableAccess


def _find_global_var_accesses_tokenize(source: str, shared_var_names: set[str], shared_pointer_vars: set[str]) -> list[VariableAccess]:
    """Source-level fallback: scan for shared variable member accesses
    that clang missed due to unresolvable types. Uses SIM_FLUSH_TEMP.

    基于词法分析而非纯正则: 先 tokenize 源码 (跳过字符串/注释内容),
    然后在 token 流上识别 var->member 链式访问。
    """
    accesses: list[VariableAccess] = []
    lines = source.splitlines()
    all_vars = shared_var_names | shared_pointer_vars

    # --- 词法 tokenize ---
    # Token types: IDENT, ARROW, DOT, ASSIGN_OP, OTHER, NEWLINE
    # 跳过: 字符串字面量内容, 字符字面量, 行注释, 块注释
    @dataclass
    class Token:
        kind: str       # IDENT, ARROW, DOT, ASSIGN_OP, SEMI, LBRACE, RBRACE, OTHER
        value: str
        line: int       # 1-indexed
        col: int        # 0-indexed position in line

    def tokenize(src: str) -> list[Token]:
        tokens: list[Token] = []
        i = 0
        line = 1
        col = 0
        n = len(src)
        while i < n:
            ch = src[i]
            # Newline
            if ch == '\n':
                line += 1
                col = 0
                i += 1
                continue
            # Block comment
            if ch == '/' and i + 1 < n and src[i + 1] == '*':
                i += 2
                col += 2
                while i < n:
                    if src[i] == '\n':
                        line += 1
                        col = 0
                    elif src[i] == '*' and i + 1 < n and src[i + 1] == '/':
                        i += 2
                        col += 2
                        break
                    else:
                        col += 1
                    i += 1
                continue
            # Line comment
            if ch == '/' and i + 1 < n and src[i + 1] == '/':
                while i < n and src[i] != '\n':
                    i += 1
                continue
            # String literal
            if ch == '"':
                i += 1
                col += 1
                while i < n and src[i] != '"':
                    if src[i] == '\\' and i + 1 < n:
                        i += 2
                        col += 2
                    elif src[i] == '\n':
                        line += 1
                        col = 0
                        i += 1
                    else:
                        i += 1
                        col += 1
                if i < n:
                    i += 1  # skip closing "
                    col += 1
                continue
            # Char literal
            if ch == "'":
                i += 1
                col += 1
                while i < n and src[i] != "'":
                    if src[i] == '\\' and i + 1 < n:
                        i += 2
                        col += 2
                    else:
                        i += 1
                        col += 1
                if i < n:
                    i += 1
                    col += 1
                continue
            # Whitespace (non-newline)
            if ch in (' ', '\t', '\r'):
                i += 1
                col += 1
                continue
            # Arrow operator ->
            if ch == '-' and i + 1 < n and src[i + 1] == '>':
                tokens.append(Token('ARROW', '->', line, col))
                i += 2
                col += 2
                continue
            # Dot
            if ch == '.':
                tokens.append(Token('DOT', '.', line, col))
                i += 1
                col += 1
                continue
            # Compound assignment operators
            if ch in ('|', '+', '-', '*', '/', '%', '&', '^') and i + 1 < n and src[i + 1] == '=':
                tokens.append(Token('ASSIGN_OP', ch + '=', line, col))
                i += 2
                col += 2
                continue
            # <<= >>=
            if ch in ('<', '>') and i + 2 < n and src[i + 1] == ch and src[i + 2] == '=':
                tokens.append(Token('ASSIGN_OP', ch + ch + '=', line, col))
                i += 3
                col += 3
                continue
            # = (but not ==)
            if ch == '=' and (i + 1 >= n or src[i + 1] != '='):
                tokens.append(Token('ASSIGN_OP', '=', line, col))
                i += 1
                col += 1
                continue
            # Semicolon
            if ch == ';':
                tokens.append(Token('SEMI', ';', line, col))
                i += 1
                col += 1
                continue
            # Braces
            if ch == '{':
                tokens.append(Token('LBRACE', '{', line, col))
                i += 1
                col += 1
                continue
            if ch == '}':
                tokens.append(Token('RBRACE', '}', line, col))
                i += 1
                col += 1
                continue
            # Identifier or keyword
            if ch.isalpha() or ch == '_':
                start = i
                start_col = col
                while i < n and (src[i].isalnum() or src[i] == '_'):
                    i += 1
                    col += 1
                tokens.append(Token('IDENT', src[start:i], line, start_col))
                continue
            # Anything else (operators, parens, brackets, etc.)
            tokens.append(Token('OTHER', ch, line, col))
            i += 1
            col += 1
        return tokens

    all_tokens = tokenize(source)

    # --- 构建 return 语句和初始化器的行集合 (复用原逻辑) ---
    in_return_lines: set[int] = set()
    in_return = False
    paren_depth = 0
    for line_idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not in_return:
            if stripped.startswith('return ') or stripped.startswith('return(') or stripped == 'return;':
                in_return = True
                paren_depth = 0
        if in_return:
            in_return_lines.add(line_idx)
            paren_depth += line.count('(') - line.count(')')
            if ';' in line:
                in_return = False
                paren_depth = 0

    in_initializer_lines: set[int] = set()
    brace_depth = 0
    in_init = False
    for line_idx, line in enumerate(lines, start=1):
        if not in_init:
            if re.search(r'=\s*\{', line):
                in_init = True
                brace_depth = 0
                for ch in line:
                    if ch == '{':
                        brace_depth += 1
                    elif ch == '}':
                        brace_depth -= 1
                if brace_depth <= 0:
                    in_init = False
                continue
        if in_init:
            in_initializer_lines.add(line_idx)
            for ch in line:
                if ch == '{':
                    brace_depth += 1
                elif ch == '}':
                    brace_depth -= 1
            if brace_depth <= 0:
                in_init = False

    # --- 构建函数体内行集合 (brace_depth > 0 的行) ---
    in_function_lines: set[int] = set()
    func_brace_depth = 0
    for line_idx, line in enumerate(lines, start=1):
        for ch in line:
            if ch == '{':
                func_brace_depth += 1
            elif ch == '}':
                func_brace_depth -= 1
        if func_brace_depth > 0:
            in_function_lines.add(line_idx)

    # --- 在 token 流上识别 member access 链 ---
    # 模式: IDENT (ARROW|DOT IDENT)+
    # 例: tp -> sess_ctx -> conn  产出表达式 "tp->sess_ctx->conn"
    _ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "^=", "|="}

    i = 0
    n = len(all_tokens)
    while i < n:
        tok = all_tokens[i]
        if tok.kind != 'IDENT' or tok.value not in all_vars:
            i += 1
            continue

        # 检查前一个 token 是否是 -> 或 . (说明当前 IDENT 是别人的成员, 不是根变量)
        if i > 0 and all_tokens[i - 1].kind in ('ARROW', 'DOT'):
            i += 1
            continue

        # 尝试解析链: IDENT (ARROW|DOT IDENT)*
        chain_start = i
        expr_parts = [tok.value]
        j = i + 1
        while j + 1 < n:
            op_tok = all_tokens[j]
            if op_tok.kind not in ('ARROW', 'DOT'):
                break
            member_tok = all_tokens[j + 1]
            if member_tok.kind != 'IDENT':
                break
            expr_parts.append(op_tok.value)
            expr_parts.append(member_tok.value)
            j += 2

        # 至少需要一次 -> 或 . 访问, 除非是 shared_var_names 中的直接共享变量引用
        if len(expr_parts) < 3 and tok.value not in shared_var_names:
            i += 1
            continue

        expr = ''.join(expr_parts)
        access_line = tok.line
        # end_line: 向后找到语句结束的 ; 所在行, 确保 flush 插在完整语句之后
        end_line = all_tokens[j - 1].line
        for k in range(j, n):
            if all_tokens[k].kind == 'SEMI':
                end_line = all_tokens[k].line
                break
            if all_tokens[k].kind in ('LBRACE', 'RBRACE'):
                break

        # 跳过 return 语句、初始化器内部、函数体外部
        if access_line in in_return_lines or access_line in in_initializer_lines or access_line not in in_function_lines:
            i = j
            continue

        # 检测是否是写操作: 链后面紧跟赋值运算符
        is_write = False
        if j < n and all_tokens[j].kind == 'ASSIGN_OP':
            is_write = True

        var_name = tok.value
        col = tok.col + 1  # 1-indexed

        already = any((a.end_line == end_line and a.expression == expr) or
                      (a.line == access_line and a.expression == expr and a.is_write == is_write)
                      for a in accesses)
        if not already:
            accesses.append(VariableAccess(
                name=var_name, line=access_line, column=col,
                end_line=end_line, is_write=is_write,
                expression=expr, root_name=var_name,
                root_expression=expr, use_temp=True,
            ))

        i = j

    return accesses




def _find_global_var_accesses_regex(source: str, shared_var_names: set[str], shared_pointer_vars: set[str]) -> list[VariableAccess]:
    """Source-level fallback: scan for shared variable member accesses
    that clang missed due to unresolvable types. Uses SIM_FLUSH_TEMP.
    Legacy regex-based implementation."""
    accesses: list[VariableAccess] = []
    lines = source.splitlines()

    all_vars = shared_var_names | shared_pointer_vars

    _ASSIGN_RE = re.compile(r'^\s*(\w+(?:(?:->|\.)\w+)+)\s*(\|=|\+=|-=|\*=|/=|%=|<<=|>>=|&=|\^=|=(?!=))')

    in_return_lines: set[int] = set()
    in_return = False
    paren_depth = 0
    for line_idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not in_return:
            if stripped.startswith('return ') or stripped.startswith('return(') or stripped == 'return;':
                in_return = True
                paren_depth = 0
        if in_return:
            in_return_lines.add(line_idx)
            paren_depth += line.count('(') - line.count(')')
            if ';' in line:
                in_return = False
                paren_depth = 0

    in_initializer_lines: set[int] = set()
    brace_depth = 0
    in_init = False
    for line_idx, line in enumerate(lines, start=1):
        if not in_init:
            if re.search(r'=\s*\{', line):
                in_init = True
                brace_depth = 0
                for ch in line:
                    if ch == '{':
                        brace_depth += 1
                    elif ch == '}':
                        brace_depth -= 1
                if brace_depth <= 0:
                    in_init = False
                continue
        if in_init:
            in_initializer_lines.add(line_idx)
            for ch in line:
                if ch == '{':
                    brace_depth += 1
                elif ch == '}':
                    brace_depth -= 1
            if brace_depth <= 0:
                in_init = False

    for var_name in sorted(all_vars, key=len, reverse=True):
        for line_idx, line in enumerate(lines, start=1):
            if line_idx in in_return_lines or line_idx in in_initializer_lines:
                continue

            # 计算该行所属语句的结束行 (找到 ; 所在行)
            stmt_end_line = line_idx
            if ';' not in line:
                for fwd in range(line_idx, len(lines)):
                    if ';' in lines[fwd]:
                        stmt_end_line = fwd + 1
                        break
                    if lines[fwd].strip() in ('{', '}'):
                        break

            in_string: set[int] = set()
            in_str = False
            str_char = ''
            for ci, ch in enumerate(line):
                if not in_str:
                    if ch in ('"', "'"):
                        in_str = True
                        str_char = ch
                        in_string.add(ci)
                else:
                    in_string.add(ci)
                    if ch == str_char and (ci == 0 or line[ci - 1] != '\\'):
                        in_str = False

            m = _ASSIGN_RE.match(line)
            if m:
                lhs = m.group(1).strip()
                if m.start(1) not in in_string and lhs.startswith(var_name) and (lhs == var_name or lhs[len(var_name)] in ('-', '.')):
                    col = m.start(1) + 1
                    already = any(a.end_line == stmt_end_line and a.expression == lhs for a in accesses)
                    if not already:
                        accesses.append(VariableAccess(
                            name=var_name, line=line_idx, column=col,
                            end_line=stmt_end_line, is_write=True,
                            expression=lhs, root_name=var_name,
                            root_expression=lhs, use_temp=True,
                        ))

            for m in re.finditer(r'\b' + re.escape(var_name) + r'((?:(?:->|\.)\s*\w+)+)', line):
                if m.start() in in_string:
                    continue
                if m.start() > 0 and line[m.start() - 1] in ('>', '.'):
                    continue
                expr = var_name + m.group(1)
                col = m.start() + 1
                already = any((a.end_line == stmt_end_line and a.expression == expr) or
                              (a.line == line_idx and a.expression == expr and a.is_write)
                              for a in accesses)
                if not already:
                    accesses.append(VariableAccess(
                        name=var_name, line=line_idx, column=col,
                        end_line=stmt_end_line, is_write=False,
                        expression=expr, root_name=var_name,
                        root_expression=expr, use_temp=True,
                    ))

            # 检测共享变量本身的直接引用 (非成员访问)
            if var_name in shared_var_names:
                for m in re.finditer(r'\b' + re.escape(var_name) + r'\b', line):
                    if m.start() in in_string:
                        continue
                    if m.start() > 0 and line[m.start() - 1] in ('>', '.'):
                        continue
                    # 跳过已被成员链匹配覆盖的位置
                    col = m.start() + 1
                    already = any(a.line == line_idx and a.column == col for a in accesses)
                    if already:
                        continue
                    # 检测写操作: var_name = ...
                    is_write = bool(re.match(r'\s*' + re.escape(var_name) + r'\s*(\|=|\+=|-=|\*=|/=|%=|<<=|>>=|&=|\^=|=(?!=))', line))
                    accesses.append(VariableAccess(
                        name=var_name, line=line_idx, column=col,
                        end_line=stmt_end_line, is_write=is_write,
                        expression=var_name, root_name=var_name,
                        root_expression=var_name, use_temp=True,
                    ))

    return accesses




def _find_global_var_accesses_from_source(source: str, shared_var_names: set[str], shared_pointer_vars: set[str], engine: str = "tokenize") -> list[VariableAccess]:
    if engine == "regex":
        return _find_global_var_accesses_regex(source, shared_var_names, shared_pointer_vars)
    return _find_global_var_accesses_tokenize(source, shared_var_names, shared_pointer_vars)
