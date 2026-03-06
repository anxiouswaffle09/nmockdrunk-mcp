"""Index local folder tool — walk, parse, summarize, save."""

import hashlib
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

import pathspec

from ..parser import parse_file, preprocess_content, ALL_EXTENSIONS
from ..security import (
    validate_path,
    is_symlink_escape,
    is_secret_file,
    should_exclude_file,
    DEFAULT_MAX_FILE_SIZE,
)
from ..storage import DocStore
from ..summarizer import summarize_sections
from ._constants import SKIP_PATTERNS


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


def _load_gitignore(folder_path: Path) -> Optional[pathspec.PathSpec]:
    gitignore_path = folder_path / ".gitignore"
    if gitignore_path.is_file():
        try:
            content = gitignore_path.read_text(encoding="utf-8", errors="replace")
            return pathspec.PathSpec.from_lines("gitignore", content.splitlines())
        except Exception:
            pass
    return None


def _should_skip(rel_path: str) -> bool:
    normalized = "/" + rel_path.replace("\\", "/")
    for pat in SKIP_PATTERNS:
        if ("/" + pat) in normalized:
            return True
    return False


def discover_doc_files(
    folder_path: Path,
    max_files: int = 500,
    max_size: int = DEFAULT_MAX_FILE_SIZE,
    extra_ignore_patterns: Optional[list] = None,
    follow_symlinks: bool = False,
) -> tuple:
    """Discover doc files (.md, .txt, .rst) with security filtering."""
    files = []
    warnings = []
    root = folder_path.resolve()

    gitignore_spec = _load_gitignore(root)
    extra_spec = None
    if extra_ignore_patterns:
        try:
            extra_spec = pathspec.PathSpec.from_lines("gitignore", extra_ignore_patterns)
        except Exception:
            pass

    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        dir_path = Path(dirpath)
        try:
            dir_rel = dir_path.relative_to(root).as_posix()
        except ValueError:
            dirnames.clear()
            continue

        # Prune skipped directories in-place so os.walk won't descend into them
        dirnames[:] = [
            d for d in dirnames
            if not _should_skip(f"{dir_rel}/{d}/".lstrip("./"))
            and not (gitignore_spec and gitignore_spec.match_file(f"{dir_rel}/{d}/".lstrip("./")))
            and not (extra_spec and extra_spec.match_file(f"{dir_rel}/{d}/".lstrip("./")))
        ]

        for filename in filenames:
            file_path = dir_path / filename

            if not follow_symlinks and file_path.is_symlink():
                continue
            if file_path.is_symlink() and is_symlink_escape(root, file_path):
                warnings.append(f"Skipped symlink escape: {file_path}")
                continue

            if not validate_path(root, file_path):
                warnings.append(f"Skipped path traversal: {file_path}")
                continue

            rel_path = f"{dir_rel}/{filename}".lstrip("./") if dir_rel != "." else filename

            if _should_skip(rel_path):
                continue

            if gitignore_spec and gitignore_spec.match_file(rel_path):
                continue

            if extra_spec and extra_spec.match_file(rel_path):
                continue

            if is_secret_file(rel_path):
                warnings.append(f"Skipped secret file: {rel_path}")
                continue

            ext = file_path.suffix.lower()
            if ext not in ALL_EXTENSIONS:
                continue

            try:
                if file_path.stat().st_size > max_size:
                    continue
            except OSError:
                continue

            files.append(file_path)

        if len(files) >= max_files:
            break

    return files[:max_files], warnings


def index_local(
    path: str,
    use_ai_summaries: bool = True,
    storage_path: Optional[str] = None,
    extra_ignore_patterns: Optional[list] = None,
    follow_symlinks: bool = False,
) -> dict:
    """Index a local folder containing documentation files.

    Args:
        path: Path to local folder.
        use_ai_summaries: Whether to use AI for section summaries.
        storage_path: Custom storage path (default: ~/.doc-index/).
        extra_ignore_patterns: Additional gitignore-style patterns to exclude.
        follow_symlinks: Whether to follow symlinks.

    Returns:
        Dict with indexing results.
    """
    t0 = time.time()
    folder_path = Path(path).expanduser().resolve()

    if not folder_path.exists():
        return {"success": False, "error": f"Folder not found: {path}"}
    if not folder_path.is_dir():
        return {"success": False, "error": f"Path is not a directory: {path}"}

    warnings = []

    try:
        doc_files, discover_warnings = discover_doc_files(
            folder_path,
            extra_ignore_patterns=extra_ignore_patterns,
            follow_symlinks=follow_symlinks,
        )
        warnings.extend(discover_warnings)

        if not doc_files:
            return {"success": False, "error": "No documentation files found"}

        all_sections = []
        doc_types: dict = {}
        raw_files: dict = {}
        parsed_files = []

        for file_path in doc_files:
            if not validate_path(folder_path, file_path):
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                warnings.append(f"Failed to read {file_path}: {e}")
                continue

            try:
                rel_path = file_path.relative_to(folder_path).as_posix()
            except ValueError:
                continue

            ext = file_path.suffix.lower()
            repo_name = folder_path.name
            owner = "local"
            repo_id = f"{owner}/{repo_name}"

            try:
                parsed_content = preprocess_content(content, rel_path)
                sections = parse_file(parsed_content, rel_path, repo_id)
                if sections:
                    all_sections.extend(sections)
                    doc_types[ext] = doc_types.get(ext, 0) + 1
                    raw_files[rel_path] = parsed_content
                    parsed_files.append(rel_path)
            except Exception as e:
                warnings.append(f"Failed to parse {rel_path}: {e}")
                continue

        if not all_sections:
            return {"success": False, "error": "No sections extracted from files"}

        all_sections = summarize_sections(all_sections, use_ai=use_ai_summaries)

        repo_name = folder_path.name
        owner = "local"

        # Build file_hashes with mtime+size for auto-refresh change detection
        file_hashes = {}
        for rel_path, content in raw_files.items():
            abs_path = folder_path / rel_path
            try:
                stat = abs_path.stat()
                file_hashes[rel_path] = {
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                }
            except OSError:
                file_hashes[rel_path] = {
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "mtime": 0.0,
                    "size": 0,
                }

        last_commit = _get_git_commit(str(folder_path))

        store = DocStore(base_path=storage_path)
        saved = store.save_index(
            owner=owner,
            name=repo_name,
            sections=all_sections,
            raw_files=raw_files,
            doc_types=doc_types,
            file_hashes=file_hashes,
            source_path=str(folder_path),
            last_indexed_commit=last_commit,
        )

        latency_ms = int((time.time() - t0) * 1000)
        result = {
            "success": True,
            "repo": f"{owner}/{repo_name}",
            "folder_path": str(folder_path),
            "indexed_at": saved.indexed_at,
            "file_count": len(parsed_files),
            "section_count": len(all_sections),
            "doc_types": doc_types,
            "files": parsed_files[:20],
            "_meta": {"latency_ms": latency_ms},
        }

        if warnings:
            result["warnings"] = warnings
        if len(doc_files) >= 500:
            result["note"] = "Folder has many files; indexed first 500"

        return result

    except Exception as e:
        return {"success": False, "error": f"Indexing failed: {str(e)}"}
