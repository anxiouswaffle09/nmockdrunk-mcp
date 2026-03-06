"""Shared doc-file scanner for auto-refresh."""

import os
from pathlib import Path

import pathspec

from ..parser import ALL_EXTENSIONS
from ..security import (
    DEFAULT_MAX_FILE_SIZE,
    is_symlink_escape,
    should_exclude_file,
    validate_path,
)
from ..tools._constants import SKIP_PATTERNS


def _load_gitignore(folder_path: Path) -> pathspec.PathSpec | None:
    gitignore_path = folder_path / ".gitignore"
    if gitignore_path.is_file():
        try:
            content = gitignore_path.read_text(encoding="utf-8", errors="replace")
            return pathspec.PathSpec.from_lines("gitignore", content.splitlines())
        except Exception:
            return None
    return None


def discover_doc_rel_paths(
    source_path: str | Path,
    max_files: int | None = None,
    max_size: int = DEFAULT_MAX_FILE_SIZE,
    extra_ignore_patterns: list[str] | None = None,
    follow_symlinks: bool = False,
    collect_warnings: bool = False,
) -> tuple[list[str], list[str]]:
    """Discover relative doc paths under source_path using local indexing rules."""
    root = Path(source_path).resolve()
    found: list[str] = []
    warnings: list[str] = []
    gitignore_spec = _load_gitignore(root)
    extra_spec = None
    if extra_ignore_patterns:
        try:
            extra_spec = pathspec.PathSpec.from_lines("gitignore", extra_ignore_patterns)
        except Exception:
            extra_spec = None

    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        dir_path = Path(dirpath)
        try:
            dir_rel = dir_path.relative_to(root).as_posix()
        except ValueError:
            dirnames.clear()
            continue

        pruned_dirnames = []
        for dirname in dirnames:
            rel_dir = f"{dir_rel}/{dirname}/".lstrip("./")
            dir_candidate = dir_path / dirname

            if _should_skip(rel_dir):
                continue
            if gitignore_spec and gitignore_spec.match_file(rel_dir):
                continue
            if extra_spec and extra_spec.match_file(rel_dir):
                continue
            if not follow_symlinks and dir_candidate.is_symlink():
                continue
            if dir_candidate.is_symlink() and is_symlink_escape(root, dir_candidate):
                if collect_warnings:
                    warnings.append(f"Skipped symlink escape: {dir_candidate}")
                continue
            if not validate_path(root, dir_candidate):
                if collect_warnings:
                    warnings.append(f"Skipped path traversal: {dir_candidate}")
                continue

            pruned_dirnames.append(dirname)

        dirnames[:] = pruned_dirnames

        for filename in filenames:
            file_path = dir_path / filename
            if not follow_symlinks and file_path.is_symlink():
                continue
            if file_path.is_symlink() and is_symlink_escape(root, file_path):
                if collect_warnings:
                    warnings.append(f"Skipped symlink escape: {file_path}")
                continue
            if not validate_path(root, file_path):
                if collect_warnings:
                    warnings.append(f"Skipped path traversal: {file_path}")
                continue

            ext = Path(filename).suffix.lower()
            if ext not in ALL_EXTENSIONS:
                continue

            rel_path = f"{dir_rel}/{filename}".lstrip("./") if dir_rel != "." else filename
            if _should_skip(rel_path):
                continue
            if gitignore_spec and gitignore_spec.match_file(rel_path):
                continue
            if extra_spec and extra_spec.match_file(rel_path):
                continue

            exclusion_reason = should_exclude_file(
                file_path,
                root,
                max_file_size=max_size,
                check_secrets=True,
                check_binary=True,
                check_symlinks=follow_symlinks,
            )
            if exclusion_reason:
                if collect_warnings and exclusion_reason == "secret_file":
                    warnings.append(f"Skipped secret file: {rel_path}")
                continue

            found.append(rel_path)
            if max_files is not None and len(found) >= max_files:
                break

        if max_files is not None and len(found) >= max_files:
            break

    return found, warnings


def scan_doc_files(
    source_path: str,
    max_size: int = DEFAULT_MAX_FILE_SIZE,
    extra_ignore_patterns: list[str] | None = None,
    follow_symlinks: bool = False,
) -> set:
    """Return set of relative paths for all doc files under source_path.

    Applies the same extension, skip, gitignore, symlink, secret-file, and
    size rules as local indexing.
    """
    found, _ = discover_doc_rel_paths(
        source_path=source_path,
        max_files=None,
        max_size=max_size,
        extra_ignore_patterns=extra_ignore_patterns,
        follow_symlinks=follow_symlinks,
        collect_warnings=False,
    )
    return set(found)


def _should_skip(rel_path: str) -> bool:
    normalized = "/" + rel_path.replace("\\", "/")
    for pat in SKIP_PATTERNS:
        if ("/" + pat) in normalized:
            return True
    return False
