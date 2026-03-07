"""Tests for summarizer module."""

import pytest
from jdocmunch_mcp.parser.sections import Section
from jdocmunch_mcp.summarizer.batch_summarize import (
    heading_summary,
    title_fallback,
    summarize_sections,
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


class TestSummarizeSections:
    def test_fills_summaries(self):
        sections = [
            make_section("Overview", level=1),
            make_section("Installation", level=2),
            make_section("Usage", level=2),
        ]
        result = summarize_sections(sections)
        for sec in result:
            assert sec.summary != ""

    def test_summaries_filled(self):
        sections = [make_section(f"Section {i}", level=2) for i in range(5)]
        result = summarize_sections(sections)
        assert all(s.summary for s in result)

    def test_preserves_existing_summary(self):
        sec = make_section("Title")
        sec.summary = "Already summarized."
        result = summarize_sections([sec])
        assert result[0].summary == "Already summarized."
