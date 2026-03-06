"""mtime+size fallback change detection for non-git repos and filtered files."""

from pathlib import Path

from ._types import ChangeSet
from ._scan import scan_doc_files


def detect_mtime_changes(
    source_path: str,
    doc_file_metas: dict,
    extra_ignore_patterns: list[str] | None = None,
    follow_symlinks: bool = False,
) -> ChangeSet:
    """Detect changes by comparing mtime+size against stored metadata.

    doc_file_metas: {rel_path: {"sha256": ..., "mtime": float, "size": int}}
    Returns ChangeSet with modified and deleted sets (new_commit is always None).
    """
    changed: set = set()
    deleted: set = set()
    root = Path(source_path)

    current_files = scan_doc_files(
        source_path,
        extra_ignore_patterns=extra_ignore_patterns,
        follow_symlinks=follow_symlinks,
    )

    for rel_path, meta in doc_file_metas.items():
        if rel_path not in current_files:
            deleted.add(rel_path)
            continue

        abs_path = root / rel_path
        if not abs_path.exists():
            deleted.add(rel_path)
            continue
        try:
            stat = abs_path.stat()
            mtime = meta.get("mtime", 0.0) if isinstance(meta, dict) else 0.0
            size = meta.get("size", 0) if isinstance(meta, dict) else 0
            if stat.st_mtime != mtime or stat.st_size != size:
                changed.add(rel_path)
        except OSError:
            deleted.add(rel_path)

    # New files not in the index yet
    indexed = set(doc_file_metas.keys())
    for rel_path in current_files - indexed:
        changed.add(rel_path)

    return ChangeSet(
        modified=changed - deleted,
        deleted=deleted,
        new_commit=None,
    )
