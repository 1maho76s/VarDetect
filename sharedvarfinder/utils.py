from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Iterator, List

from .models import SharedVariable, SUPPORTED_EXTENSIONS
from .shared_vars import find_shared_variables_in_text


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
