"""Shared doc-file scanner for auto-refresh."""

import os
from pathlib import Path

from ..parser import ALL_EXTENSIONS
from ..tools._constants import SKIP_PATTERNS


def scan_doc_files(source_path: str) -> set:
    """Return set of relative paths for all doc files under source_path.

    Uses the same extension filter as discover_doc_files in index_local.py.
    Does not apply gitignore rules — callers handle that distinction.
    """
    root = Path(source_path).resolve()
    found = set()

    for dirpath, dirnames, filenames in os.walk(root):
        dir_path = Path(dirpath)
        try:
            dir_rel = dir_path.relative_to(root).as_posix()
        except ValueError:
            dirnames.clear()
            continue

        # Prune skipped directories in-place
        dirnames[:] = [
            d for d in dirnames
            if not _should_skip(f"{dir_rel}/{d}/".lstrip("./"))
        ]

        for filename in filenames:
            ext = Path(filename).suffix.lower()
            if ext not in ALL_EXTENSIONS:
                continue

            rel_path = f"{dir_rel}/{filename}".lstrip("./") if dir_rel != "." else filename
            if _should_skip(rel_path):
                continue

            found.add(rel_path)

    return found


def _should_skip(rel_path: str) -> bool:
    normalized = "/" + rel_path.replace("\\", "/")
    for pat in SKIP_PATTERNS:
        if ("/" + pat) in normalized:
            return True
    return False
