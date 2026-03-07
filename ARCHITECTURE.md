# Architecture

## Directory Structure

```
nmockdrunk-mcp/
├── pyproject.toml
├── README.md
├── SECURITY.md
├── ARCHITECTURE.md
├── SPEC.md
├── USER_GUIDE.md
├── TOKEN_SAVINGS.md
│
├── src/jdocmunch_mcp/
│   ├── __init__.py
│   ├── server.py                    # MCP server: 10 tool definitions + dispatch
│   ├── security.py                  # Path traversal, symlink, secret, binary detection
│   │
│   ├── parser/
│   │   ├── __init__.py              # parse_file() dispatcher, ALL_EXTENSIONS registry
│   │   ├── sections.py              # Section dataclass, ID generation, slugify, hash
│   │   ├── markdown_parser.py       # ATX + setext heading splitter, MDX preprocessor
│   │   ├── text_parser.py           # Plain-text / RST section splitting
│   │   └── hierarchy.py             # parent_id / children wiring after parse
│   │
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── doc_store.py             # DocIndex, DocStore: save/load, byte-range reads
│   │   └── token_tracker.py         # Persistent token savings counter (~/.doc-index/_savings.json)
│   │
│   ├── summarizer/
│   │   ├── __init__.py
│   │   └── batch_summarize.py       # Heading text → AI batch → title fallback
│   │
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── _constants.py            # SKIP_PATTERNS shared across indexing tools
│   │   ├── index_local.py           # Local folder indexing
│   │   ├── index_repo.py            # GitHub repository indexing
│   │   ├── list_repos.py
│   │   ├── get_toc.py
│   │   ├── get_toc_tree.py
│   │   ├── get_document_outline.py
│   │   ├── search_sections.py
│   │   ├── get_section.py
│   │   ├── get_sections.py
│   │   └── delete_index.py
│   │
│   └── auto_refresh/
│       ├── __init__.py
│       ├── _scan.py             # File system scan helpers
│       ├── _types.py            # Shared types for refresh system
│       ├── git_detector.py      # Git-based change detection
│       ├── incremental.py       # Incremental reindex logic
│       ├── mtime_detector.py    # mtime-based change detection
│       ├── refresh_manager.py   # Orchestrates pre-call refresh
│       └── summarization_queue.py  # Deferred AI summarization for new sections
│
├── tests/
│   ├── fixtures/
│   ├── test_parser.py
│   ├── test_storage.py
│   ├── test_tools.py
│   ├── test_server.py
│   └── test_security.py
│
└── benchmarks/
    ├── jDocMunch_Benchmark_Kubernetes.md
    ├── jDocMunch_Benchmark_LangChain_MDX.md
    └── jDocMunch_Benchmark_SciPy.md
```

---

## Data Flow

```
Pre-call auto-refresh (local indexes only)
    │  mtime / git change detection → incremental reindex if changed
    ▼
Documentation files (GitHub API or local folder)
    │
    ▼
Security filters (path traversal, symlinks, secrets, binary, size)
    │
    ▼
File type dispatch (markdown / MDX / text / RST)
    │
    ▼
MDX pre-processor (strip JSX tags, frontmatter, import/export)
    │
    ▼
Heading-based section splitting (ATX # / setext underline / paragraph blocks)
    │
    ▼
Byte offset recording + content hashing + reference/tag extraction
    │
    ▼
Hierarchy wiring (parent_id, children populated)
    │
    ▼
Summarization (heading text → AI batch → title fallback)
    │
    ▼
Storage (JSON index + raw files, atomic writes, ~/.doc-index/)
    │
    ▼
MCP tools (TOC, search, byte-range retrieval)
```

---

## Parser Design

The parser follows a **format dispatch pattern**. File extension determines which parser is used.

### Supported Formats

| Extension            | Parser        | Notes                                       |
| -------------------- | ------------- | ------------------------------------------- |
| `.md`, `.markdown`   | Markdown      | ATX headings + setext headings              |
| `.mdx`               | Markdown      | MDX-specific syntax stripped first          |
| `.txt`               | Text          | Paragraph-block splitting                   |
| `.rst`               | Text          | Treated as plain text (heading detection planned) |

### Markdown Parser

`parse_markdown()` in `parser/markdown_parser.py`:

* Handles **ATX headings** (`# H1` through `###### H6`)
* Handles **setext headings** (text underlined with `===` or `---`)
* Content before the first heading becomes a **level-0 root section**
* Tracks **byte offset per line** using `len(line.encode("utf-8"))` for UTF-8-correct offsets
* Extracts **references** (URLs, markdown link targets) and **#hashtag tags** from each section

### MDX Preprocessor

`strip_mdx()` in `parser/markdown_parser.py`:

* Strips YAML/TOML frontmatter (`---...---`)
* Removes `:::js` fenced blocks (keeps `:::python` blocks)
* Removes JSX component tags, preserving inner text content
* Removes mermaid diagrams, import/export statements
* Collapses excess blank lines

### Hierarchy Wiring

`wire_hierarchy()` in `parser/hierarchy.py`:

After flat parsing, a second pass assigns `parent_id` and populates `children` by tracking the stack of open heading levels.

---

## Section ID Scheme

```
{repo}::{doc_path}::{slug}#{level}
```

Examples:

* `owner/repo::docs/install.md::installation#1`
* `owner/repo::README.md::quick-start#2`
* `local/myproject::guide.md::configuration#2`

**Slug generation:** heading text is lowercased, non-alphanumeric sequences replaced with hyphens, trimmed. Duplicate slugs within the same document receive `-2`, `-3` suffixes.

IDs are stable across re-indexing as long as the file path, heading text, and heading level remain unchanged.

---

## Storage

Indexes are stored at `~/.doc-index/` (configurable via `DOC_INDEX_PATH`):

```
~/.doc-index/
├── {owner}/
│   ├── {name}.json           # DocIndex: metadata, section metadata (no content)
│   └── {name}/               # Cached raw doc files for byte-range reads
│       ├── README.md
│       └── docs/
│           └── guide.md
└── _savings.json             # Cumulative token savings counter
```

* Sections in the JSON index include byte offsets but **not** full content.
* Full content is retrieved on demand via **O(1) `seek()` + `read()`** using stored byte offsets.
* Atomic writes (temp file + rename) prevent corrupt state on interrupted writes — applied to both the index JSON and content cache files during incremental reindex.
* Index version (`INDEX_VERSION = 1`) allows future schema migrations; mismatched versions are ignored and require re-indexing.

---

## Auto-Refresh (Local Indexes)

Before each tool call on a local index, nmockdrunk-mcp checks whether any watched files have changed since the last index. If changes are detected, it runs an incremental reindex before returning results.

**Change detection:** Two strategies — one runs per call based on whether the folder is a git repo:
- **Git detector** (`git_detector.py`) — used when the folder is a git repo; runs `git diff --name-only` against the last indexed commit to catch committed changes, plus `git status --porcelain` for uncommitted working-tree changes
- **mtime detector** (`mtime_detector.py`) — used for non-git folders; compares file modification times against stored metadata

**Refresh manager** (`refresh_manager.py`):
- Orchestrates the full pre-call refresh cycle: detects changes, triggers incremental reindex, and drains the summarization queue

**Incremental reindex** (`incremental.py`):
- Only parses files that changed, were added, or were deleted
- Writes updated content cache files atomically (temp file + rename)
- Merges new section data into the existing index without full re-parse

**Summarization queue** (`summarization_queue.py`):
- New sections added during auto-refresh are queued for AI summarization
- Summarization runs asynchronously and does not block the tool response

For most workflows, agents working with local documentation folders never need to call `index_local` again after the initial index — the server detects and applies changes automatically before each tool call.

---

## Search Algorithm

`DocIndex.search()` uses weighted scoring:

| Match type              | Weight                         |
| ----------------------- | ------------------------------ |
| Title exact match       | +20                            |
| Title substring         | +10                            |
| Title word overlap      | +5 per word                    |
| Summary substring       | +8                             |
| Summary word overlap    | +2 per word                    |
| Tag match               | +3 per matching tag            |
| Content word match      | +1 per word (capped at 5)      |

Optional `doc_path` filter scopes search to a single document. Results scoring zero are excluded. Content is stripped from results — use `get_section` to retrieve full content.

---

## Summarization Tiers

Section summaries are generated in three tiers, in order:

1. **Heading text** — used directly as the summary (free, deterministic, often sufficient)
2. **AI batch** — Claude Haiku (if `ANTHROPIC_API_KEY`), in batches of 8 sections per prompt
3. **Title fallback** — `"Section: {title}"` when AI is unavailable or fails

---

## Response Envelope

Search and retrieval tools return a `_meta` object with timing and token savings:

```json
{
  "results": [...],
  "_meta": {
    "latency_ms": 12,
    "sections_returned": 5,
    "tokens_saved": 1840,
    "total_tokens_saved": 94320,
    "cost_avoided": { "claude_opus": 0.0276, "gpt5_latest": 0.0184 },
    "total_cost_avoided": { "claude_opus": 1.4148, "gpt5_latest": 0.9432 }
  }
}
```

`total_tokens_saved` and `total_cost_avoided` accumulate across all tool calls and persist to `~/.doc-index/_savings.json`.

---

## Dependencies

| Package                      | Purpose                                |
| ---------------------------- | -------------------------------------- |
| `mcp>=1.0.0,<1.10.0`         | MCP server framework                   |
| `httpx>=0.27.0`              | Async HTTP for GitHub API              |
| `anthropic>=0.40.0`          | AI summarization via Claude Haiku (optional: `pip install jdocmunch-mcp[anthropic]`) |
| `pathspec>=0.12.0`           | `.gitignore` pattern matching          |
