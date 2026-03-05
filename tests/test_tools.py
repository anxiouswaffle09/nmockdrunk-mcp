"""Tests for tool functions."""

import pytest
from pathlib import Path

from jdocmunch_mcp.tools.index_local import _should_skip

from jdocmunch_mcp.tools.index_local import index_local
from jdocmunch_mcp.tools.list_repos import list_repos
from jdocmunch_mcp.tools.delete_index import delete_index
from jdocmunch_mcp.tools.get_toc import get_toc
from jdocmunch_mcp.tools.get_toc_tree import get_toc_tree
from jdocmunch_mcp.tools.get_document_outline import get_document_outline
from jdocmunch_mcp.tools.search_sections import search_sections
from jdocmunch_mcp.tools.get_section import get_section
from jdocmunch_mcp.tools.get_sections import get_sections

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def indexed_repo(tmp_path):
    """Index the fixture docs folder and return the repo identifier."""
    result = index_local(
        path=str(FIXTURES / "docs"),
        use_ai_summaries=False,
        storage_path=str(tmp_path),
    )
    assert result["success"], f"Indexing failed: {result}"
    return result["repo"], str(tmp_path)


class TestShouldSkip:
    def test_skips_build(self):
        assert _should_skip("build/output.md") is True

    def test_skips_node_modules(self):
        assert _should_skip("node_modules/pkg/README.md") is True

    def test_does_not_skip_rebuild(self):
        """'rebuild/' should not be caught by the 'build/' pattern."""
        assert _should_skip("rebuild/output.md") is False

    def test_does_not_skip_normal_file(self):
        assert _should_skip("docs/guide.md") is False

    def test_skips_nested_git(self):
        assert _should_skip("submodule/.git/config") is True

    def test_does_not_skip_partial_match_in_filename(self):
        """'build_notes.md' has 'build' but no 'build/' component."""
        assert _should_skip("docs/build_notes.md") is False


class TestIndexLocal:
    def test_success(self, tmp_path):
        result = index_local(
            path=str(FIXTURES / "docs"),
            use_ai_summaries=False,
            storage_path=str(tmp_path),
        )
        assert result["success"] is True
        assert result["file_count"] >= 1
        assert result["section_count"] >= 1
        assert "_meta" in result

    def test_invalid_path(self, tmp_path):
        result = index_local(path="/nonexistent/path", storage_path=str(tmp_path))
        assert result["success"] is False
        assert "error" in result

    def test_not_a_dir(self, tmp_path):
        f = tmp_path / "file.md"
        f.write_text("# Hello")
        result = index_local(path=str(f), storage_path=str(tmp_path))
        assert result["success"] is False

    def test_includes_txt(self, tmp_path):
        result = index_local(
            path=str(FIXTURES / "text"),
            use_ai_summaries=False,
            storage_path=str(tmp_path),
        )
        assert result["success"] is True
        assert ".txt" in result["doc_types"]


class TestListRepos:
    def test_lists_indexed(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        result = list_repos(storage_path=storage_path)
        assert result["count"] >= 1
        assert any(r["repo"] == repo_id for r in result["repos"])

    def test_empty(self, tmp_path):
        result = list_repos(storage_path=str(tmp_path))
        assert result["count"] == 0


class TestDeleteIndex:
    def test_deletes(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        result = delete_index(repo=repo_id, storage_path=storage_path)
        assert result["success"] is True
        # Should be gone
        after = list_repos(storage_path=storage_path)
        assert all(r["repo"] != repo_id for r in after["repos"])

    def test_nonexistent(self, tmp_path):
        result = delete_index(repo="nobody/nothing", storage_path=str(tmp_path))
        assert result["success"] is False


class TestGetToc:
    def test_returns_sections(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        result = get_toc(repo=repo_id, storage_path=storage_path)
        assert "sections" in result
        assert result["section_count"] >= 1
        assert "_meta" in result

    def test_no_content_field(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        result = get_toc(repo=repo_id, storage_path=storage_path)
        for sec in result["sections"]:
            assert "content" not in sec

    def test_sorted_by_doc_and_offset(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        result = get_toc(repo=repo_id, storage_path=storage_path)
        secs = result["sections"]
        for i in range(1, len(secs)):
            prev = (secs[i - 1]["doc_path"], secs[i - 1]["byte_start"])
            curr = (secs[i]["doc_path"], secs[i]["byte_start"])
            assert prev <= curr


class TestGetTocTree:
    def test_returns_documents(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        result = get_toc_tree(repo=repo_id, storage_path=storage_path)
        assert "documents" in result
        assert result["doc_count"] >= 1

    def test_nested_structure(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        result = get_toc_tree(repo=repo_id, storage_path=storage_path)
        # Each document has sections with potential children
        for doc in result["documents"]:
            assert "doc_path" in doc
            assert "sections" in doc


class TestGetDocumentOutline:
    def test_specific_doc(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        result = get_document_outline(
            repo=repo_id,
            doc_path="sample.md",
            storage_path=storage_path,
        )
        assert "sections" in result
        assert result["section_count"] >= 1

    def test_not_found(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        result = get_document_outline(
            repo=repo_id,
            doc_path="nonexistent.md",
            storage_path=storage_path,
        )
        assert "error" in result


class TestSearchSections:
    def test_basic_search(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        result = search_sections(
            repo=repo_id,
            query="installation",
            storage_path=storage_path,
        )
        assert "results" in result
        assert "_meta" in result

    def test_no_content_in_results(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        result = search_sections(
            repo=repo_id,
            query="section",
            storage_path=storage_path,
        )
        for r in result["results"]:
            assert "content" not in r

    def test_max_results(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        result = search_sections(
            repo=repo_id,
            query="the",
            max_results=2,
            storage_path=storage_path,
        )
        assert len(result["results"]) <= 2

    def test_tokens_saved_reported(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        result = search_sections(
            repo=repo_id,
            query="install",
            storage_path=storage_path,
        )
        assert "tokens_saved" in result["_meta"]


class TestGetSection:
    def test_get_content(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        toc = get_toc(repo=repo_id, storage_path=storage_path)
        first_id = toc["sections"][0]["id"]

        result = get_section(
            repo=repo_id,
            section_id=first_id,
            storage_path=storage_path,
        )
        assert "section" in result
        assert "content" in result["section"]

    def test_verify_hash(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        toc = get_toc(repo=repo_id, storage_path=storage_path)
        first_id = toc["sections"][0]["id"]

        result = get_section(
            repo=repo_id,
            section_id=first_id,
            verify=True,
            storage_path=storage_path,
        )
        assert "section" in result
        # hash_verified may be True or None (if no hash stored)
        assert "hash_verified" in result["section"]

    def test_not_found(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        result = get_section(
            repo=repo_id,
            section_id="nobody::nowhere::nothing#1",
            storage_path=storage_path,
        )
        assert "error" in result


class TestGetSections:
    def test_batch_retrieval(self, indexed_repo):
        repo_id, storage_path = indexed_repo
        toc = get_toc(repo=repo_id, storage_path=storage_path)
        ids = [s["id"] for s in toc["sections"][:3]]

        result = get_sections(
            repo=repo_id,
            section_ids=ids,
            storage_path=storage_path,
        )
        assert "sections" in result
        assert result["section_count"] == len(ids)

    def test_meta_complete(self, indexed_repo):
        """_meta must include tokens_saved, total_tokens_saved, and cost_avoided."""
        repo_id, storage_path = indexed_repo
        toc = get_toc(repo=repo_id, storage_path=storage_path)
        ids = [s["id"] for s in toc["sections"][:2]]

        result = get_sections(repo=repo_id, section_ids=ids, storage_path=storage_path)
        meta = result["_meta"]
        assert "tokens_saved" in meta
        assert "total_tokens_saved" in meta
        assert "cost_avoided" in meta
        assert "total_cost_avoided" in meta

    def test_invalid_section_id_returns_error(self, indexed_repo):
        """Invalid section IDs should produce per-item error dicts, not crash."""
        repo_id, storage_path = indexed_repo
        result = get_sections(
            repo=repo_id,
            section_ids=["invalid::id::nope#9"],
            storage_path=storage_path,
        )
        assert result["section_count"] == 1
        assert "error" in result["sections"][0]
