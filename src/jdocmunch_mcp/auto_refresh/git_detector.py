"""Git-based change detection for auto-refresh."""

import subprocess
import time
from pathlib import Path
from typing import Optional

from ._types import ChangeSet
from ._scan import scan_doc_files

# Cache is_git_repo results per path with a TTL to handle git init/rm after server start
_git_repo_cache: dict = {}   # {path: (value, timestamp)}
_GIT_CACHE_TTL = 300.0       # re-check after 5 minutes


def is_git_repo(path: str) -> bool:
    """Check if path is inside a git working tree."""
    cached = _git_repo_cache.get(path)
    if cached is not None and (time.monotonic() - cached[1]) < _GIT_CACHE_TTL:
        return cached[0]
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5
        )
        val = result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        val = False
    _git_repo_cache[path] = (val, time.monotonic())
    return val


def _get_repo_root(path: str) -> Optional[str]:
    """Return the absolute path of the git repository root containing path."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _strip_repo_prefix(paths: set, source_path: str, repo_root: str) -> set:
    """Strip the repo-root prefix from git output paths.

    git status/diff return paths relative to the repo root, but source_path
    may be a subdirectory. Strip so paths match what the index stores.
    Files outside source_path are silently dropped.
    """
    try:
        prefix = Path(source_path).resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError:
        return paths
    if prefix == ".":
        return paths
    prefix_slash = prefix + "/"
    return {p[len(prefix_slash):] for p in paths if p.startswith(prefix_slash)}


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


def get_commit_diff_files(path: str, old_commit: str, new_commit: str) -> set:
    """Return relative paths of files changed between old_commit and new_commit."""
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


def get_status_changes(path: str) -> dict:
    """Return {relative_path: status_code} for all working tree changes.

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

            primary = xy[0] if xy[0] != " " else xy[1]
            if primary == "?":
                status = "?"
            else:
                status = primary

            changes[file_part.strip()] = status

        return changes
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}


def detect_git_changes(
    source_path: str,
    last_commit: Optional[str],
    indexed_file_metas: dict,
) -> ChangeSet:
    """Detect changed and deleted files using git.

    indexed_file_metas: {rel_path: {"sha256": ..., "mtime": float, "size": int}}
    Returns ChangeSet with modified and deleted sets.
    """
    changed: set = set()
    deleted: set = set()

    current_commit = get_head_commit(source_path)

    # Compute repo root once — needed to strip the subdir prefix from git output paths.
    # git status/diff return paths relative to the repo root, not source_path.
    repo_root = _get_repo_root(source_path)

    def _strip(paths: set) -> set:
        if repo_root:
            return _strip_repo_prefix(paths, source_path, repo_root)
        return paths

    def _strip_dict(d: dict) -> dict:
        if not repo_root:
            return d
        result = {}
        for k, v in d.items():
            stripped = _strip_repo_prefix({k}, source_path, repo_root)
            if stripped:
                result[stripped.pop()] = v
        return result

    # 1. Committed changes since last index
    if last_commit and current_commit and current_commit != last_commit:
        diff_files = _strip(get_commit_diff_files(source_path, last_commit, current_commit))
        changed.update(diff_files)

    # 2. Uncommitted working tree changes
    status = _strip_dict(get_status_changes(source_path))
    for rel_path, code in status.items():
        if code == "D":
            deleted.add(rel_path)
        else:
            changed.add(rel_path)

    # 3. Gitignored files → mtime fallback
    # git ls-files returns paths relative to the -C directory (source_path), no stripping needed
    all_doc_files = scan_doc_files(source_path)
    git_known = set(status.keys())
    if current_commit:
        try:
            result = subprocess.run(
                ["git", "-C", source_path, "ls-files"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                git_known.update(line.strip() for line in result.stdout.splitlines() if line.strip())
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    gitignored = {f for f in all_doc_files if f not in git_known}
    mtime_changed = _mtime_check(source_path, gitignored, indexed_file_metas)
    changed.update(mtime_changed)

    return ChangeSet(
        modified=changed - deleted,
        deleted=deleted,
        new_commit=current_commit,
    )


def _mtime_check(source_path: str, rel_paths: set, indexed_file_metas: dict) -> set:
    """Check mtime+size for a given set of relative paths.

    Returns the subset that have changed or are new.
    """
    changed = set()
    root = Path(source_path)

    for rel_path in rel_paths:
        abs_path = root / rel_path
        meta = indexed_file_metas.get(rel_path)
        if meta is None:
            # Not in index at all — new file
            if abs_path.exists():
                changed.add(rel_path)
            continue
        try:
            stat = abs_path.stat()
            if stat.st_mtime != meta.get("mtime", 0.0) or stat.st_size != meta.get("size", 0):
                changed.add(rel_path)
        except OSError:
            pass  # deletion handled by git status

    return changed
