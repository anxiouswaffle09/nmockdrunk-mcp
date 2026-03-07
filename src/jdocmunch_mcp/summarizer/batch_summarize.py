"""Two-tier summarization for doc sections: heading > title fallback."""

from ..parser.sections import Section


def heading_summary(section: Section) -> str:
    """Use heading text as a natural summary (free, deterministic).

    For sections whose title is descriptive, the heading IS the summary.
    Returns up to 120 chars of the title.
    """
    return section.title[:120]


def title_fallback(section: Section) -> str:
    """Generate a summary from the section title when heading is too short."""
    level_label = {0: "Root", 1: "Section", 2: "Subsection"}.get(section.level, "Section")
    return f"{level_label}: {section.title[:100]}"


def summarize_sections(sections: list) -> list:
    """Two-tier summarization: heading text, then title fallback."""
    for sec in sections:
        if not sec.summary:
            sec.summary = heading_summary(sec)
    for sec in sections:
        if not sec.summary:
            sec.summary = title_fallback(sec)
    return sections
