"""Incremental re-index: synchronous re-parse of changed files, no AI."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..parser import parse_file, preprocess_content
from ..storage.doc_store import DocIndex, DocStore, INDEX_VERSION
from ..summarizer.batch_summarize import heading_summary, title_fallback


def _file_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def reindex_changed_files(
    index: DocIndex,
    source_path: str,
    modified: set,
    deleted: set,
    new_commit: Optional[str],
    store: DocStore,
) -> DocIndex:
    """Synchronous, no AI. Updates byte offsets and section structure.

    Preserves existing summaries for sections whose heading hasn't changed.
    Returns the updated DocIndex (also written atomically to disk).
    """
    owner, name = index.owner, index.name
    repo_id = f"{owner}/{name}"

    files_to_remove = deleted | modified

    # Sections for unchanged files (already serialized dicts)
    surviving_sections = [
        s for s in index.sections
        if s.get("doc_path") not in files_to_remove
    ]

    new_sections_objs = []
    new_raw_files: dict = {}
    new_file_metas: dict = {}

    for rel_path in modified:
        abs_path = Path(source_path) / rel_path
        if not abs_path.exists():
            continue
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        try:
            parsed_content = preprocess_content(content, rel_path)
            sections = parse_file(parsed_content, rel_path, repo_id)
        except Exception:
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

    # Persist changed raw files to content cache
    content_dir = store._content_dir(owner, name)
    for rel_path, content in new_raw_files.items():
        dest = store._safe_content_path(content_dir, rel_path)
        if dest:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(content.encode("utf-8"))

    # Remove deleted files from content cache
    for rel_path in deleted:
        dest = store._safe_content_path(content_dir, rel_path)
        if dest and dest.exists():
            dest.unlink()

    # Build updated doc_paths list
    updated_doc_paths = [p for p in index.doc_paths if p not in deleted]
    for rel_path in modified:
        if rel_path not in updated_doc_paths and rel_path in new_raw_files:
            updated_doc_paths.append(rel_path)
    updated_doc_paths = sorted(updated_doc_paths)

    updated_index = DocIndex(
        repo=index.repo,
        owner=owner,
        name=name,
        indexed_at=datetime.now().isoformat(),
        doc_paths=updated_doc_paths,
        doc_types=index.doc_types,
        sections=all_sections_dicts,
        index_version=INDEX_VERSION,
        file_hashes=updated_file_hashes,
        source_path=source_path,
        last_indexed_commit=new_commit or index.last_indexed_commit,
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
        return index  # Preserve old index on disk failure

    return updated_index, sections_needing_ai
