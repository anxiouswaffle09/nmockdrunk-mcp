# Technical Specification

## Overview

**jdocmunch-mcp** pre-indexes documentation files by their heading hierarchy, assigning each section a stable ID and byte offsets for O(1) content retrieval. Agents discover sections via TOC or search, then retrieve only the content they need.

### Token Savings

| Scenario                              | Raw dump        | jDocMunch       | Savings |
| ------------------------------------- | --------------- | --------------- | ------- |
| Browse 50-file doc set structure      | ~100,000 tokens | ~2,000 tokens   | **98%** |
| Find a specific configuration section | ~12,000 tokens  | ~400 tokens     | **97%** |
| Read one section body                 | ~12,000 tokens  | ~300 tokens     | **97.5%** |
| Understand a module's public API docs | ~8,000 tokens   | ~500 tokens     | **93.7%** |

---

## MCP Tools (10)

### Indexing Tools

#### `index_local` — Index a local documentation folder

```json
{
  "path": "/path/to/docs",
  "use_ai_summaries": true,
  "extra_ignore_patterns": ["drafts/**"],
  "follow_symlinks": false
}
```

Walks the local directory with full security controls: path traversal prevention, symlink escape protection, secret detection, binary filtering, `.gitignore` respect, and directory pruning. Parses `.md`, `.mdx`, `.markdown`, `.txt`, and `.rst` files.

#### `index_repo` — Index a GitHub repository's documentation

```json
{
  "url": "owner/repo",
  "use_ai_summaries": true
}
```

Fetches documentation files via the GitHub API, parses sections, and saves to local storage.

#### `delete_index` — Delete index for a repository

```json
{
  "repo": "owner/repo"
}
```

Deletes both the index JSON and the raw content cache directory.

---

### Discovery Tools

#### `list_repos` — List indexed documentation sets

No input required. Returns all indexed repositories with section counts, document counts, and document type breakdown.

#### `get_toc` — Flat table of contents

```json
{
  "repo": "owner/repo"
}
```

Returns all sections in document order with their IDs, titles, levels, and summaries. Content is excluded — use `get_section` to retrieve full content.

#### `get_toc_tree` — Nested table of contents tree

```json
{
  "repo": "owner/repo"
}
```

Returns sections organized by document, with parent/child heading relationships visible. Content excluded.

#### `get_document_outline` — Section hierarchy for one document

```json
{
  "repo": "owner/repo",
  "doc_path": "docs/configuration.md"
}
```

Returns the heading hierarchy for a single file without content. Lighter than `get_toc` when you already know which document is relevant.

---

### Search Tools

#### `search_sections` — Weighted section search

```json
{
  "repo": "owner/repo",
  "query": "authentication",
  "doc_path": "docs/security.md",
  "max_results": 10
}
```

Weighted scoring across title, summary, tags, and content. Returns summaries only — use `get_section` for full content. `doc_path` is optional; omit to search all documents.

---

### Retrieval Tools

#### `get_section` — Retrieve full content of one section

```json
{
  "repo": "owner/repo",
  "section_id": "owner/repo::docs/install.md::installation#1",
  "verify": true
}
```

Retrieves section source via byte-offset seeking (O(1)). Optional `verify` re-hashes the content and compares it to the stored `content_hash`. Response `_meta.content_verified` will be `true` if matched, `false` if drifted.

#### `get_sections` — Batch retrieve multiple sections

```json
{
  "repo": "owner/repo",
  "section_ids": ["id1", "id2", "id3"],
  "verify": false
}
```

Returns a list of sections with full content, plus an error list for any IDs not found.

---

## Data Models

### Section

```python
@dataclass
class Section:
    id: str            # "{repo}::{doc_path}::{slug}#{level}"
    repo: str
    doc_path: str      # Relative path of the source document
    title: str         # Heading text
    content: str       # Full section text (heading + body, including subsections)
    level: int         # 1–6 (ATX heading level); 0 = pre-first-heading root section
    parent_id: str     # Section ID of parent heading; "" if top-level
    children: list     # List of child section IDs
    byte_start: int    # Start byte offset in the cached raw file
    byte_end: int      # End byte offset in the cached raw file
    summary: str       # One-sentence summary (heading text / AI / fallback)
    tags: list         # #hashtag tags extracted from content
    references: list   # URLs and markdown link targets extracted from content
    content_hash: str  # SHA-256 of section content (drift detection)
```

### DocIndex

```python
@dataclass
class DocIndex:
    repo: str              # "owner/repo"
    owner: str
    name: str
    indexed_at: str        # ISO timestamp
    doc_paths: list        # Sorted list of indexed document paths
    doc_types: dict        # {".md": 12, ".txt": 3}
    sections: list         # Serialized Section dicts (without content)
    index_version: int     # Schema version (current: 1)
    file_hashes: dict      # {doc_path: SHA-256} for change detection
```

---

## File Discovery

### GitHub Repositories

Fetches via GitHub API. `.gitignore` is fetched and respected (if present in the repo root).

### Local Folders

Recursive directory walk using `os.walk` with early directory pruning to skip `SKIP_PATTERNS` before descending.

### Filtering Pipeline (Both Paths)

1. **Skip patterns** — `node_modules/`, `vendor/`, `venv/`, `.venv/`, `__pycache__/`, `dist/`, `build/`, `.git/`, `.tox/`, `.mypy_cache/`, `.gradle/`, `target/`
2. **`.gitignore`** — respected via the `pathspec` library
3. **`extra_ignore_patterns`** — user-supplied gitignore-style patterns (local only)
4. **Extension filter** — must be in `ALL_EXTENSIONS` (`.md`, `.markdown`, `.mdx`, `.txt`, `.rst`)
5. **Secret detection** — `.env`, `*.pem`, `*.key`, credentials files excluded
6. **Binary detection** — extension-based + null-byte content sniffing
7. **Size limit** — 500 KB per file
8. **File count limit** — 500 files max

---

## Section ID Format

```
{repo}::{doc_path}::{slug}#{level}
```

Examples:

```
owner/repo::README.md::installation#1
owner/repo::docs/config.md::authentication-options#2
local/myproject::guide.md::quick-start#1
```

**Slug:** heading text lowercased, non-alphanumeric sequences replaced with hyphens. Duplicate slugs within the same document receive `-2`, `-3` suffixes.

Section IDs are returned by `get_toc`, `get_toc_tree`, `get_document_outline`, and `search_sections`. Pass them to `get_section` or `get_sections` to retrieve content.

---

## Response Envelope

Search and retrieval tools return a `_meta` object:

```json
{
  "_meta": {
    "latency_ms": 12,
    "sections_returned": 5,
    "tokens_saved": 1840,
    "total_tokens_saved": 94320,
    "cost_avoided": {
      "claude_opus": 0.0276,
      "gpt5_latest": 0.0184
    },
    "total_cost_avoided": {
      "claude_opus": 1.4148,
      "gpt5_latest": 0.9432
    }
  }
}
```

- **`tokens_saved`**: Tokens saved this call (raw bytes of matched docs vs response bytes, ÷ 4)
- **`total_tokens_saved`**: Cumulative tokens saved, persisted to `~/.doc-index/_savings.json`
- **`cost_avoided`**: Dollar value saved this call (Opus 4.6 @ $15/1M, GPT-5 @ $10/1M)
- **`total_cost_avoided`**: Cumulative cost avoided across all sessions

Present on: `search_sections`, `get_section`, `get_sections`.

---

## Error Handling

All errors return:

```json
{
  "error": "Human-readable message"
}
```

| Scenario                           | Behavior                                              |
| ---------------------------------- | ----------------------------------------------------- |
| Repository not found (GitHub 404)  | Error with message                                    |
| Rate limited (GitHub 403)          | Error with message; suggest setting `GITHUB_TOKEN`    |
| File fetch fails                   | File skipped; indexing continues                      |
| Parse fails (single file)          | File skipped with warning; indexing continues         |
| No documentation files found      | Error returned                                        |
| No sections extracted              | Error returned                                        |
| Section ID not found               | Error in per-section error list                       |
| Repository not indexed             | Error suggesting indexing first                       |
| AI summarization fails             | Falls back to title fallback                          |
| Index version mismatch             | Old index ignored; full re-index required             |

---

## Environment Variables

| Variable                     | Purpose                                                    | Required |
| ---------------------------- | ---------------------------------------------------------- | -------- |
| `GITHUB_TOKEN`               | GitHub API authentication (higher limits, private repos)   | No       |
| `ANTHROPIC_API_KEY`          | AI summarization via Claude Haiku                          | No       |
| `DOC_INDEX_PATH`             | Custom storage path (default: `~/.doc-index/`)             | No       |
| `JDOCMUNCH_SHARE_SAVINGS`    | Set to `0` to disable anonymous token savings reporting    | No       |
