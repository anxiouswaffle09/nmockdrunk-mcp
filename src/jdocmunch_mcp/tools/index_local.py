"""Index local folder tool — walk, parse, summarize, save."""

import hashlib
import subprocess
import time
from pathlib import Path
from typing import Optional

from ..auto_refresh._scan import discover_doc_rel_paths
from ..parser import parse_file, preprocess_content
from ..security import (
    validate_path,
    DEFAULT_MAX_FILE_SIZE,
)
from ..storage import DocStore
from ..summarizer import summarize_sections


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


def discover_doc_files(
    folder_path: Path,
    max_files: int = 500,
    max_size: int = DEFAULT_MAX_FILE_SIZE,
    extra_ignore_patterns: Optional[list] = None,
    follow_symlinks: bool = False,
) -> tuple:
    """Discover doc files (.md, .txt, .rst) with security filtering."""
    rel_paths, warnings = discover_doc_rel_paths(
        source_path=folder_path,
        max_files=max_files,
        max_size=max_size,
        extra_ignore_patterns=extra_ignore_patterns,
        follow_symlinks=follow_symlinks,
        collect_warnings=True,
    )
    root = folder_path.resolve()
    return [root / rel_path for rel_path in rel_paths], warnings


def index_local(
    path: str,
    storage_path: Optional[str] = None,
    extra_ignore_patterns: Optional[list] = None,
    follow_symlinks: bool = False,
) -> dict:
    """Index a local folder containing documentation files.

    Args:
        path: Path to local folder.
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
    store = DocStore(base_path=storage_path)
    owner, repo_name = store.resolve_local_repo(str(folder_path))
    repo_id = f"{owner}/{repo_name}"

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

        all_sections = summarize_sections(all_sections)

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

        saved = store.save_index(
            owner=owner,
            name=repo_name,
            sections=all_sections,
            raw_files=raw_files,
            doc_types=doc_types,
            file_hashes=file_hashes,
            source_path=str(folder_path),
            last_indexed_commit=last_commit,
            extra_ignore_patterns=extra_ignore_patterns,
            follow_symlinks=follow_symlinks,
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
