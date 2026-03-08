"""Lightweight per-document overview for a repo."""

import time
from typing import Optional

from ..storage import DocStore


def get_repo_overview(repo: str, storage_path: Optional[str] = None) -> dict:
    """Return a lightweight per-document overview: path, top-level title, section count.

    Args:
        repo: Repository identifier.
        storage_path: Custom storage path.

    Returns:
        Dict with one entry per document.
    """
    t0 = time.time()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repo not found: {repo}"}

    doc_top = {}
    doc_count = {}
    for sec in index.sections:
        dp = sec.get("doc_path", "")
        doc_count[dp] = doc_count.get(dp, 0) + 1
        if not sec.get("parent_id") and dp not in doc_top:
            doc_top[dp] = sec.get("title", "")

    documents = [
        {
            "path": dp,
            "title": doc_top.get(dp, ""),
            "sections": doc_count.get(dp, 0),
        }
        for dp in sorted(index.doc_paths)
    ]

    latency_ms = int((time.time() - t0) * 1000)
    return {
        "repo": f"{owner}/{name}",
        "doc_count": len(documents),
        "documents": documents,
        "_meta": {"latency_ms": latency_ms},
    }
