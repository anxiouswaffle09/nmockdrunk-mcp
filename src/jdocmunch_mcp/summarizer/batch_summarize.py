"""Three-tier summarization for doc sections: heading > AI > title fallback."""

import os
import re
from dataclasses import dataclass
from typing import Optional

_SUMMARY_LINE_RE = re.compile(r"^(\d+)\.\s+(.+)")


def _build_prompt(sections: list) -> str:
    lines = [
        "Summarize each documentation section in ONE short sentence (max 15 words).",
        "Focus on what the section covers.",
        "",
        "Input:",
    ]
    for i, sec in enumerate(sections, 1):
        snippet = sec.content[:200].replace("\n", " ")
        lines.append(f"{i}. [{sec.title}] {snippet}")
    lines.extend([
        "",
        "Output format: NUMBER. SUMMARY",
        "Example: 1. Explains how to install the package via pip.",
        "",
        "Summaries:",
    ])
    return "\n".join(lines)

from ..parser.sections import Section


def heading_summary(section: Section) -> str:
    """Tier 1: Use heading text as a natural summary (free, deterministic).

    For sections whose title is descriptive, the heading IS the summary.
    Returns up to 120 chars of the title.
    """
    return section.title[:120]


def title_fallback(section: Section) -> str:
    """Tier 3: Generate a summary from the section title when all else fails."""
    level_label = {0: "Root", 1: "Section", 2: "Subsection"}.get(section.level, "Section")
    return f"{level_label}: {section.title[:100]}"


@dataclass
class BatchSummarizer:
    """AI-based batch summarization using Claude Haiku (Tier 2)."""

    model: str = "claude-haiku-4-5-20251001"
    max_tokens_per_batch: int = 600

    def __post_init__(self):
        self.client = None
        self._init_client()

    def _init_client(self):
        try:
            from anthropic import Anthropic
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if api_key:
                self.client = Anthropic(api_key=api_key)
        except ImportError:
            self.client = None

    def summarize_batch(self, sections: list, batch_size: int = 8) -> list:
        """Summarize sections that don't yet have summaries."""
        if not self.client:
            for sec in sections:
                if not sec.summary:
                    sec.summary = title_fallback(sec)
            return sections

        to_summarize = [s for s in sections if not s.summary]

        for i in range(0, len(to_summarize), batch_size):
            batch = to_summarize[i:i + batch_size]
            self._summarize_one_batch(batch)

        return sections

    def _summarize_one_batch(self, batch: list):
        prompt = _build_prompt(batch)
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens_per_batch,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}]
            )
            summaries = self._parse_response(response.content[0].text, len(batch))
            for sec, summary in zip(batch, summaries):
                sec.summary = summary if summary else title_fallback(sec)
        except Exception:
            for sec in batch:
                if not sec.summary:
                    sec.summary = title_fallback(sec)

    def _parse_response(self, text: str, expected_count: int) -> list:
        summaries = [""] * expected_count
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            m = _SUMMARY_LINE_RE.match(line)
            if m:
                num = int(m.group(1))
                if 1 <= num <= expected_count:
                    summaries[num - 1] = m.group(2).strip()
        return summaries


def _create_summarizer():
    """Return the appropriate summarizer."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        s = BatchSummarizer()
        if s.client:
            return s
    return None


def summarize_sections(sections: list, use_ai: bool = True) -> list:
    """Three-tier summarization for doc sections.

    Tier 1: Heading text (always free — used as initial summary)
    Tier 2: AI batch summarization (Claude Haiku)
    Tier 3: title_fallback (always works)
    """
    # Tier 1: seed summary from heading
    for sec in sections:
        if not sec.summary:
            sec.summary = heading_summary(sec)

    # Tier 2: AI for sections where heading is short/uninformative
    if use_ai:
        # Only call AI on sections whose summary is very short (likely uninformative headings)
        needs_ai = [s for s in sections if len(s.summary) < 20 and s.content]
        if needs_ai:
            summarizer = _create_summarizer()
            if summarizer:
                # Temporarily clear summaries so batch_summarize processes them
                for sec in needs_ai:
                    sec.summary = ""
                summarizer.summarize_batch(needs_ai)

    # Tier 3: fallback for any still missing
    for sec in sections:
        if not sec.summary:
            sec.summary = title_fallback(sec)

    return sections
