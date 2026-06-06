import argparse
import json
import os
import re


def extract_structs_final(folder_path: str, output_file: str) -> None:
    """Extract struct definitions from header files in folder_path and write to output_file."""
    # 精准匹配结构体正则（使用非贪婪匹配 .*? 并在尾部增强对分号的捕获）
    struct_pattern = re.compile(
        r"(?:typedef\s+)?struct\s*\w*\s*\{.*?\}\s*\w*;", re.DOTALL
    )

    if not os.path.exists(folder_path):
        print(f"路径不存在: {folder_path}")
        return

    struct_count = 0

    with open(output_file, "w", encoding="utf-8") as outfile:
        for root, _, files in os.walk(folder_path):
            for file in files:
                if file.endswith((".h", ".hpp", ".c")):
                    file_path = os.path.join(root, file)
                    content = None

                    # 1. 优先 GBK 读取
                    try:
                        with open(file_path, "r", encoding="gbk") as infile:
                            content = infile.read()
                    except UnicodeDecodeError:
                        try:
                            with open(file_path, "r", encoding="utf-8") as infile:
                                content = infile.read()
                        except UnicodeDecodeError:
                            try:
                                with open(
                                    file_path,
                                    "r",
                                    encoding="gbk",
                                    errors="ignore",
                                ) as infile:
                                    content = infile.read()
                            except Exception:
                                continue

                    if content:
                        matches = struct_pattern.findall(content)
                        if matches:
                            # 【优化点】使用集合对当前文件内的 struct 进行去重，防止条件编译导致重复打印
                            seen_structs = set()
                            unique_matches = []

                            for match in matches:
                                cleaned_match = match.strip()
                                if cleaned_match not in seen_structs:
                                    seen_structs.add(cleaned_match)
                                    unique_matches.append(cleaned_match)

                            # 写入文件
                            outfile.write(
                                f"/* ------------------------------------------\n"
                            )
                            outfile.write(f"   文件位置: {file_path}\n")
                            outfile.write(
                                f"   ------------------------------------------ */\n\n"
                            )

                            for match in unique_matches:
                                # 【优化点】将连续超过2个的换行符压缩为1个，让排版更紧凑
                                format_match = re.sub(r"\n\s*\n+", "\n\n", match)
                                outfile.write(format_match + "\n\n")
                                struct_count += 1

    print(f"\n全部提取完成！共精准提取并清洗了 {struct_count} 个结构体。")


def _extract_member_name(member_line: str) -> str:
    """Extract the variable name from a struct member declaration line.
    Returns empty string for anonymous bit-fields like 'unsigned int : 0;'."""
    member_line = member_line.strip().rstrip(";").strip()
    # Check for anonymous bit-field: e.g., "unsigned int : 0", "int : 3"
    # Pattern: type keywords followed by : digits with no identifier in between
    if re.search(r'^\s*(?:unsigned\s+)?(?:int|char|short|long|float|double)\s*:\s*\d+\s*$', member_line):
        return ""
    # Remove bit-field width
    member_line = re.sub(r'\s*:\s*\d+\s*$', '', member_line)
    # Remove array dimensions
    member_line = re.sub(r'\s*\[[^\]]*\]', '', member_line)
    parts = member_line.split()
    for part in reversed(parts):
        part = part.lstrip('*').strip()
        if re.match(r'^[A-Za-z_]\w*$', part):
            return part
    return ""


def _split_multi_declarators(member_text: str) -> list[tuple[str, bool]]:
    """Split a comma-separated member line into individual (name, is_bitfield) pairs.
    Handles: 'uint32_t a : 1, b : 2, c;'  -> [('a', True), ('b', True), ('c', False)]"""
    results: list[tuple[str, bool]] = []
    # Split by comma outside of brackets/parens
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in member_text:
        if ch in '([{':
            depth += 1
            current.append(ch)
        elif ch in ')]}':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current).strip())

    if not parts:
        return results

    # Extract type prefix: everything before the first declarator name.
    # The first part contains "type name1 [:width]".
    # Use regex to split: optional pointer stars + name [+ optional :width]
    first = parts[0]
    type_m = re.match(r'^(.+?)\s+([*\s]*)(\w+)\s*(:.*)?$', first)
    type_prefix = ''
    if type_m:
        type_prefix = type_m.group(1)  # e.g., "uint32_t" or "const char"
        # Add first declarator
        name = type_m.group(3)
        width_part = type_m.group(4) or ''
        is_bf = bool(re.search(r':\s*\d+', width_part))
        results.append((name, is_bf))
    else:
        return results

    # Process remaining parts (each is "name [:width]" optionally with * prefix)
    for part in parts[1:]:
        part = part.strip()
        if not part:
            continue
        # Strip any leading * (pointer)
        name_part = part.lstrip('*').strip()
        m = re.match(r'(\w+)\s*(:.*)?$', name_part)
        if m:
            name = m.group(1)
            width_part = m.group(2) or ''
            is_bf = bool(re.search(r':\s*\d+', width_part))
            results.append((name, is_bf))
    return results


def _find_struct_defs(text: str) -> list[tuple[str, str, str]]:
    """Find all struct/typedef struct definitions using brace matching.
    Returns list of (tag_name, body, typedef_name)."""
    results: list[tuple[str, str, str]] = []
    pattern = re.compile(r'(?:typedef\s+)?struct\s+(\w*)\s*\{')
    for m in pattern.finditer(text):
        tag_name = m.group(1) or ""
        start = m.end() - 1  # position of the opening {
        # Match the closing }
        depth = 1
        pos = start + 1
        while pos < len(text) and depth > 0:
            if text[pos] == '{':
                depth += 1
            elif text[pos] == '}':
                depth -= 1
            pos += 1
        if depth != 0:
            continue
        body = text[start + 1: pos - 1]
        # Find the typedef name after the closing }
        after = text[pos - 1:].lstrip('}').strip()
        m2 = re.match(r'(\w+)\s*;', after)
        typedef_name = m2.group(1) if m2 else ""
        if tag_name or typedef_name:
            results.append((tag_name, body, typedef_name))
    return results


def build_bitfield_map(folder_path: str, output_file: str) -> dict[str, dict[str, bool]]:
    """Build a JSON map of struct -> member -> is_bitfield, skipping unnamed members."""
    member_pattern = re.compile(r'([^;]+);', re.DOTALL)
    bitfield_re = re.compile(r'\s*:\s*\d+\s*$')

    if not os.path.exists(folder_path):
        print(f"路径不存在: {folder_path}")
        return {}

    all_structs: dict[str, dict[str, bool]] = {}

    for root, _, files in os.walk(folder_path):
        for file in files:
            if not file.endswith((".h", ".hpp", ".c")):
                continue
            file_path = os.path.join(root, file)
            content = None
            try:
                with open(file_path, "r", encoding="gbk") as infile:
                    content = infile.read()
            except UnicodeDecodeError:
                try:
                    with open(file_path, "r", encoding="utf-8") as infile:
                        content = infile.read()
                except UnicodeDecodeError:
                    try:
                        with open(file_path, "r", encoding="gbk", errors="ignore") as infile:
                            content = infile.read()
                    except Exception:
                        continue

            if not content:
                continue

            content = re.sub(r'/\*.*?\*/', '', content, flags=re.S)
            content = re.sub(r'//.*', '', content)

            # Use brace matching to extract struct bodies (handles nested structs/unions)
            struct_defs = _find_struct_defs(content)
            for tag_name, body, typedef_name in struct_defs:
                struct_name = typedef_name or tag_name
                if not struct_name:
                    continue
                if struct_name in all_structs:
                    continue

                members: dict[str, bool] = {}
                for member_match in member_pattern.finditer(body):
                    member_text = member_match.group(1).strip()
                    if not member_text or member_text.startswith('#'):
                        continue
                    # Handle multi-declarator lines (e.g. 'int a : 1, b : 2, c;')
                    multi = _split_multi_declarators(member_text)
                    if multi:
                        for name, is_bf in multi:
                            members[name] = is_bf
                    else:
                        is_bitfield = bool(bitfield_re.search(member_text.rstrip()))
                        name = _extract_member_name(member_text)
                        if name:
                            members[name] = is_bitfield

                if members:
                    all_structs[struct_name] = members

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(all_structs, f, indent=2, ensure_ascii=False)

    print(f"位域映射已写入: {output_file}  (共 {len(all_structs)} 个结构体)")
    return all_structs


def build_struct_header(folder_path: str, output_file: str) -> None:
    """Generate a consolidated header containing all struct definitions from folder_path.
    This header can be #included during AST parsing to help clang resolve types."""
    if not os.path.exists(folder_path):
        print(f"路径不存在: {folder_path}")
        return

    collected: list[tuple[str, str, str, str]] = []  # (relpath, tag_name, typedef_name, full_def)
    
    for root, _, files in os.walk(folder_path):
        for file in files:
            if not file.endswith((".h", ".hpp", ".c")):
                continue
            file_path = os.path.join(root, file)
            # Skip the output file itself to avoid self-inclusion
            if os.path.abspath(file_path) == os.path.abspath(output_file):
                continue
            content = None
            try:
                with open(file_path, "r", encoding="gbk") as f:
                    content = f.read()
            except UnicodeDecodeError:
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                except UnicodeDecodeError:
                    try:
                        with open(file_path, "r", encoding="gbk", errors="ignore") as f:
                            content = f.read()
                    except Exception:
                        continue
            if not content:
                continue

            content_no_comments = re.sub(r'/\*.*?\*/', '', content, flags=re.S)
            content_no_comments = re.sub(r'//.*', '', content_no_comments)

            struct_defs = _find_struct_defs(content_no_comments)
            seen_names: set[str] = set()
            for tag_name, body, typedef_name in struct_defs:
                name = typedef_name or tag_name
                if not name or name in seen_names:
                    continue
                seen_names.add(name)
                # Clean up body: remove leading/trailing blank lines, collapse multi-blank-lines
                clean_body = body.strip()
                clean_body = re.sub(r'\n\s*\n\s*\n+', '\n\n', clean_body)
                # Normalize indentation: find min leading spaces and strip that much
                lines = clean_body.splitlines()
                min_indent = min((len(l) - len(l.lstrip())) for l in lines if l.strip()) if lines else 0
                clean_body = '\n'.join('    ' + l[min_indent:] for l in lines)
                # Reconstruct full definition
                if typedef_name and tag_name:
                    full = f"typedef struct {tag_name} {{\n{clean_body}\n}} {typedef_name};"
                elif typedef_name:
                    full = f"typedef struct {{\n{clean_body}\n}} {typedef_name};"
                else:
                    full = f"struct {tag_name} {{\n{clean_body}\n}};"
                collected.append((os.path.relpath(file_path, folder_path), tag_name, typedef_name, name, full))

    if not collected:
        print("未找到结构体定义")
        return

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("/* Auto-generated consolidated struct header for AST resolution */\n")
        f.write("/* Generated by: collect.py --output-header */\n\n")
        for relpath, tag, tdef, name, full_def in collected:
            f.write(f"/* source: {relpath} */\n")
            f.write(full_def + "\n\n")

    print(f"结构体头文件已写入: {output_file}  (共 {len(collected)} 个结构体)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="从 C/C++ 头文件中提取所有结构体定义"
    )
    parser.add_argument(
        "source", nargs="?", default="./component",
        help="源文件夹路径 (默认: ./component)"
    )
    parser.add_argument(
        "-o", "--output", default="all_structs_cleaned.txt",
        help="结构体文本输出路径 (默认: all_structs_cleaned.txt)"
    )
    parser.add_argument(
        "--output-header", default=None,
        help="生成合并结构体头文件 (例: all_structs.h)，帮助 clang 解析类型"
    )
    parser.add_argument(
        "--bitfield-map", default=None,
        help="输出位域映射 JSON 文件路径 (例: bitfield_map.json)"
    )
    parser.add_argument(
        "--all", action="store_true", default=False,
        help="同时生成文本、头文件和位域映射"
    )
    args = parser.parse_args()

    if args.all:
        base = args.output.rsplit(".", 1)[0] if "." in args.output else args.output
        extract_structs_final(args.source, args.output)
        build_struct_header(args.source, args.output_header or f"{base}.h")
        build_bitfield_map(args.source, args.bitfield_map or f"{base}_bitfield.json")
    elif args.output_header and args.bitfield_map:
        build_struct_header(args.source, args.output_header)
        build_bitfield_map(args.source, args.bitfield_map)
    elif args.output_header:
        build_struct_header(args.source, args.output_header)
    elif args.bitfield_map:
        build_bitfield_map(args.source, args.bitfield_map)
    else:
        extract_structs_final(args.source, args.output)


if __name__ == "__main__":
    main()
