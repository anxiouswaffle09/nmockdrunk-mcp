"""Incremental re-index: synchronous re-parse of changed files, no AI."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from ..parser import parse_file, preprocess_content
from ..security import is_secret_file, DEFAULT_MAX_FILE_SIZE
from ..storage.doc_store import DocIndex, DocStore, INDEX_VERSION
from ..summarizer.batch_summarize import heading_summary, title_fallback
from ._scan import scan_doc_files


def _file_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def reindex_changed_files(
    index: DocIndex,
    source_path: str,
    modified: set,
    deleted: set,
    new_commit: Optional[str],
    store: DocStore,
    extra_ignore_patterns: list[str] | None = None,
    follow_symlinks: bool = False,
) -> Tuple[DocIndex, list]:
    """Synchronous, no AI. Updates byte offsets and section structure.

    Preserves existing summaries for sections whose heading hasn't changed.
    Returns the updated DocIndex (also written atomically to disk).
    """
    owner, name = index.owner, index.name
    repo_id = f"{owner}/{name}"
    current_files = scan_doc_files(
        source_path,
        extra_ignore_patterns=extra_ignore_patterns,
        follow_symlinks=follow_symlinks,
    )

    files_to_remove = deleted | modified

    # Sections for unchanged files (already serialized dicts)
    surviving_sections = [
        s for s in index.sections
        if s.get("doc_path") not in files_to_remove
    ]

    new_sections_objs = []
    new_raw_files: dict = {}
    new_file_metas: dict = {}
    retained_modified = set()

    for rel_path in modified:
        if rel_path not in current_files or is_secret_file(rel_path):
            continue
        abs_path = Path(source_path) / rel_path
        if not abs_path.exists():
            continue
        try:
            if abs_path.stat().st_size > DEFAULT_MAX_FILE_SIZE:
                continue
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        try:
            parsed_content = preprocess_content(content, rel_path)
            sections = parse_file(parsed_content, rel_path, repo_id)
        except Exception:
            continue
        if not sections:
            continue

        # Reuse cached summaries for headings that haven't changed
        old_summaries = {
            s["title"]: s.get("summary", "")
            for s in index.sections
            if s.get("doc_path") == rel_path
        }
        for sec in sections:
            if sec.title in old_summaries and old_summaries[sec.title]:
                sec.summary = old_summaries[sec.title]

        new_sections_objs.extend(sections)
        new_raw_files[rel_path] = parsed_content
        retained_modified.add(rel_path)

        try:
            stat = abs_path.stat()
            new_file_metas[rel_path] = {
                "sha256": _file_hash(parsed_content),
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            }
        except OSError:
            new_file_metas[rel_path] = {
                "sha256": _file_hash(parsed_content),
                "mtime": 0.0,
                "size": 0,
            }

    # Tier 1 + Tier 3 summarization synchronously for sections without a summary
    sections_needing_ai = []
    for sec in new_sections_objs:
        if not sec.summary:
            sec.summary = heading_summary(sec)
        if len(sec.summary) < 20 and sec.content:
            sections_needing_ai.append(sec)

    # Tier 3 fallback for any still missing
    for sec in new_sections_objs:
        if not sec.summary:
            sec.summary = title_fallback(sec)

    all_sections_dicts = surviving_sections + [s.to_dict() for s in new_sections_objs]

    # Update file_hashes: remove deleted/modified, add updated
    updated_file_hashes = {
        k: v for k, v in index.file_hashes.items()
        if k not in files_to_remove
    }
    updated_file_hashes.update(new_file_metas)

    # Persist changed raw files to content cache (atomic per-file write)
    content_dir = store._content_dir(owner, name)
    for rel_path, content in new_raw_files.items():
        dest = store._safe_content_path(content_dir, rel_path)
        if dest:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp_dest = dest.with_name(dest.name + ".tmp")
            with open(tmp_dest, "wb") as f:
                f.write(content.encode("utf-8"))
            tmp_dest.replace(dest)

    # Remove files that were deleted or are no longer indexable from content cache
    for rel_path in deleted | (modified - retained_modified):
        dest = store._safe_content_path(content_dir, rel_path)
        if dest and dest.exists():
            dest.unlink()

    # Build updated doc_paths list
    updated_doc_paths = [p for p in index.doc_paths if p not in files_to_remove]
    for rel_path in retained_modified:
        if rel_path not in updated_doc_paths:
            updated_doc_paths.append(rel_path)
    updated_doc_paths = sorted(updated_doc_paths)
    updated_doc_types: dict[str, int] = {}
    for rel_path in updated_doc_paths:
        ext = Path(rel_path).suffix.lower()
        updated_doc_types[ext] = updated_doc_types.get(ext, 0) + 1

    updated_index = DocIndex(
        repo=index.repo,
        owner=owner,
        name=name,
        indexed_at=datetime.now().isoformat(),
        doc_paths=updated_doc_paths,
        doc_types=updated_doc_types,
        sections=all_sections_dicts,
        index_version=INDEX_VERSION,
        file_hashes=updated_file_hashes,
        source_path=source_path,
        last_indexed_commit=new_commit or index.last_indexed_commit,
        extra_ignore_patterns=list(extra_ignore_patterns or []),
        follow_symlinks=follow_symlinks,
    )

    # Atomic write
    index_path = store._index_path(owner, name)
    tmp_path = index_path.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(store._index_to_dict(updated_index), f, indent=2)
        tmp_path.replace(index_path)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return index, []  # Preserve old index on disk failure

    store._write_sidecar(owner, name, source_path, updated_index.doc_paths)
    return updated_index, sections_needing_ai
