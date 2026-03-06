"""Batch content retrieval for multiple sections."""

import hashlib
import time
from typing import Optional

from ..storage import DocStore
from ..storage.token_tracker import estimate_savings, record_savings, cost_avoided


def get_sections(
    repo: str,
    section_ids: list,
    verify: bool = False,
    storage_path: Optional[str] = None,
) -> dict:
    """Retrieve full content for multiple sections in one call.

    Args:
        repo: Repository identifier.
        section_ids: List of section IDs to retrieve.
        verify: If True, verify content hashes.
        storage_path: Custom storage path.

    Returns:
        Dict with list of section results.
    """
    t0 = time.time()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repo not found: {repo}"}

    results = []
    total_tokens_saved = 0

    for section_id in section_ids:
        sec = index.get_section(section_id)
        if not sec:
            results.append({"error": f"Section not found: {section_id}"})
            continue

        content = store._read_section_bytes(owner, name, sec)
        if content is None:
            results.append({"error": f"Content not available for section: {section_id}"})
            continue

        result_sec = {k: v for k, v in sec.items() if k != "content"}
        result_sec["content"] = content

        if verify:
            actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            stored_hash = sec.get("content_hash", "")
            result_sec["hash_verified"] = (actual_hash == stored_hash) if stored_hash else None

        doc_path = sec.get("doc_path", "")
        raw_bytes = sum(
            len(s.get("content", "").encode("utf-8"))
            for s in index.sections
            if s.get("doc_path") == doc_path
        )
        response_bytes = len(content.encode("utf-8"))
        tokens_saved = estimate_savings(raw_bytes, response_bytes)
        total_tokens_saved += tokens_saved

        results.append({"section": result_sec, "tokens_saved": tokens_saved})

    total = record_savings(total_tokens_saved, storage_path)
    ca = cost_avoided(total_tokens_saved, total)

    latency_ms = int((time.time() - t0) * 1000)
    return {
        "sections": results,
        "section_count": len(results),
        "_meta": {
            "latency_ms": latency_ms,
            "sections_returned": len(results),
            "tokens_saved": total_tokens_saved,
            "total_tokens_saved": total,
            **ca,
        },
    }
