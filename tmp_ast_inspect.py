import pathlib
import sys
import json
sys.path.insert(0, 'src')
from sharedvarfinder.finder import _run_clang_ast_dump
from collections import deque

def offset_to_line_col(offset: int, source: str) -> tuple[int, int]:
    lines = source[:offset].splitlines(keepends=True)
    line = len(lines)
    col = len(lines[-1]) if lines else 0
    return line, col

p = pathlib.Path('srcFile/shared_var_example2.cpp')
source = p.read_text()
ast = _run_clang_ast_dump(source, str(p), p, include_paths=['/home/zz/code/VarDetect_pylibtool/srcFile'])
if not ast:
    print('NO AST')
    sys.exit(1)

fig_lines = {6, 7, 9, 11, 12}
dq = deque([ast])
while dq:
    n = dq.popleft()
    if isinstance(n, dict):
        rng = n.get('range', {})
        begin = rng.get('begin', {})
        line = begin.get('line')
        if line in fig_lines or n.get('kind') in ('MemberExpr', 'UnaryOperator'):
            begin = rng.get('begin', {})
            end = rng.get('end', {})
            if isinstance(begin.get('offset'), int):
                line_col = offset_to_line_col(begin['offset'], source)
            else:
                line_col = None
            print('LINE', line, 'KIND', n.get('kind'), 'OP', n.get('opcode'), 'OFFSET', begin.get('offset'), 'LINECOL', line_col, 'KEYS', sorted(n.keys()))
            if 'inner' in n:
                print('  INNER', [c.get('kind') if isinstance(c, dict) else type(c).__name__ for c in n['inner']])
            if n.get('kind') in ('BinaryOperator', 'CompoundAssignOperator', 'VarDecl') and 'inner' in n:
                for c in n['inner']:
                    if isinstance(c, dict):
                        print('    child', c.get('kind'), c.get('opcode'), sorted(c.keys()))
            if n.get('kind') == 'MemberExpr' or (n.get('kind') == 'UnaryOperator' and n.get('opcode') == '*'):
                print('  RANGE', json.dumps(rng, ensure_ascii=False))
                b = begin.get('offset', 0)
                e = rng.get('end', {}).get('offset', 0)
                tok = rng.get('end', {}).get('tokLen', 0)
                print('  SLICE', repr(source[b:e+tok]))
            if n.get('kind') == 'MemberExpr':
                print('  name', n.get('name'), 'memberDecl', n.get('memberDecl'))
            print('---')
        for c in n.get('inner', []):
            dq.append(c)
    elif isinstance(n, list):
        for c in n:
            dq.append(c)
