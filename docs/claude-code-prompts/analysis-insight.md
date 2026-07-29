# Analysis & Insight — Prompt for Claude Code

## Intent
FRIDAY in the MCU answers "why" and "what if" — not just "what." Tony asks "What happens if I reroute power?" and she tells him. Your Friday has knowledge but can't trace forward or backward through a codebase's dependency graph. This covers: **Semantic Code Search**, **Change Impact Analysis**, and **Codebase Narrative**.

## What to build

### Phase 1: Semantic Code Search

Create `src/friday/code_search.py`. The `query.py` file says "No embeddings, no semantic search — simple SQL + filtering." Fix that.

**What it does:**
- Indexes your workspace code into a lightweight searchable store
- Answers: "Find where we handle JWT tokens" / "Show me all API routes that touch user data" / "What pattern do we use for database migrations?"
- Returns file paths + line numbers + 3-line context snippets

**Approach:**
- Use `sentence-transformers` (local, no API) or `llama.cpp` embeddings to encode code chunks
- Chunk by: function boundaries, class boundaries, file-level docstrings
- Store embeddings in a local SQLite table with ANN search or simple cosine via numpy
- Query: embed the question, find top-5 most similar code chunks, return file:line context
- **Deterministic fallback**: If embedding model unavailable, fall back to ripgrep-aware grep with keyword expansion ("JWT" → "jwt token auth authentication bearer")

**Key design:**
- Index is built on demand (`friday code-search --rebuild`) or incrementally on each daemon cycle
- Embedding DB is separate from the main friday.db — can be deleted/rebuilt without affecting core state
- Only indexes files tracked by git (obeys .gitignore)
- Language-aware chunking: for Python, split on `def`/`class`; for Rust, `fn`/`impl`; fallback to paragraph-split
- Results are ordered by similarity score, file:line, and snippet
- CLI: `friday search "jwt auth"` → returns results, `friday search "jwt auth" --repo friday` → scope to repo

### Phase 2: Change Impact Analysis

Create `src/friday/impact.py`. The answer to "If I rename X, what breaks?"

**What it does:**
- Given a symbol, file, or function name, trace all reverse dependencies
- Produce a tree: "This symbol is imported/used by N files across M projects"
- Categorize: DIRECT caller, TRANSITIVE caller, TEST file, CONFIG reference

**Approach:**
- Build a **static import graph** per workspace: file → what it imports, project → what it depends on
- This is NOT a runtime analysis — it's a call-frequency and import-reference map
- For Python: parse imports with `ast` (stdlib, already a dep)
- For other languages: regex-based extraction of `import`/`use`/`require` patterns
- Store as `code_dependencies` table: `(file_path, symbol, dep_type, resolved_path)`
- `friday impact <symbol>` → traverses the graph forward (what references this?) and backward (what does this depend on?)

**Output format:**
```
Impact of 'TokenAuth.verify()':
  DIRECT (12 files)
    src/friday/auth.py:45 — import
    src/friday/api/routes.py:102 — direct call
    ...
  TRANSITIVE (2 files)
    src/friday/middleware.py:30 → routes.py:102
  TEST (4 files)
    tests/test_auth.py:15, 22, 40, 77
  CONFIG (1 file)
    config/default.yaml:17 — "auth_verify_enabled"
```

**Deterministic fallback:** grep-based reference counting (no parser needed):

### Phase 3: Codebase Narrative

Create `src/friday/narrative.py`. The answer to "Tell me the story of this module."

**What it does:**
- Given a file path or module name, trace its git history as a narrative
- Not a log dump — structured: birth, evolution, major refactors, current state
- Branch: when was it created, by whom, for what purpose
- Growth: how has it changed over time (LOC history per commit)
- Crises: bug-fix clusters, reverts, major rewrites
- Contributors: who touches it most, who's the domain expert
- Relations: what was added alongside it, what was refactored when this changed

**Approach:**
- All data from git log + blame — no LLM needed for the structural narrative
- `git log --follow -- <file>` for the history
- `git diff --stat` per commit for LOC deltas
- Cluster commits by: feature work, bug fixes, refactors (heuristic based on commit message keywords)
- LLM OPTIONAL: if available, summarize the narrative into 3-5 sentences. "This module was born as a 50-line utility for JWT decoding. Over 14 commits and 8 months, it grew to 340 lines as rate limiting and caching were added. The biggest change was in April when the caching layer was extracted. 3 people have contributed; [name] is the primary author."

**Output formats:**
- `friday narrative src/friday/auth.py` → structured narrative
- `friday narrative src/friday/auth.py --summary` → LLM summary if available, else 3-line structural summary
- `friday narrative src/friday/auth.py --timeline` → compact timeline (commit dates, authors, LOC delta)

## Files to touch
- `src/friday/code_search.py` (new) — embeddings, chunking, search
- `src/friday/impact.py` (new) — dependency graph, impact reporter
- `src/friday/narrative.py` (new) — git history narrative engine
- `src/friday/db.py` — add `code_embeddings`, `code_dependencies` tables (separate DB or schema)
- `src/friday/cli.py` — add `friday search`, `friday impact`, `friday narrative` commands
- `src/friday/daemon.py` — optional incremental reindex of code_search on cycles
- `src/friday/query.py` — leave unchanged, this is supplementary
- `pyproject.toml` — add `sentence-transformers` as optional dep
- `tests/test_code_search.py` (new)
- `tests/test_impact.py` (new)
- `tests/test_narrative.py` (new)

## Acceptance criteria
1. `friday search "jwt token validation"` → returns 5 file:line results with snippets, ordered by relevance
2. `friday impact "verify_auth"` → shows direct/transitive/test/config breakdown
3. Import graph is built incrementally — adding a file reindexes only that file
4. `friday narrative src/friday/auth.py` → shows birth date, author, LOC history, key changes, contributors
5. `friday narrative src/friday/auth.py --summary` → 3-sentence LLM narrative (if LLM enabled) OR structural fallback
6. No embedding model available → search falls back to ripgrep-grep mode, works without error
7. Impact analysis for a nonexistent symbol → "No references found"
8. Narrative for a deleted file → "File was deleted on [date]. Last state: ..."
