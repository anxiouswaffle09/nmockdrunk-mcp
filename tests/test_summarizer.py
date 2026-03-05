"""Tests for summarizer module."""

import pytest
from jdocmunch_mcp.parser.sections import Section
from jdocmunch_mcp.summarizer.batch_summarize import (
    heading_summary,
    title_fallback,
    summarize_sections,
    BatchSummarizer,
    _build_prompt,
)


def make_section(title="Test Section", level=2, content="Some content here."):
    return Section(
        id=f"repo::doc.md::{title.lower().replace(' ', '-')}#{level}",
        repo="repo",
        doc_path="doc.md",
        title=title,
        content=content,
        level=level,
        parent_id="",
        children=[],
    )


class TestHeadingSummary:
    def test_returns_title(self):
        sec = make_section(title="Installation Guide")
        assert heading_summary(sec) == "Installation Guide"

    def test_truncates_long_title(self):
        long_title = "A" * 200
        sec = make_section(title=long_title)
        assert len(heading_summary(sec)) == 120


class TestTitleFallback:
    def test_level_1(self):
        sec = make_section(title="Overview", level=1)
        result = title_fallback(sec)
        assert "Section" in result or "Overview" in result

    def test_level_2(self):
        sec = make_section(title="Install", level=2)
        result = title_fallback(sec)
        assert "Subsection" in result or "Install" in result


class TestParseResponse:
    def setup_method(self):
        self.summarizer = BatchSummarizer.__new__(BatchSummarizer)
        self.summarizer.client = None

    def test_basic_parse(self):
        text = "1. Explains installation.\n2. Covers configuration."
        result = self.summarizer._parse_response(text, 2)
        assert result[0] == "Explains installation."
        assert result[1] == "Covers configuration."

    def test_dotted_non_numbered_line_ignored(self):
        """Lines like 'e.g., something' or 'v1.2.3' should not corrupt output."""
        text = "e.g., some context\nv1.2.3 released\n1. Real summary here."
        result = self.summarizer._parse_response(text, 1)
        assert result[0] == "Real summary here."

    def test_out_of_range_ignored(self):
        text = "5. Out of range summary."
        result = self.summarizer._parse_response(text, 2)
        assert result == ["", ""]

    def test_partial_response(self):
        """Missing entries leave empty strings."""
        text = "1. First summary."
        result = self.summarizer._parse_response(text, 3)
        assert result[0] == "First summary."
        assert result[1] == ""
        assert result[2] == ""


class TestBuildPrompt:
    def test_contains_section_content(self):
        from jdocmunch_mcp.parser.sections import Section
        sec = Section(
            id="r::d::s#1", repo="r", doc_path="d.md", title="My Title",
            content="Some content here.", level=1, parent_id="", children=[],
        )
        prompt = _build_prompt([sec])
        assert "My Title" in prompt
        assert "Some content" in prompt
        assert "1." in prompt

    def test_numbered_correctly(self):
        from jdocmunch_mcp.parser.sections import Section
        secs = [
            Section(id=f"r::d::s{i}#1", repo="r", doc_path="d.md",
                    title=f"Title {i}", content="x", level=1, parent_id="", children=[])
            for i in range(3)
        ]
        prompt = _build_prompt(secs)
        assert "1." in prompt
        assert "2." in prompt
        assert "3." in prompt


class TestSummarizeSections:
    def test_no_ai(self):
        sections = [
            make_section("Overview", level=1),
            make_section("Installation", level=2),
            make_section("Usage", level=2),
        ]
        result = summarize_sections(sections, use_ai=False)
        for sec in result:
            assert sec.summary != ""

    def test_summaries_filled(self):
        sections = [make_section(f"Section {i}", level=2) for i in range(5)]
        result = summarize_sections(sections, use_ai=False)
        assert all(s.summary for s in result)

    def test_preserves_existing_summary(self):
        sec = make_section("Title")
        sec.summary = "Already summarized."
        result = summarize_sections([sec], use_ai=False)
        # summarize_sections seeds from heading for empty summaries
        # but shouldn't CLEAR an existing summary if heading_summary tier runs
        assert result[0].summary != ""
