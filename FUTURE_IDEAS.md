# Future Ideas

## Search Index

Currently `DocIndex.search()` does a linear scan over all sections (O(N)) — iterating every section and scoring it against the query. This is fine up to ~15k sections (sub-200ms) but would degrade noticeably beyond that.

### The idea

Replace the linear scan with an inverted index: a dict mapping each word to the list of section IDs that contain it. A query would look up only the relevant sections instead of touching every one.

### Why we haven't done it

The current performance is good enough for the doc corpus sizes this server is realistically used with. The complexity isn't worth it until there's an actual slowness problem.

### When to revisit

If search latency becomes noticeable — likely when section counts exceed ~50k. At that point the JSON index load time would also need addressing (currently the index is re-parsed from disk on every tool call, which is intentional to ensure post-refresh correctness, but expensive at scale).
