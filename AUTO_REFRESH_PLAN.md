# Auto-Refresh Implementation Plan

## Overview

Add pre-call automatic index refresh to jdocmunch-mcp, mirroring the pattern from jcodemunch.
Before any read tool call (`get_toc`, `get_section`, `search_sections`, etc.), the server checks
whether indexed files have changed and re-indexes only what changed — without blocking on AI summarization.

Change detection uses git when available (primary), falling back to mtime+size for non-git folders.

---

## Architecture Summary

```
Tool call arrives
       │
       ▼
auto_refresh(repo, storage_path)          ← NEW: runs synchronously before dispatch
       │
       ├─ load index → get source_path, last_indexed_commit
       │
       ├─ is git repo?
       │     YES → git_detect_changes()
       │     NO  → mtime_detect_changes()
       │
       ├─ changed files found?
       │     YES → incremental_reindex(changed_files)   ← sync, no AI
       │           └─ spawn_ai_summarization_thread()   ← async, non-blocking
       │     NO  → nothing
       │
       └─ proceed with original tool call
```

---

## Part 1: New Fields in DocIndex

**File:** `src/jdocmunch_mcp/storage/doc_store.py`

Add two new fields to `DocIndex` dataclass:

```python
source_path: Optional[str] = None          # Absolute path to original folder (local indexes only)
last_indexed_commit: Optional[str] = None  # git HEAD hash at index time (None if not a git repo)
```

Bump `INDEX_VERSION = 2`. Update `save_index`, `load_index`, and `_index_to_dict` to handle these fields.

Also add per-file mtime and size to `file_hashes` dict for the mtime fallback path. Change structure from:
```json
{ "README.md": "<sha256>" }
```
to:
```json
{
  "README.md": {
    "sha256": "<hash>",
    "mtime": 1709123456.789,
    "size": 4096
  }
}
```

Backwards compatibility: `load_index` checks `index_version`. If version < 2, treat `file_hashes`
entries as bare strings (old format) and convert in-memory. Don't fail — just fall back to full
re-index on first pre-call check.

**Why source_path is needed:** `index_local` currently derives the index key from `folder_path.name`
(e.g. `/home/user/project/docs` → `local/docs`). Without storing the original path, auto-refresh has
no way to know where to look for file changes. We store the absolute resolved path.

---

## Part 2: Store source_path at Index Time

**File:** `src/jdocmunch_mcp/tools/index_local.py`

Pass `source_path=str(folder_path)` into `store.save_index(...)`.

Update `DocStore.save_index` signature to accept `source_path: Optional[str] = None` and
`last_indexed_commit: Optional[str] = None`.

When saving a local index, also capture the current git HEAD:

```python
def _get_git_commit(path: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None
```

Call this in `index_local` and pass the result to `save_index`.

---

## Part 3: Git Change Detection

**New file:** `src/jdocmunch_mcp/auto_refresh/git_detector.py`

### 3.1 Is this a git repo?

```python
def is_git_repo(path: str) -> bool:
    """Check if path is inside a git working tree."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
```

Cache result per source_path in a module-level dict to avoid repeated subprocess calls on every
tool invocation.

### 3.2 Get current HEAD

```python
def get_head_commit(path: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None
```

Detached HEAD and shallow clones both work fine with this command.

### 3.3 Files changed between two commits

```python
def get_commit_diff_files(path: str, old_commit: str, new_commit: str) -> set[str]:
    """Returns relative paths of files changed between old_commit and new_commit."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "diff", "--name-only", old_commit, new_commit, "--", "."],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return {line.strip() for line in result.stdout.splitlines() if line.strip()}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return set()
```

The `--` `.` scopes the diff to the working directory, keeping it fast even in large monorepos.

### 3.4 Uncommitted working tree changes

```python
def get_status_changes(path: str) -> dict[str, str]:
    """
    Returns {relative_path: status_code} for all working tree changes.
    Status codes: 'M' (modified), 'A' (added), 'D' (deleted), 'R' (renamed), '?' (untracked)
    """
    try:
        result = subprocess.run(
            ["git", "-C", path, "status", "--porcelain", "--untracked-files=all", "--", "."],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return {}

        changes = {}
        for line in result.stdout.splitlines():
            if len(line) < 4:
                continue
            xy = line[:2]
            file_part = line[3:]

            # Handle renames: "R old -> new" format
            if "R" in xy and " -> " in file_part:
                old, new = file_part.split(" -> ", 1)
                changes[old.strip()] = "D"
                changes[new.strip()] = "A"
                continue

            status = xy.strip() or "?"
            # Simplify: use first non-space char as the primary status
            primary = xy[0] if xy[0] != " " else xy[1]
            if primary == "?":
                status = "?"
            else:
                status = primary

            changes[file_part.strip()] = status

        return changes
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
```

### 3.5 Gitignored file detection

After getting the git status output, any files present in the `source_path` directory that are
neither tracked nor shown as untracked (`??`) are gitignored. For those, use mtime fallback
(see Part 4).

In practice: after `get_status_changes`, scan the directory for doc files and subtract the set
of files git knows about. Remaining files → mtime check.

### 3.6 Full git detection orchestration

```python
def detect_git_changes(source_path: str, last_commit: Optional[str], indexed_file_mtimes: dict) -> ChangeSet:
    """
    Returns a ChangeSet with:
      - modified: set of relative paths to re-index
      - deleted: set of relative paths to remove from index
    """
    changed = set()
    deleted = set()

    current_commit = get_head_commit(source_path)

    # 1. Committed changes since last index
    if last_commit and current_commit and current_commit != last_commit:
        diff_files = get_commit_diff_files(source_path, last_commit, current_commit)
        changed.update(diff_files)

    # 2. Uncommitted working tree changes
    status = get_status_changes(source_path)
    for rel_path, code in status.items():
        if code == "D":
            deleted.add(rel_path)
        else:
            changed.add(rel_path)

    # 3. Gitignored files → mtime fallback
    all_doc_files = _scan_doc_files(source_path)
    git_known = set(status.keys())
    if current_commit:
        git_known.update(get_commit_diff_files(source_path, current_commit, current_commit))
    gitignored = {f for f in all_doc_files if f not in git_known}
    mtime_changed = _mtime_check(source_path, gitignored, indexed_file_mtimes)
    changed.update(mtime_changed)

    return ChangeSet(modified=changed - deleted, deleted=deleted, new_commit=current_commit)
```

---

## Part 4: Mtime Fallback Detector

**New file:** `src/jdocmunch_mcp/auto_refresh/mtime_detector.py`

Used for: non-git repos, and gitignored files within git repos.

```python
def detect_mtime_changes(source_path: str, doc_file_metas: dict) -> ChangeSet:
    """
    doc_file_metas: {rel_path: {"mtime": float, "size": int}}
    Scans all doc files in source_path, returns changed/deleted sets.
    """
    changed = set()
    deleted = set()
    current_files = _scan_doc_files(source_path)  # returns set of rel_paths

    for rel_path, meta in doc_file_metas.items():
        abs_path = Path(source_path) / rel_path
        if not abs_path.exists():
            deleted.add(rel_path)
            continue
        try:
            stat = abs_path.stat()
            if stat.st_mtime != meta["mtime"] or stat.st_size != meta["size"]:
                changed.add(rel_path)
        except OSError:
            deleted.add(rel_path)

    # New files not in the index yet
    indexed = set(doc_file_metas.keys())
    for rel_path in current_files - indexed:
        changed.add(rel_path)

    return ChangeSet(modified=changed - deleted, deleted=deleted, new_commit=None)
```

**Same-second double-edit:** mtime alone can miss this. Checking both mtime AND size catches the
common case. For the rare case where content changes but size stays the same within one second,
the next tool call will still see a clean mtime/size — accept this as a known limitation. Using
SHA-256 hashing for every file on every pre-call would add too much latency.

**New file detection:** `_scan_doc_files` uses the same extension filter as `discover_doc_files`
in `index_local.py`. New files show up as present in `current_files` but absent from `indexed`.

---

## Part 5: Incremental Re-index

**New file:** `src/jdocmunch_mcp/auto_refresh/incremental.py`

### 5.1 Re-parse only changed files

```python
def reindex_changed_files(
    index: DocIndex,
    source_path: str,
    modified: set[str],
    deleted: set[str],
    new_commit: Optional[str],
    store: DocStore,
) -> DocIndex:
    """
    Synchronous, no AI. Updates byte offsets and section structure.
    Returns updated DocIndex.
    """
    owner, name = index.owner, index.name
    repo_id = f"{owner}/{name}"

    # Remove sections for deleted and modified files (will be re-added)
    files_to_remove = deleted | modified
    surviving_sections = [
        s for s in index.sections
        if s.get("doc_path") not in files_to_remove
    ]

    # Re-parse modified files
    new_sections = []
    new_raw_files = {}
    new_file_metas = {}

    for rel_path in modified:
        abs_path = Path(source_path) / rel_path
        if not abs_path.exists():
            continue  # became deleted between detection and now
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        try:
            parsed_content = preprocess_content(content, rel_path)
            sections = parse_file(parsed_content, rel_path, repo_id)
        except Exception:
            continue

        # Preserve existing summaries for sections whose heading hasn't changed
        old_sections_for_file = {
            s["title"]: s.get("summary", "")
            for s in index.sections
            if s.get("doc_path") == rel_path
        }
        for sec in sections:
            if sec.title in old_sections_for_file and old_sections_for_file[sec.title]:
                sec.summary = old_sections_for_file[sec.title]  # reuse cached summary

        new_sections.extend(sections)
        new_raw_files[rel_path] = parsed_content

        stat = abs_path.stat()
        new_file_metas[rel_path] = {
            "sha256": _file_hash(parsed_content),
            "mtime": stat.st_mtime,
            "size": stat.st_size,
        }

    # Merge: surviving + new
    all_sections_dicts = surviving_sections  # already dicts
    all_sections_objs = new_sections         # Section objects, need summarization

    # Apply Tier 1 + Tier 3 summarization synchronously for sections with no summary
    # (Tier 2 / AI runs async — see Part 6)
    for sec in all_sections_objs:
        if not sec.summary:
            sec.summary = heading_summary(sec) or title_fallback(sec)

    all_sections_dicts.extend([s.to_dict() for s in all_sections_objs])

    # Update file_hashes: remove deleted, update modified, keep unchanged
    updated_file_hashes = {
        k: v for k, v in index.file_hashes.items()
        if k not in files_to_remove
    }
    updated_file_hashes.update(new_file_metas)

    # Also persist the changed raw files to content cache
    content_dir = store._content_dir(owner, name)
    for rel_path, content in new_raw_files.items():
        dest = store._safe_content_path(content_dir, rel_path)
        if dest:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(content.encode("utf-8"))

    # Delete removed files from content cache
    for rel_path in deleted:
        dest = store._safe_content_path(content_dir, rel_path)
        if dest and dest.exists():
            dest.unlink()

    # Build updated DocIndex
    updated_doc_paths = [
        p for p in index.doc_paths if p not in deleted
    ]
    for rel_path in modified:
        if rel_path not in updated_doc_paths:
            updated_doc_paths.append(rel_path)
    updated_doc_paths = sorted(updated_doc_paths)

    updated_index = DocIndex(
        repo=index.repo,
        owner=owner,
        name=name,
        indexed_at=datetime.now().isoformat(),
        doc_paths=updated_doc_paths,
        doc_types=index.doc_types,   # approximate — good enough
        sections=all_sections_dicts,
        index_version=INDEX_VERSION,
        file_hashes=updated_file_hashes,
        source_path=source_path,
        last_indexed_commit=new_commit or index.last_indexed_commit,
    )

    # Atomic write
    index_path = store._index_path(owner, name)
    tmp_path = index_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(store._index_to_dict(updated_index), f, indent=2)
    tmp_path.replace(index_path)

    return updated_index
```

### 5.2 Summary reuse logic

When re-parsing a modified file, we check if any sections have the same heading text as before.
If yes, reuse the old summary. This avoids unnecessary AI calls for files where only content
changed under existing headings (the common case: fixing typos, updating examples, etc.).

AI is only needed for:
- Sections with new heading text
- Brand new files
- Sections where heading text < 20 chars (same threshold as original code)

---

## Part 6: Background AI Summarization

**New file:** `src/jdocmunch_mcp/auto_refresh/summarization_queue.py`

```python
import threading
from typing import Optional

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None


def queue_ai_summarization(
    index: DocIndex,
    sections_needing_ai: list,   # Section objects with short/missing summaries
    store: DocStore,
    owner: str,
    name: str,
):
    """
    Spawns a background daemon thread to run AI summarization.
    Non-blocking. If a thread is already running for this repo, skips
    (the next pre-call check will catch any remaining unsummarized sections).
    """
    def _run():
        try:
            summarizer = _create_summarizer()
            if not summarizer:
                return

            # Clear summaries so batch_summarize processes them
            for sec in sections_needing_ai:
                sec.summary = ""
            summarizer.summarize_batch(sections_needing_ai)

            # Re-load current index (may have been updated since we started)
            current_index = store.load_index(owner, name)
            if not current_index:
                return

            # Merge updated summaries by section ID
            summary_map = {sec.id: sec.summary for sec in sections_needing_ai}
            for sec_dict in current_index.sections:
                sid = sec_dict.get("id")
                if sid in summary_map and summary_map[sid]:
                    sec_dict["summary"] = summary_map[sid]

            # Atomic write
            index_path = store._index_path(owner, name)
            tmp_path = index_path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(store._index_to_dict(current_index), f, indent=2)
            tmp_path.replace(index_path)

        except Exception:
            pass  # Never crash the background thread

    t = threading.Thread(target=_run, daemon=True)
    t.start()
```

**Thread safety:** The background thread does a load-merge-atomic-write cycle. If two threads run
concurrently for the same repo (shouldn't happen normally, but possible if many tool calls come in
rapid succession), the atomic write means the last writer wins — no corruption. The worst outcome
is losing one batch of AI summaries, which will be regenerated on the next change.

---

## Part 7: Per-Repo Locking

**In:** `src/jdocmunch_mcp/auto_refresh/refresh_manager.py`

```python
import threading

_repo_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _get_repo_lock(repo_key: str) -> threading.Lock:
    with _locks_lock:
        if repo_key not in _repo_locks:
            _repo_locks[repo_key] = threading.Lock()
        return _repo_locks[repo_key]
```

The main `auto_refresh` function acquires `_get_repo_lock(repo_key)` with `blocking=False`.
If the lock is already held (another concurrent tool call is mid-refresh for the same repo),
skip the refresh and proceed — the concurrent refresh will finish and the index will be fresh.

```python
def auto_refresh(repo: str, storage_path: Optional[str]) -> None:
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index or not index.source_path:
        return  # Nothing to refresh: remote repo or no source_path stored

    lock = _get_repo_lock(f"{owner}/{name}")
    if not lock.acquire(blocking=False):
        return  # Another refresh in progress, skip

    try:
        _do_refresh(index, store, owner, name)
    finally:
        lock.release()
```

---

## Part 8: Hook Into server.py

**File:** `src/jdocmunch_mcp/server.py`

In `call_tool`, add auto-refresh before dispatching read tools:

```python
READ_TOOLS = {
    "get_toc", "get_toc_tree", "get_document_outline",
    "search_sections", "get_section", "get_sections"
}

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    storage_path = os.environ.get("DOC_INDEX_PATH")

    # Auto-refresh before read tools
    if name in READ_TOOLS and "repo" in arguments:
        auto_refresh(arguments["repo"], storage_path)

    try:
        # ... existing dispatch logic unchanged ...
```

`auto_refresh` is synchronous and runs in the same thread. Since the MCP server uses asyncio,
wrap it to avoid blocking the event loop:

```python
import asyncio
from .auto_refresh import auto_refresh as _auto_refresh

async def _refresh(repo: str, storage_path: Optional[str]):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _auto_refresh, repo, storage_path)
```

Then `await _refresh(arguments["repo"], storage_path)` before dispatch.

---

## Part 9: Edge Cases and Mitigations

| Edge Case | Detection | Mitigation |
|---|---|---|
| File mid-write when pre-call triggers | mtime changed, content partial | Accept stale read; next call corrects it |
| Same-second double edit (same mtime) | mtime miss | Size check catches most cases |
| File deleted between detection and re-parse | `abs_path.exists()` check | Skip silently, remove from index |
| git not installed | `FileNotFoundError` on subprocess | Caught → fall back to mtime |
| `git rev-parse HEAD` fails (empty repo, bare repo) | Non-zero returncode | Treated as non-git, use mtime |
| `git status` timeout (huge repo) | `subprocess.TimeoutExpired` | Caught → fall back to mtime for that call |
| Detached HEAD | Works normally | No special handling needed |
| Shallow clone | Works normally | No special handling needed |
| git pull brings many changed files | Large `diff --name-only` output | All files re-parsed; first call slower, subsequent calls fast |
| Branch switch | HEAD changes → triggers commit diff | Correct files re-indexed |
| `git reset --hard` | HEAD changes → triggers commit diff | Same handling as branch switch |
| Gitignored files in watched dir | Not in git status output | Detected via directory scan, mtime fallback |
| Submodule inside watched dir | git status shows `M submodule` (not file list) | Run separate `git status` scoped to submodule path |
| New file added | git `??` status | Caught by `get_status_changes` |
| File renamed | git `R` status | Old path deleted, new path added |
| Concurrent tool calls (same repo) | `lock.acquire(blocking=False)` | Second caller skips refresh, uses current index |
| Background AI thread crashes | `except Exception: pass` | Thread dies silently; summaries stay as heading text |
| Disk full during atomic write | `OSError` on `open(tmp_path)` | Caught; old index preserved |
| Orphaned `.json.tmp` files | Startup cleanup | On `DocStore.__init__`, glob and remove `*.json.tmp` |
| Index version mismatch (old index loaded) | `index_version < 2` | Treat file_hashes as strings, trigger full re-index once |
| source_path points to deleted folder | `Path(source_path).exists()` | Skip auto-refresh silently |
| Two folders with same name (index collision) | Existing limitation, not introduced | Out of scope |
| `.git` directory in watched folder | git already excludes it | `discover_doc_files` also has `SKIP_PATTERNS` |
| Binary file with doc extension | `read_text` with `errors="replace"` | Parse produces garbage sections; bounded damage |

---

## Part 10: Files Changed / Created

### Modified files

| File | Changes |
|---|---|
| `src/jdocmunch_mcp/storage/doc_store.py` | Add `source_path`, `last_indexed_commit` to `DocIndex`; update `file_hashes` format; bump `INDEX_VERSION`; orphaned tmp cleanup in `__init__` |
| `src/jdocmunch_mcp/tools/index_local.py` | Capture git HEAD at index time; pass `source_path` and `last_indexed_commit` to `save_index`; store mtime+size in `file_hashes` |
| `src/jdocmunch_mcp/server.py` | Add `auto_refresh` call before read tool dispatch; wrap in `run_in_executor` |

### New files

| File | Purpose |
|---|---|
| `src/jdocmunch_mcp/auto_refresh/__init__.py` | Exports `auto_refresh` |
| `src/jdocmunch_mcp/auto_refresh/refresh_manager.py` | Top-level orchestration, per-repo locking |
| `src/jdocmunch_mcp/auto_refresh/git_detector.py` | All git subprocess logic |
| `src/jdocmunch_mcp/auto_refresh/mtime_detector.py` | mtime+size fallback |
| `src/jdocmunch_mcp/auto_refresh/incremental.py` | Re-parse changed files, merge into index |
| `src/jdocmunch_mcp/auto_refresh/summarization_queue.py` | Background AI thread |

### Unchanged files

Everything else: parsers, security, `index_repo`, all tool implementations, summarizer core.

---

## Part 11: Testing Plan

### Unit tests (new file: `tests/test_auto_refresh.py`)

1. `test_git_detector_no_git` — non-git dir → falls back to mtime cleanly
2. `test_git_detector_clean_repo` — no changes → empty ChangeSet
3. `test_git_detector_modified_file` — mock `git status` output → correct file in modified set
4. `test_git_detector_deleted_file` — `D` status → file in deleted set
5. `test_git_detector_renamed_file` — `R` status → old in deleted, new in modified
6. `test_git_detector_commit_diff` — mock `git diff --name-only` → changed files from commit range
7. `test_mtime_detector_no_change` — same mtime/size → empty ChangeSet
8. `test_mtime_detector_changed` — bumped mtime → file in modified
9. `test_mtime_detector_new_file` — file on disk not in index → in modified
10. `test_mtime_detector_deleted` — file in index not on disk → in deleted
11. `test_incremental_reindex_modified` — one file changed → only that file's sections replaced
12. `test_incremental_reindex_summary_reuse` — heading unchanged → old summary preserved
13. `test_incremental_reindex_deleted` — file deleted → its sections removed
14. `test_atomic_write` — interrupt mid-write (mock) → old index intact
15. `test_concurrent_calls_same_repo` — two threads hit refresh simultaneously → no corruption
16. `test_background_ai_thread_crash` — AI raises exception → no crash, fallback summaries intact
17. `test_auto_refresh_no_source_path` — remote repo index → refresh skipped cleanly
18. `test_docindex_v1_compat` — load old-format index → file_hashes strings handled correctly

### Integration tests (additions to `tests/test_tools.py`)

1. `test_auto_refresh_on_get_section` — index a folder, modify a file, call `get_section` → returns updated content
2. `test_auto_refresh_on_search` — index, add new file, call `search_sections` → new file's sections appear
3. `test_auto_refresh_git_pull_simulation` — index at commit A, advance HEAD to commit B, call tool → re-indexed automatically

---

## Part 12: Implementation Order

1. `doc_store.py` — add fields, update serialization, bump version, backwards compat
2. `index_local.py` — capture source_path and git commit at index time
3. `auto_refresh/git_detector.py` — git subprocess logic with all edge case handling
4. `auto_refresh/mtime_detector.py` — mtime fallback
5. `auto_refresh/incremental.py` — merge logic + summary reuse
6. `auto_refresh/summarization_queue.py` — background AI thread
7. `auto_refresh/refresh_manager.py` — orchestration + per-repo locking
8. `auto_refresh/__init__.py` — export
9. `server.py` — hook in the pre-call refresh
10. Tests — unit then integration

Each step is independently testable before moving to the next.
