"""Top-level auto-refresh orchestration with per-repo locking."""

import threading
from pathlib import Path
from typing import Optional

from ..storage.doc_store import DocStore
from .git_detector import is_git_repo, detect_git_changes
from .mtime_detector import detect_mtime_changes
from .incremental import reindex_changed_files
from .summarization_queue import queue_ai_summarization

_repo_locks: dict = {}
_locks_lock = threading.Lock()


def _get_repo_lock(repo_key: str) -> threading.Lock:
    with _locks_lock:
        if repo_key not in _repo_locks:
            _repo_locks[repo_key] = threading.Lock()
        return _repo_locks[repo_key]


def auto_refresh(repo: str, storage_path: Optional[str]) -> None:
    """Check for file changes and incrementally re-index if needed.

    Runs synchronously. Skips if:
    - Index has no source_path (remote repo or not yet indexed locally)
    - source_path no longer exists on disk
    - Another refresh is already running for this repo (lock.acquire non-blocking)
    """
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index or not index.source_path:
        return

    if not Path(index.source_path).exists():
        return

    lock = _get_repo_lock(f"{owner}/{name}")
    if not lock.acquire(blocking=False):
        return

    try:
        _do_refresh(index, store, owner, name)
    finally:
        lock.release()


def _do_refresh(index, store: DocStore, owner: str, name: str) -> None:
    source_path = index.source_path

    if is_git_repo(source_path):
        changeset = detect_git_changes(
            source_path=source_path,
            last_commit=index.last_indexed_commit,
            indexed_file_metas=index.file_hashes,
        )
    else:
        changeset = detect_mtime_changes(
            source_path=source_path,
            doc_file_metas=index.file_hashes,
        )

    if not changeset.modified and not changeset.deleted:
        return

    result = reindex_changed_files(
        index=index,
        source_path=source_path,
        modified=changeset.modified,
        deleted=changeset.deleted,
        new_commit=changeset.new_commit,
        store=store,
    )

    # reindex_changed_files returns (updated_index, sections_needing_ai)
    if isinstance(result, tuple):
        updated_index, sections_needing_ai = result
        queue_ai_summarization(
            owner=owner,
            name=name,
            sections_needing_ai=sections_needing_ai,
            store=store,
        )
