# jDocMunch-MCP Fix Checklist

## Critical
- [x] **1. Reverse telemetry default** — SKIPPED (keep as-is) — `token_tracker.py:65`: `JDOCMUNCH_SHARE_SAVINGS` defaults to `"1"` (opt-out). Flip to opt-in or remove. Also the persistent `anon_id` UUID is silently created/sent with no user awareness.
- [x] **2. `_resolve_repo` glob injection** — `doc_store.py:298`: `repo` is raw user input used directly in `self.base_path.glob(f"*-{repo}.json")`. Sanitize before glob.
- [x] **3. `get_sections` loads index N times** — `get_sections.py:30-39`: calls `_get_one()` in a loop, each doing a full `load_index()` disk read. Load once, fetch all sections from memory.

## High
- [x] **4. `fetch_gitignore` defined but never called** — `index_repo.py:70-76`: `.gitignore` is fetched as a function but `index_repo()` never applies it. Local indexing respects gitignore; remote does not.
- [x] **5. `_repo_slug` collision** — `doc_store.py:124-125`: `owner="foo-bar", name="baz"` and `owner="foo", name="bar-baz"` both produce slug `foo-bar-baz`, silently overwriting each other.
- [x] **6. `get_sections._meta` incomplete** — fixed in item 3 — `get_sections.py:44-49`: missing `total_tokens_saved` and `cost_avoided` in `_meta`. All other tools include these.
- [x] **7. New httpx client per file in `index_repo`** — `index_repo.py:51,64`: creates a new `AsyncClient` for every file fetch. Share one client across all fetches.

## Medium
- [x] **8. `record_savings` race condition** — `token_tracker.py:53-74`: read/modify/write with no lock. Concurrent calls lose increments. Add a `threading.Lock`.
- [x] **9. `server.py` swallows all exceptions** — `server.py:294-295`: tracebacks lost entirely; log to stderr.
- [x] **10. `_should_skip` substring too broad** — `index_local.py:40-45`: `"build/"` matches `"rebuild/"`. Fix pattern matching to use path components.
- [x] **11. `_parse_response` fragile** — `batch_summarize.py:104`: `if "." in line` triggers on any dotted text. Replace with regex `r"^(\d+)\.\s+(.+)"`.
- [x] **12. Extra `load_index` for `indexed_at`** — `index_local.py:213`, `index_repo.py:207`: unnecessary disk re-read just to get the timestamp. Capture it before saving.
- [x] **13. Pricing wrong/inconsistent** — `token_tracker.py:21-23`: jdocmunch uses `$25/1M` and `$10/1M`; jcodemunch uses `$5/1M` and `$2/1M`. Neither is accurate (Opus 4.6 = $15/1M input). Align both packages.

## Low / Cleanup
- [x] **14. `SKIP_PATTERNS` duplicated** — `index_local.py:22-26` and `index_repo.py:17-21`. Extract to shared constants.
- [x] **15. `rglob("*")` no directory pruning** — `index_local.py:68`: descends into `node_modules/`, `.git/` etc. before filtering. Use `os.walk` with early pruning.
- [x] **16. `BatchSummarizer` / `GeminiBatchSummarizer` duplication** — `batch_summarize.py`: `_build_prompt` is identical in both classes; `_parse_response` nearly so. Extract shared base.
