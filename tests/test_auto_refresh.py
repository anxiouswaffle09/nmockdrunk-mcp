"""Tests for auto-refresh module."""

import json
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from jdocmunch_mcp.storage.doc_store import DocStore, DocIndex, INDEX_VERSION
from jdocmunch_mcp.parser import parse_file
from jdocmunch_mcp.auto_refresh._types import ChangeSet
from jdocmunch_mcp.auto_refresh.git_detector import (
    is_git_repo, get_status_changes, get_commit_diff_files, detect_git_changes,
    _git_repo_cache,
)
from jdocmunch_mcp.auto_refresh.mtime_detector import detect_mtime_changes
from jdocmunch_mcp.auto_refresh.incremental import reindex_changed_files
from jdocmunch_mcp.auto_refresh.refresh_manager import auto_refresh
from jdocmunch_mcp.tools.index_local import index_local
from jdocmunch_mcp.tools.get_section import get_section
from jdocmunch_mcp.tools.search_sections import search_sections
from jdocmunch_mcp.tools.get_toc import get_toc

FIXTURES = Path(__file__).parent / "fixtures"

SAMPLE_MD = """# Root

Intro content.

## Section A

Content for A.

## Section B

Content for B.
"""

UPDATED_MD = """# Root

Intro content.

## Section A

Updated content for A — version 2.

## Section B

Content for B.
"""

NEW_FILE_MD = """# New File

## Brand New Section

This section was added after initial indexing.
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_store_with_index(tmp_path, md_content=SAMPLE_MD, use_git=False):
    """Create a docs folder and index it; return (store, index, docs_dir)."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "README.md").write_text(md_content, encoding="utf-8")

    store = DocStore(base_path=str(tmp_path / "store"))
    sections = parse_file(md_content, "README.md", "local/docs")
    for sec in sections:
        if not sec.summary:
            sec.summary = sec.title

    stat = (docs_dir / "README.md").stat()
    file_hashes = {
        "README.md": {
            "sha256": "abc",
            "mtime": stat.st_mtime,
            "size": stat.st_size,
        }
    }

    index = store.save_index(
        owner="local",
        name="docs",
        sections=sections,
        raw_files={"README.md": md_content},
        doc_types={".md": 1},
        file_hashes=file_hashes,
        source_path=str(docs_dir),
        last_indexed_commit=None,
    )
    return store, index, docs_dir


# ── Unit tests: git_detector ─────────────────────────────────────────────────

class TestGitDetectorNoGit:
    def test_is_git_repo_non_git_dir(self, tmp_path):
        # Clear cache first
        _git_repo_cache.clear()
        result = is_git_repo(str(tmp_path))
        assert result is False

    def test_detect_git_changes_falls_back_gracefully(self, tmp_path):
        """Non-git dir: detect_git_changes should not raise."""
        _git_repo_cache.clear()
        (tmp_path / "README.md").write_text("# Hello\n## Sec\ncontent")
        # Provide some indexed metas so scan_doc_files can find files
        result = detect_git_changes(
            source_path=str(tmp_path),
            last_commit=None,
            indexed_file_metas={},
        )
        assert isinstance(result, ChangeSet)


class TestGitDetectorCleanRepo:
    def test_no_changes_empty_changeset(self, tmp_path):
        """If git status is empty and HEAD == last_commit, nothing changed."""
        _git_repo_cache.clear()
        with patch("subprocess.run") as mock_run:
            # is_git_repo → True
            # get_head_commit → "abc123"
            # get_commit_diff_files → no output (same commit)
            # get_status_changes → empty
            # git ls-files → empty

            def side_effect(cmd, **kwargs):
                r = MagicMock()
                if "--is-inside-work-tree" in cmd:
                    r.returncode = 0
                    r.stdout = "true\n"
                elif "rev-parse" in cmd and "HEAD" in cmd:
                    r.returncode = 0
                    r.stdout = "abc123\n"
                elif "status" in cmd:
                    r.returncode = 0
                    r.stdout = ""
                elif "ls-files" in cmd:
                    r.returncode = 0
                    r.stdout = ""
                else:
                    r.returncode = 0
                    r.stdout = ""
                return r

            mock_run.side_effect = side_effect

            result = detect_git_changes(
                source_path=str(tmp_path),
                last_commit="abc123",
                indexed_file_metas={},
            )
            assert len(result.modified) == 0
            assert len(result.deleted) == 0


class TestGitDetectorModifiedFile:
    def test_modified_file_in_changeset(self, tmp_path):
        _git_repo_cache.clear()
        (tmp_path / "guide.md").write_text("# Guide\n## Sec\ncontent")

        with patch("subprocess.run") as mock_run:
            def side_effect(cmd, **kwargs):
                r = MagicMock()
                if "--is-inside-work-tree" in cmd:
                    r.returncode = 0; r.stdout = "true\n"
                elif "rev-parse" in cmd:
                    r.returncode = 0; r.stdout = "def456\n"
                elif "status" in cmd:
                    r.returncode = 0; r.stdout = " M guide.md\n"
                elif "ls-files" in cmd:
                    r.returncode = 0; r.stdout = "guide.md\n"
                else:
                    r.returncode = 0; r.stdout = ""
                return r

            mock_run.side_effect = side_effect
            _git_repo_cache[str(tmp_path)] = True

            result = detect_git_changes(
                source_path=str(tmp_path),
                last_commit="def456",
                indexed_file_metas={},
            )
            assert "guide.md" in result.modified


class TestGitDetectorDeletedFile:
    def test_deleted_file_in_changeset(self, tmp_path):
        _git_repo_cache.clear()
        with patch("subprocess.run") as mock_run:
            def side_effect(cmd, **kwargs):
                r = MagicMock()
                if "--is-inside-work-tree" in cmd:
                    r.returncode = 0; r.stdout = "true\n"
                elif "rev-parse" in cmd:
                    r.returncode = 0; r.stdout = "aaa\n"
                elif "status" in cmd:
                    r.returncode = 0; r.stdout = " D deleted.md\n"
                elif "ls-files" in cmd:
                    r.returncode = 0; r.stdout = ""
                else:
                    r.returncode = 0; r.stdout = ""
                return r

            mock_run.side_effect = side_effect
            _git_repo_cache[str(tmp_path)] = True

            result = detect_git_changes(
                source_path=str(tmp_path),
                last_commit="aaa",
                indexed_file_metas={},
            )
            assert "deleted.md" in result.deleted
            assert "deleted.md" not in result.modified


class TestGitDetectorRenamedFile:
    def test_renamed_old_deleted_new_modified(self, tmp_path):
        _git_repo_cache.clear()
        (tmp_path / "new.md").write_text("# New\n## Sec\ncontent")

        with patch("subprocess.run") as mock_run:
            def side_effect(cmd, **kwargs):
                r = MagicMock()
                if "--is-inside-work-tree" in cmd:
                    r.returncode = 0; r.stdout = "true\n"
                elif "rev-parse" in cmd:
                    r.returncode = 0; r.stdout = "bbb\n"
                elif "status" in cmd:
                    r.returncode = 0; r.stdout = "R  old.md -> new.md\n"
                elif "ls-files" in cmd:
                    r.returncode = 0; r.stdout = "new.md\n"
                else:
                    r.returncode = 0; r.stdout = ""
                return r

            mock_run.side_effect = side_effect
            _git_repo_cache[str(tmp_path)] = True

            result = detect_git_changes(
                source_path=str(tmp_path),
                last_commit="bbb",
                indexed_file_metas={},
            )
            assert "old.md" in result.deleted
            assert "new.md" in result.modified


class TestGitDetectorCommitDiff:
    def test_commit_diff_files_detected(self, tmp_path):
        _git_repo_cache.clear()
        (tmp_path / "changed.md").write_text("# Changed\n## Sec\ncontent")

        with patch("subprocess.run") as mock_run:
            def side_effect(cmd, **kwargs):
                r = MagicMock()
                if "--is-inside-work-tree" in cmd:
                    r.returncode = 0; r.stdout = "true\n"
                elif "rev-parse" in cmd:
                    r.returncode = 0; r.stdout = "new_commit\n"
                elif "diff" in cmd and "--name-only" in cmd:
                    r.returncode = 0; r.stdout = "changed.md\n"
                elif "status" in cmd:
                    r.returncode = 0; r.stdout = ""
                elif "ls-files" in cmd:
                    r.returncode = 0; r.stdout = "changed.md\n"
                else:
                    r.returncode = 0; r.stdout = ""
                return r

            mock_run.side_effect = side_effect
            _git_repo_cache[str(tmp_path)] = True

            result = detect_git_changes(
                source_path=str(tmp_path),
                last_commit="old_commit",
                indexed_file_metas={},
            )
            assert "changed.md" in result.modified
            assert result.new_commit == "new_commit"


# ── Unit tests: mtime_detector ────────────────────────────────────────────────

class TestMtimeDetectorNoChange:
    def test_no_change_empty_changeset(self, tmp_path):
        f = tmp_path / "README.md"
        f.write_text("# Hello\n## Sec\ncontent")
        stat = f.stat()

        result = detect_mtime_changes(
            source_path=str(tmp_path),
            doc_file_metas={
                "README.md": {"sha256": "x", "mtime": stat.st_mtime, "size": stat.st_size}
            },
        )
        assert len(result.modified) == 0
        assert len(result.deleted) == 0


class TestMtimeDetectorChanged:
    def test_changed_mtime_detected(self, tmp_path):
        f = tmp_path / "README.md"
        f.write_text("# Hello\n## Sec\ncontent")
        stat = f.stat()
        old_mtime = stat.st_mtime - 100  # simulate earlier mtime

        result = detect_mtime_changes(
            source_path=str(tmp_path),
            doc_file_metas={
                "README.md": {"sha256": "x", "mtime": old_mtime, "size": stat.st_size}
            },
        )
        assert "README.md" in result.modified


class TestMtimeDetectorNewFile:
    def test_new_file_in_modified(self, tmp_path):
        f = tmp_path / "new.md"
        f.write_text("# New\n## Sec\ncontent")

        result = detect_mtime_changes(
            source_path=str(tmp_path),
            doc_file_metas={},  # empty — no prior index
        )
        assert "new.md" in result.modified


class TestMtimeDetectorDeleted:
    def test_deleted_file_in_deleted_set(self, tmp_path):
        result = detect_mtime_changes(
            source_path=str(tmp_path),
            doc_file_metas={
                "vanished.md": {"sha256": "x", "mtime": 1234.0, "size": 100}
            },
        )
        assert "vanished.md" in result.deleted
        assert "vanished.md" not in result.modified


# ── Unit tests: incremental reindex ──────────────────────────────────────────

class TestIncrementalReindexModified:
    def test_only_modified_file_sections_replaced(self, tmp_path):
        store, index, docs_dir = _make_store_with_index(tmp_path)

        # Create a second file
        (docs_dir / "other.md").write_text("# Other\n## Other Sec\nother content")
        other_stat = (docs_dir / "other.md").stat()
        index.file_hashes["other.md"] = {"sha256": "y", "mtime": other_stat.st_mtime, "size": other_stat.st_size}
        index.sections.extend([
            s.to_dict() for s in parse_file("# Other\n## Other Sec\nother content", "other.md", "local/docs")[:1]
        ])

        # Modify README.md
        (docs_dir / "README.md").write_text(UPDATED_MD, encoding="utf-8")

        result = reindex_changed_files(
            index=index,
            source_path=str(docs_dir),
            modified={"README.md"},
            deleted=set(),
            new_commit=None,
            store=store,
        )

        updated_index, _ = result
        readme_sections = [s for s in updated_index.sections if s.get("doc_path") == "README.md"]
        other_sections = [s for s in updated_index.sections if s.get("doc_path") == "other.md"]

        # other.md sections unchanged
        assert len(other_sections) >= 1
        # README.md sections re-parsed
        assert len(readme_sections) >= 1


class TestIncrementalReindexSummaryReuse:
    def test_unchanged_heading_summary_preserved(self, tmp_path):
        store, index, docs_dir = _make_store_with_index(tmp_path)

        # Inject a distinctive summary into Section A
        for sec in index.sections:
            if sec.get("title") == "Section A":
                sec["summary"] = "Distinctive cached summary for Section A"

        # Modify README.md with same headings but different content
        (docs_dir / "README.md").write_text(UPDATED_MD, encoding="utf-8")

        result = reindex_changed_files(
            index=index,
            source_path=str(docs_dir),
            modified={"README.md"},
            deleted=set(),
            new_commit=None,
            store=store,
        )

        updated_index, _ = result
        sec_a = next(
            (s for s in updated_index.sections if s.get("title") == "Section A"),
            None
        )
        assert sec_a is not None
        assert sec_a["summary"] == "Distinctive cached summary for Section A"


class TestIncrementalReindexDeleted:
    def test_deleted_file_sections_removed(self, tmp_path):
        store, index, docs_dir = _make_store_with_index(tmp_path)

        result = reindex_changed_files(
            index=index,
            source_path=str(docs_dir),
            modified=set(),
            deleted={"README.md"},
            new_commit=None,
            store=store,
        )

        updated_index, _ = result
        readme_sections = [s for s in updated_index.sections if s.get("doc_path") == "README.md"]
        assert len(readme_sections) == 0
        assert "README.md" not in updated_index.doc_paths


class TestAtomicWrite:
    def test_no_tmp_files_after_successful_reindex(self, tmp_path):
        store, index, docs_dir = _make_store_with_index(tmp_path)
        (docs_dir / "README.md").write_text(UPDATED_MD, encoding="utf-8")

        reindex_changed_files(
            index=index,
            source_path=str(docs_dir),
            modified={"README.md"},
            deleted=set(),
            new_commit=None,
            store=store,
        )

        tmp_files = list(Path(str(tmp_path / "store")).glob("*/*.json.tmp"))
        assert len(tmp_files) == 0

    def test_old_index_preserved_on_disk_error(self, tmp_path):
        store, index, docs_dir = _make_store_with_index(tmp_path)
        (docs_dir / "README.md").write_text(UPDATED_MD, encoding="utf-8")

        original_sections_count = len(index.sections)

        # Patch only the atomic JSON write step
        with patch("jdocmunch_mcp.auto_refresh.incremental.json.dump", side_effect=OSError("disk full")):
            result = reindex_changed_files(
                index=index,
                source_path=str(docs_dir),
                modified={"README.md"},
                deleted=set(),
                new_commit=None,
                store=store,
            )

        # On disk error, returns old index unchanged
        if isinstance(result, tuple):
            returned_index = result[0]
        else:
            returned_index = result
        assert len(returned_index.sections) == original_sections_count


class TestConcurrentCalls:
    def test_two_threads_no_corruption(self, tmp_path):
        """Two concurrent auto_refresh calls for the same repo must not corrupt the index."""
        store, index, docs_dir = _make_store_with_index(tmp_path)
        storage_path = str(tmp_path / "store")

        errors = []
        results = []

        def do_refresh():
            try:
                auto_refresh("local/docs", storage_path)
                loaded = store.load_index("local", "docs")
                if loaded:
                    results.append(len(loaded.sections))
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=do_refresh)
        t2 = threading.Thread(target=do_refresh)
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert not errors
        # Index should still be loadable
        final = store.load_index("local", "docs")
        assert final is not None


class TestBackgroundAiThreadCrash:
    def test_ai_crash_does_not_propagate(self, tmp_path):
        """If AI summarization crashes, it must not affect the main thread or index."""
        from jdocmunch_mcp.auto_refresh.summarization_queue import queue_ai_summarization

        store, index, docs_dir = _make_store_with_index(tmp_path)

        # Create mock Section objects that will make AI crash
        class BadSection:
            id = "bad::id"
            summary = ""
            content = "content"
            title = "Bad"

        sections_needing_ai = [BadSection()]

        with patch("jdocmunch_mcp.auto_refresh.summarization_queue._create_summarizer") as mock_s:
            mock_s.return_value = MagicMock(
                summarize_batch=MagicMock(side_effect=RuntimeError("AI exploded"))
            )
            # Should not raise
            queue_ai_summarization(
                owner="local",
                name="docs",
                sections_needing_ai=sections_needing_ai,
                store=store,
            )
            time.sleep(0.05)  # Give daemon thread time to run and fail

        # Index still intact
        loaded = store.load_index("local", "docs")
        assert loaded is not None


class TestAutoRefreshNoSourcePath:
    def test_remote_repo_skipped(self, tmp_path):
        """If index has no source_path, auto_refresh returns without doing anything."""
        store = DocStore(base_path=str(tmp_path))
        sections = parse_file(SAMPLE_MD, "README.md", "gh/repo")
        for sec in sections:
            sec.summary = sec.title
        store.save_index("gh", "repo", sections, {"README.md": SAMPLE_MD}, {".md": 1})

        # Should not raise
        auto_refresh("gh/repo", str(tmp_path))

        # Index unchanged
        loaded = store.load_index("gh", "repo")
        assert loaded is not None
        assert loaded.source_path is None


class TestDocIndexV1Compat:
    def test_v1_file_hashes_strings_loaded_as_dicts(self, tmp_path):
        """Loading a v1 index with bare-string file_hashes should convert them in-memory."""
        store = DocStore(base_path=str(tmp_path))
        index_path = tmp_path / "local" / "oldrepo.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)

        v1_data = {
            "repo": "local/oldrepo",
            "owner": "local",
            "name": "oldrepo",
            "indexed_at": "2025-01-01T00:00:00",
            "doc_paths": ["README.md"],
            "doc_types": {".md": 1},
            "sections": [],
            "index_version": 1,
            "file_hashes": {"README.md": "deadbeef1234"},
        }
        index_path.write_text(json.dumps(v1_data), encoding="utf-8")

        loaded = store.load_index("local", "oldrepo")
        assert loaded is not None
        assert isinstance(loaded.file_hashes["README.md"], dict)
        assert loaded.file_hashes["README.md"]["sha256"] == "deadbeef1234"
        assert loaded.file_hashes["README.md"]["mtime"] == 0.0
        assert loaded.file_hashes["README.md"]["size"] == 0


# ── Integration tests ─────────────────────────────────────────────────────────

class TestAutoRefreshIntegration:
    def test_auto_refresh_on_get_section(self, tmp_path):
        """Index a folder, modify a file, trigger auto_refresh, verify updated content returned."""
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "guide.md").write_text(
            "# Guide\n\n## Installation\n\nRun pip install.\n\n## Usage\n\nImport and call.\n"
        )

        storage_path = str(tmp_path / "store")
        result = index_local(
            path=str(docs_dir),
            use_ai_summaries=False,
            storage_path=storage_path,
        )
        assert result["success"]
        repo_id = result["repo"]

        # Modify the file
        time.sleep(0.01)  # ensure mtime differs
        (docs_dir / "guide.md").write_text(
            "# Guide\n\n## Installation\n\nRun pip install --upgrade.\n\n## Usage\n\nImport and call.\n"
        )
        import os
        os.utime(docs_dir / "guide.md", None)  # touch to ensure new mtime

        # auto_refresh should detect the change and re-index
        auto_refresh(repo_id, storage_path)

        # Get the installation section
        store = DocStore(base_path=storage_path)
        index = store.load_index("local", "docs")
        assert index is not None

        install_sec = next(
            (s for s in index.sections if "Installation" in s.get("title", "")),
            None
        )
        assert install_sec is not None

        sec_result = get_section(
            repo=repo_id,
            section_id=install_sec["id"],
            storage_path=storage_path,
        )
        assert "section" in sec_result
        assert "upgrade" in sec_result["section"]["content"].lower()

    def test_auto_refresh_on_search(self, tmp_path):
        """Index folder, add new file, auto_refresh picks it up, search finds it."""
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "original.md").write_text("# Original\n\n## Alpha\n\nAlpha content.\n")

        storage_path = str(tmp_path / "store")
        result = index_local(
            path=str(docs_dir),
            use_ai_summaries=False,
            storage_path=storage_path,
        )
        assert result["success"]
        repo_id = result["repo"]

        # Add a new file
        (docs_dir / "newfile.md").write_text(
            "# New File\n\n## BrandNewUniqueSection\n\nFoo bar baz quux.\n"
        )

        auto_refresh(repo_id, storage_path)

        search_result = search_sections(
            repo=repo_id,
            query="BrandNewUniqueSection",
            storage_path=storage_path,
        )
        titles = [r["title"] for r in search_result["results"]]
        assert any("BrandNewUniqueSection" in t for t in titles)

    def test_auto_refresh_git_pull_simulation(self, tmp_path):
        """Simulate a git pull: index at commit A, advance HEAD to commit B, auto_refresh detects it."""
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "readme.md").write_text("# Readme\n\n## Intro\n\nVersion 1.\n")

        storage_path = str(tmp_path / "store")
        result = index_local(
            path=str(docs_dir),
            use_ai_summaries=False,
            storage_path=storage_path,
        )
        assert result["success"]
        repo_id = result["repo"]

        store = DocStore(base_path=storage_path)
        index = store.load_index("local", "docs")
        assert index is not None

        # Simulate: last_indexed_commit = "commit_A", now HEAD = "commit_B"
        # and the file has changed in between
        import os
        (docs_dir / "readme.md").write_text("# Readme\n\n## Intro\n\nVersion 2 — pulled.\n")
        os.utime(docs_dir / "readme.md", None)

        # Patch: pretend it's a git repo where HEAD changed
        def fake_run(cmd, **kwargs):
            r = MagicMock()
            if "--is-inside-work-tree" in cmd:
                r.returncode = 0; r.stdout = "true\n"
            elif "rev-parse" in cmd and "HEAD" in cmd:
                r.returncode = 0; r.stdout = "commit_B\n"
            elif "diff" in cmd and "--name-only" in cmd:
                r.returncode = 0; r.stdout = "readme.md\n"
            elif "status" in cmd:
                r.returncode = 0; r.stdout = ""
            elif "ls-files" in cmd:
                r.returncode = 0; r.stdout = "readme.md\n"
            else:
                r.returncode = 0; r.stdout = ""
            return r

        # Manually set last_indexed_commit to commit_A
        index_path = Path(storage_path) / "local" / "docs.json"
        with open(index_path) as f:
            data = json.load(f)
        data["last_indexed_commit"] = "commit_A"
        with open(index_path, "w") as f:
            json.dump(data, f)

        _git_repo_cache.clear()
        _git_repo_cache[str(docs_dir)] = True

        with patch("subprocess.run", side_effect=fake_run):
            auto_refresh(repo_id, storage_path)

        refreshed = store.load_index("local", "docs")
        assert refreshed is not None
        assert refreshed.last_indexed_commit == "commit_B"

        intro_sec = next(
            (s for s in refreshed.sections if "Intro" in s.get("title", "")),
            None
        )
        assert intro_sec is not None

        sec_result = get_section(
            repo=repo_id,
            section_id=intro_sec["id"],
            storage_path=storage_path,
        )
        assert "Version 2" in sec_result["section"]["content"]
