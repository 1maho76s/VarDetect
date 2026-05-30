from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional


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
            struct_header_args = ["-include", "stdint.h", "-include", "stdbool.h", "-include", struct_header]
        result = subprocess.run(
            [
                clang_path,
                "-Xclang",
                "-ast-dump=json",
                "-fsyntax-only",
                "-ferror-limit=0",
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
