"""Section hierarchy for one file (no content)."""

import time
from pathlib import Path
from typing import Optional

from ..storage import DocStore
from ..storage.token_tracker import estimate_savings, record_savings, cost_avoided


def get_document_outline(
    repo: str,
    doc_path: str,
    storage_path: Optional[str] = None,
) -> dict:
    """Return the section structure for a single document, without content."""
    t0 = time.time()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repo not found: {repo}"}

    normalized_doc_path = doc_path.strip()
    if not normalized_doc_path:
        return {"error": "Document path must not be empty"}

    matched_doc_path = normalized_doc_path
    doc_sections = [s for s in index.sections if s.get("doc_path") == matched_doc_path]

    if not doc_sections:
        normalized_path_query = normalized_doc_path.replace("\\", "/").strip("/")
        use_suffix_match = "/" in normalized_path_query
        candidate_paths = sorted({
            s.get("doc_path", "")
            for s in index.sections
            if s.get("doc_path")
            and (
                (
                    use_suffix_match
                    and (
                        s.get("doc_path", "") == normalized_path_query
                        or s.get("doc_path", "").endswith(f"/{normalized_path_query}")
                    )
                )
                or (
                    not use_suffix_match
                    and Path(s.get("doc_path", "")).name == normalized_path_query
                )
            )
        })
        if len(candidate_paths) > 1:
            return {
                "error": f"Document path is ambiguous: {doc_path}",
                "matches": candidate_paths,
            }
        if candidate_paths:
            matched_doc_path = candidate_paths[0]
            doc_sections = [
                s for s in index.sections if s.get("doc_path") == matched_doc_path
            ]

    if not doc_sections:
        return {"error": f"Document not found: {doc_path}"}

    doc_sections = sorted(doc_sections, key=lambda s: s.get("byte_start", 0))

    outline = []
    for sec in doc_sections:
        outline.append({
            "id": sec.get("id"),
            "title": sec.get("title"),
            "level": sec.get("level"),
            "summary": sec.get("summary"),
            "parent_id": sec.get("parent_id"),
            "children": sec.get("children"),
            "byte_start": sec.get("byte_start"),
            "byte_end": sec.get("byte_end"),
        })

    raw_bytes = sum(len(s.get("content", "").encode("utf-8")) for s in doc_sections)
    response_bytes = sum(len(str(o).encode("utf-8")) for o in outline)
    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total = record_savings(tokens_saved, storage_path)
    ca = cost_avoided(tokens_saved, total)

    latency_ms = int((time.time() - t0) * 1000)
    return {
        "repo": f"{owner}/{name}",
        "doc_path": matched_doc_path,
        "sections": outline,
        "section_count": len(outline),
        "_meta": {
            "latency_ms": latency_ms,
            "sections_returned": len(outline),
            "tokens_saved": tokens_saved,
            **ca,
        },
    }
