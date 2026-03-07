# Documentation Rebrand and Update Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rebrand all docs from "jDocMunch MCP" to "nmockdrunk-mcp", add a "What's different from upstream" section to the README, document the auto-refresh feature, and remove all Gemini references.

**Architecture:** Pure documentation changes across four files (README.md, ARCHITECTURE.md, USER_GUIDE.md, SPEC.md). No code changes. Each file is edited in-place; no new files created except this plan.

**Tech Stack:** Markdown, Git

---

## Context

- Upstream: `jgravelle/jdocmunch-mcp`
- This fork: `anxiouswaffle09/nmockdrunk-mcp`
- PyPI package name `jdocmunch-mcp` is **unchanged** — do not rename package references in install commands or config examples
- Tool names (`index_local`, `get_section`, etc.) are **unchanged**
- "Google Antigravity" in client compatibility lists is a separate product — **leave untouched**

## Changes per doc

| File | Rebrand | What's Different section | Auto-refresh | Gemini removal |
|------|---------|--------------------------|--------------|----------------|
| README.md | ✓ | ✓ (new section) | ✓ (in new section) | ✓ |
| ARCHITECTURE.md | ✓ | — | ✓ (new subsection + dir tree) | ✓ |
| USER_GUIDE.md | ✓ | — | ✓ (new workflow + troubleshooting) | ✓ |
| SPEC.md | ✓ | — | ✓ (index_local description) | already clean |

---

### Task 1: Update README.md

**Files:**
- Modify: `README.md`

**Step 1: Replace the title and all jDocMunch name references**

Find and replace:
- `jDocMunch MCP` → `nmockdrunk-mcp` (title, heading, body text)
- `jDocMunch` (standalone) → `nmockdrunk-mcp`
- Do NOT change: `jdocmunch-mcp` (package name in install/config), `jdocmunch` (tool names), `jDocMunch-MCP` links to upstream repo

**Step 2: Add "What's different from upstream" section**

Insert the following section after the badges block (after the `---` that follows the badges, before the `## Why this exists` heading):

```markdown
## What's different from upstream

nmockdrunk-mcp is a fork of [jgravelle/jdocmunch-mcp](https://github.com/jgravelle/jdocmunch-mcp) with the following improvements:

- **Auto-refresh for local indexes** — the server detects file changes and re-indexes automatically before each tool call; no manual `index_local` needed after editing local docs
- **Anthropic-only AI summaries** — dropped Google Gemini support; `ANTHROPIC_API_KEY` is the sole optional AI backend, reducing dependencies and complexity
- **Atomic content cache writes** — incremental reindex uses temp-file + rename to prevent corrupt cache state on interrupted writes
- **Optimized section reads** — `get_section` and `get_sections` use a direct byte-range read that avoids a redundant index load per call
- **Indexing and outline edge case fixes** — hardened handling of malformed heading hierarchies and empty section edge cases

---
```

**Step 3: Update the security bullet**

In the `## Security` section, the bullet that reads:
```
* atomic index writes
```
Change to:
```
* atomic writes (index and content cache)
```

**Step 4: Verify the env vars table has no GOOGLE_API_KEY row**

The table in `## Environment variables` should only list: `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, `DOC_INDEX_PATH`, `JDOCMUNCH_SHARE_SAVINGS`. Confirm and remove any Google row if present.

**Step 5: Commit**

```bash
git add README.md
git commit -m "docs: rebrand to nmockdrunk-mcp, add upstream diff section"
```

---

### Task 2: Update ARCHITECTURE.md

**Files:**
- Modify: `ARCHITECTURE.md`

**Step 1: Replace jDocMunch name references in body text**

Same substitution rules as Task 1. The file title is `# Architecture` — leave as-is.

**Step 2: Add auto_refresh/ to the directory tree**

In the `## Directory Structure` code block, after the `└── tools/` block and before the closing of `src/jdocmunch_mcp/`, add:

```
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
```

**Step 3: Add auto-refresh data flow step**

In the `## Data Flow` section, prepend a step before "Security filters":

```
Pre-call auto-refresh (local indexes only)
    │  mtime / git change detection → incremental reindex if changed
    ▼
Security filters (path traversal, symlinks, secrets, binary, size)
```

**Step 4: Add Auto-Refresh subsection**

After the `## Storage` section and before `## Search Algorithm`, insert:

```markdown
## Auto-Refresh (Local Indexes)

Before each tool call on a local index, nmockdrunk-mcp checks whether any watched files have changed since the last index. If changes are detected, it runs an incremental reindex before returning results.

**Change detection:** Two strategies, applied in order:
1. **Git detector** (`git_detector.py`) — uses `git status` to identify modified, added, and deleted tracked files (fast, used when the folder is a git repo)
2. **mtime detector** (`mtime_detector.py`) — compares file modification times against stored metadata (fallback for non-git folders)

**Incremental reindex** (`incremental.py`):
- Only parses files that changed, were added, or were deleted
- Writes updated content cache files atomically (temp file + rename)
- Merges new section data into the existing index without full re-parse

**Summarization queue** (`summarization_queue.py`):
- New sections added during auto-refresh are queued for AI summarization
- Summarization runs asynchronously and does not block the tool response

This means agents working with local documentation folders never need to call `index_local` again after the initial index — the server keeps itself up to date.
```

**Step 5: Update the storage section atomic writes note**

Current:
> Atomic writes (temp file + rename) prevent corrupt indexes on interrupted writes.

Change to:
> Atomic writes (temp file + rename) prevent corrupt state on interrupted writes — applied to both the index JSON and content cache files during incremental reindex.

**Step 6: Commit**

```bash
git add ARCHITECTURE.md
git commit -m "docs: add auto_refresh module to architecture, update data flow"
```

---

### Task 3: Update USER_GUIDE.md

**Files:**
- Modify: `USER_GUIDE.md`

**Step 1: Replace jDocMunch name references**

Same substitution rules. The file has a few mentions of "jDocMunch" in the community savings meter section.

**Step 2: Add Auto-Refresh workflow**

In the `## Workflows` section, add a new subsection after "Index and Browse a Documentation Folder":

```markdown
### Auto-Refresh for Local Indexes

After the initial `index_local`, nmockdrunk-mcp automatically detects and re-indexes changed files before each tool call. No manual re-indexing needed.

If you add, edit, or delete files in an indexed local folder, the next tool call will pick up the changes transparently.

To force a full clean re-index (e.g. after restructuring a folder):

```
delete_index: { "repo": "local/myfolder" }
index_local:  { "path": "/path/to/myfolder" }
```
```

**Step 3: Update troubleshooting entry for stale index**

Current:
> **Stale index**
> Use `delete_index` followed by `index_local` or `index_repo` to force a clean re-index.

Replace with:
> **Stale index (local)**
> Local indexes auto-refresh on each tool call. If something looks wrong after a large restructure, use `delete_index` followed by `index_local` to force a full clean re-index.
>
> **Stale index (GitHub)**
> GitHub indexes are not auto-refreshed. Use `delete_index` followed by `index_repo` to update.

**Step 4: Commit**

```bash
git add USER_GUIDE.md
git commit -m "docs: add auto-refresh workflow and troubleshooting to user guide"
```

---

### Task 4: Update SPEC.md

**Files:**
- Modify: `SPEC.md`

**Step 1: Replace jDocMunch name references**

The SPEC.md title is `# Technical Specification` — leave as-is. Replace `jdocmunch-mcp` in the overview paragraph with `nmockdrunk-mcp`.

**Step 2: Update index_local tool description**

Current description for `index_local`:
> Walks the local directory with full security controls...

Append to that paragraph:
> After the initial index, nmockdrunk-mcp auto-refreshes this index before each tool call — detecting changed, added, or deleted files and reindexing incrementally without agent intervention.

**Step 3: Confirm GOOGLE_API_KEY is absent from the env vars table**

Check the `## Environment Variables` table. It currently lists: `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, `DOC_INDEX_PATH`, `JDOCMUNCH_SHARE_SAVINGS`. No Google row — nothing to remove.

**Step 4: Search for any remaining Gemini references**

```bash
grep -n -i "gemini\|google_api_key" SPEC.md
```

Remove or update any found.

**Step 5: Commit**

```bash
git add SPEC.md
git commit -m "docs: add auto-refresh behavior to spec, replace jDocMunch name"
```

---

## Verification

After all four commits:

```bash
# Confirm no stray jDocMunch references remain (excluding upstream credit link and package names)
grep -rn "jDocMunch" README.md ARCHITECTURE.md USER_GUIDE.md SPEC.md | grep -v "jgravelle/jdocmunch" | grep -v "jdocmunch-mcp"

# Confirm no Gemini or GOOGLE_API_KEY references remain
grep -rn -i "gemini\|google_api_key" README.md ARCHITECTURE.md USER_GUIDE.md SPEC.md

# Confirm auto-refresh appears in all four files
grep -l "auto.refresh\|auto_refresh" README.md ARCHITECTURE.md USER_GUIDE.md SPEC.md
```

Expected: first two commands produce no output; third lists all four files.
