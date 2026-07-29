"""Semantic Code Search — search the codebase by meaning, not just keywords.

Uses a query-expansion pipeline:
1. LLM expands the natural-language query into a set of search terms
   (synonyms, related concepts, alternative phrasings)
2. ripgrep runs across all known repositories with the expanded terms
3. Results are deduplicated, ranked, and optionally re-scored by an LLM
   for semantic relevance to the original query

Usage::

    from friday.code_search import semantic_search

    results = semantic_search(conn, "find code that handles authentication")
    for r in results:
        print(r.format())
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SearchMatch:
    """A single semantic search match — one file + line number."""

    repo: str
    file_path: str  # relative to repo root
    line_number: int
    line_content: str
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)
    term_matched: str = ""
    relevance_score: float = 1.0
    relevance_reason: str = ""

    def format(self, max_context: int = 3) -> str:
        """Render this match as a human-readable snippet."""
        lines: list[str] = []
        lines.append(f"  {self.file_path}:{self.line_number} "
                      f"(relevance: {self.relevance_score:.2f})")
        if self.relevance_reason:
            lines.append(f"    → {self.relevance_reason}")
        for ctx_line in self.context_before[-max_context:]:
            lines.append(f"    │ {ctx_line}")
        lines.append(f"    → {self.line_content}")
        for ctx_line in self.context_after[:max_context]:
            lines.append(f"    │ {ctx_line}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "line_content": self.line_content[:200],
            "context_before": self.context_before[-3:],
            "context_after": self.context_after[:3],
            "term_matched": self.term_matched,
            "relevance_score": self.relevance_score,
            "relevance_reason": self.relevance_reason,
        }


@dataclass
class SearchResult:
    """Result of a semantic search query."""

    query: str
    expanded_terms: list[str] = field(default_factory=list)
    total_matches: int = 0
    matches_by_repo: dict[str, list[SearchMatch]] = field(default_factory=dict)
    elapsed_ms: int = 0
    errors: list[str] = field(default_factory=list)

    def format(self, max_per_repo: int = 5) -> str:
        """Render the search result as human-readable text."""
        lines: list[str] = []
        lines.append(f"Semantic Search: {self.query}")
        lines.append("=" * 60)
        lines.append(f"  Expanded terms: {', '.join(self.expanded_terms)}")
        lines.append(f"  Total matches:  {self.total_matches}")
        lines.append(f"  Time:           {self.elapsed_ms}ms")
        lines.append("")

        if self.errors:
            lines.append("  ⚠ Issues:")
            for err in self.errors:
                lines.append(f"    {err}")
            lines.append("")

        if not self.matches_by_repo:
            lines.append("  No results found.")
            return "\n".join(lines)

        for repo_name, matches in sorted(self.matches_by_repo.items()):
            lines.append(f"📁 {repo_name} ({len(matches)} match(es))")
            lines.append("-" * 40)
            for m in matches[:max_per_repo]:
                lines.append(m.format())
            if len(matches) > max_per_repo:
                lines.append(f"  ... and {len(matches) - max_per_repo} more")
            lines.append("")

        lines.append("=" * 60)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "expanded_terms": self.expanded_terms,
            "total_matches": self.total_matches,
            "elapsed_ms": self.elapsed_ms,
            "matches_by_repo": {
                repo: [m.to_dict() for m in matches]
                for repo, matches in self.matches_by_repo.items()
            },
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Query expansion (LLM)
# ---------------------------------------------------------------------------

_QUERY_EXPANSION_PROMPT = (
    "You are a code search assistant. Given a natural-language search query, "
    "generate a list of search terms (keywords, symbols, function names, file names, "
    "API names) that a developer would use to find relevant source code.\n\n"
    "Rules:\n"
    "- Output ONLY a JSON array of strings, no other text.\n"
    "- Each term should be a single keyword or short phrase (1-3 words).\n"
    "- Include synonyms, related concepts, alternative phrasings.\n"
    "- Include common abbreviations and acronyms.\n"
    "- Include function/method names that might implement the concept.\n"
    "- Generate 8-15 terms total.\n"
    "- Be specific to programming (e.g. for 'authentication' include 'auth', "
    "'login', 'token', 'session', 'credentials', 'authenticate', 'authorize', 'jwt').\n\n"
    "Query: {query}\n\n"
    "JSON array:"
)


def _expand_query(query: str) -> list[str]:
    """Use LLM to expand a natural-language query into search terms.

    If the LLM is unavailable or fails, fall back to splitting the query
    into words.
    """
    try:
        from .services.llm import _call_structured, _enabled as llm_enabled

        if not llm_enabled():
            return query.split()

        data = _call_structured(_QUERY_EXPANSION_PROMPT.format(query=query), "")
        if isinstance(data, list) and data:
            return [str(t).strip() for t in data if str(t).strip()]
    except Exception:
        pass
    return query.split()


# ---------------------------------------------------------------------------
# ripgrep search
# ---------------------------------------------------------------------------


def _ripgrep_search(
    repo_paths: list[str],
    terms: list[str],
    max_per_term: int = 200,
    context_lines: int = 3,
) -> list[dict]:
    """Run ripgrep across multiple repos for multiple search terms.

    Uses a single ripgrep invocation per term per repo, with ``--no-heading``
    so every output line includes the file path prefix. Parses the context
    output (``-C``) with ``_parse_rg_output``.

    Returns a list of raw match dicts with fields:
      repo, file_path, line_number, line_content, context_before,
      context_after, term_matched
    """
    all_matches: list[dict] = []
    seen: set[tuple[str, str, int, str]] = set()  # (repo, file, line, content[:80])

    for term in terms:
        if not term or len(term) < 2:
            continue
        for repo_path in repo_paths:
            try:
                result = subprocess.run(
                    ["rg", "-n", "--no-heading", "-C", str(context_lines),
                     "-i", "--", term, repo_path],
                    capture_output=True, text=True, timeout=30,
                )
                # Exit code 0 = matches, 1 = no matches.
                if result.returncode not in (0, 1):
                    continue
                if not result.stdout.strip():
                    continue
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue

            repo_name = Path(repo_path).name
            _parse_rg_output(
                result.stdout, repo_name, term, all_matches, seen, max_per_term,
            )

    return all_matches


def _parse_rg_output(
    stdout: str,
    repo_name: str,
    term: str,
    all_matches: list[dict],
    seen: set,
    max_per_term: int,
) -> None:
    """Parse ripgrep context output (``--no-heading`` format) into match dicts.

    ripgrep with ``-n --no-heading -C N`` outputs:
        file:line:content        (match)
        file:line-context        (context before/after)
        --                       (context group separator)

    This extracts file_path, line_number, line_content, and surrounding context.
    """
    if len(all_matches) >= max_per_term:
        return

    lines = stdout.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        # Context separator between match groups.
        if line.strip() == "--":
            i += 1
            continue

        # Parse "file:line:content" (match) or "file:line-content" (context).
        # Find the second colon/dash by working backwards from after the first.
        first_colon = line.find(":")
        if first_colon < 0:
            i += 1
            continue

        file_path = line[:first_colon]
        rest = line[first_colon + 1:]

        # Find the second delimiter to separate line number from content.
        # It's either ":" (match) or "-" (context) after the number.
        delim_pos = -1
        for pos, ch in enumerate(rest):
            if ch == ":" or ch == "-":
                delim_pos = pos
                break

        if delim_pos < 0 or not rest[:delim_pos].isdigit():
            i += 1
            continue

        line_num = int(rest[:delim_pos])
        content = rest[delim_pos + 1:]
        is_match = (rest[delim_pos] == ":")

        if not is_match:
            # Context line (dash separator) — skip, will be collected as context.
            i += 1
            continue

        # ── Gather context before ──────────────────────────────────────
        # Context lines use ``file-line-content`` (dash) format, not
        # ``file:line:content`` (colon). Strip the file_path prefix that
        # was already parsed from the match line, then extract content
        # after the ``-linenum-`` pattern.
        context_before: list[str] = []
        j = i - 1
        while j >= 0:
            prev = lines[j]
            if not prev.strip() or prev.strip() == "--":
                break
            # Check if this line starts with the same file path (context
            # line format) or is a match line for a DIFFERENT file.
            if prev.startswith(file_path) and len(prev) > len(file_path):
                # ``file_path-line_content`` or ``file_path:line_content``
                suffix = prev[len(file_path):]
                if len(suffix) >= 2 and suffix[0] in (":", "-") and suffix[1:].split(":")[0].split("-")[0].isdigit():
                    # Content is after ``file_path-LINENUM-``
                    # Find the delimiter after the line number
                    rest = suffix[1:]  # skip the first colon/dash
                    for ppos, pch in enumerate(rest):
                        if pch == ":" or pch == "-":
                            context_before.insert(0, rest[ppos + 1:])
                            break
            j -= 1

        # ── Gather context after ───────────────────────────────────────
        context_after: list[str] = []
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if not nxt.strip() or nxt.strip() == "--":
                break
            # Same logic: if the line starts with the same file_path, it's
            # a context line for this match (either ``file_path-LINENUM-``
            # for context or ``file_path:LINENUM:`` for another match).
            if nxt.startswith(file_path) and len(nxt) > len(file_path):
                suffix = nxt[len(file_path):]
                if len(suffix) >= 2 and suffix[0] in (":", "-") and suffix[1:].split(":")[0].split("-")[0].isdigit():
                    rest = suffix[1:]
                    for npos, nch in enumerate(rest):
                        if nch == ":" or nch == "-":
                            context_after.append(rest[npos + 1:])
                            break
            j += 1

        key = (repo_name, file_path, line_num, content[:80])
        if key not in seen:
            seen.add(key)
            all_matches.append({
                "repo": repo_name,
                "file_path": file_path,
                "line_number": line_num,
                "line_content": content,
                "context_before": context_before[-3:],
                "context_after": context_after[:3],
                "term_matched": term,
            })
            if len(all_matches) >= max_per_term:
                return

        i += 1


# ---------------------------------------------------------------------------
# Semantic re-ranking (LLM)
# ---------------------------------------------------------------------------

_RERANK_PROMPT = (
    "You are a code search ranker. Given a user's natural-language query and a list "
    "of code search results, score each result for how relevant it is to the query.\n\n"
    "Rules:\n"
    "- Output ONLY a JSON object mapping index -> relevance_score (0.0 to 1.0).\n"
    "- 1.0 = directly implements the concept described in the query\n"
    "- 0.8-0.9 = strongly related, key part of the implementation\n"
    "- 0.5-0.7 = related but not the core implementation\n"
    "- 0.2-0.4 = tangentially related\n"
    "- 0.0-0.1 = not relevant (keyword match but wrong concept)\n"
    "- Also include a brief reason (1 sentence) for each score.\n\n"
    "User query: {query}\n\n"
    "Results:\n{results}\n\n"
    "Output JSON:"
)


def _rerank_results(
    query: str,
    matches: list[dict],
    max_rerank: int = 30,
) -> list[dict]:
    """Use LLM to rerank search results by semantic relevance.

    If the LLM is unavailable, results are returned in their original order
    (grouped by repo, then file).
    """
    try:
        from .services.llm import _call as llm_call, _enabled as llm_enabled

        if not llm_enabled() or not matches:
            return matches

        # Limit the number of results to rerank.
        to_rerank = matches[:max_rerank]
        if len(to_rerank) < 2:
            return matches

        # Build the results block for the LLM.
        results_lines: list[str] = []
        for i, m in enumerate(to_rerank):
            rel_path = m["file_path"]
            line = m["line_content"][:120]
            before = m["context_before"][-1:] if m["context_before"] else []
            after = m["context_after"][:1] if m["context_after"] else []
            ctx = (before + [line] + after)[-3:]
            ctx_str = " ".join(ctx)
            results_lines.append(f"[{i}] {rel_path}:{m['line_number']} — {ctx_str[:150]}")

        if not results_lines:
            return matches

        scores = _call_structured(
            _RERANK_PROMPT.format(
                query=query,
                results="\n".join(results_lines),
            ),
            "",
        )
        if not isinstance(scores, dict) and not isinstance(scores, list):
            return matches

        # Apply scores to the reranked subset.
        for i, m in enumerate(to_rerank):
            if str(i) in scores:
                score_obj = scores[str(i)]
                if isinstance(score_obj, dict):
                    m["relevance_score"] = float(score_obj.get("score", score_obj.get("relevance_score", 0.5)))
                    m["relevance_reason"] = str(score_obj.get("reason", score_obj.get("relevance_reason", "")))
                else:
                    m["relevance_score"] = float(score_obj)
                    m["relevance_reason"] = ""
            elif isinstance(scores, list) and i < len(scores):
                m["relevance_score"] = float(scores[i])

        # Sort by relevance score descending.
        matches.sort(key=lambda m: m.get("relevance_score", 0), reverse=True)
    except Exception:
        pass

    return matches


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def semantic_search(
    conn,
    query: str,
    max_results: int = 30,
    rerank: bool = True,
    expand_query: bool = True,
) -> SearchResult:
    """Run a semantic code search across all known repositories.

    Args:
        conn: Database connection.
        query: Natural-language search query.
        max_results: Maximum number of results to return.
        rerank: Whether to LLM-rerank results by semantic relevance.
        expand_query: Whether to LLM-expand the query into search terms.

    Returns:
        A ``SearchResult`` with matches grouped by repository.
    """
    start = time.monotonic()
    result = SearchResult(query=query)

    # ── Get repositories ───────────────────────────────────────────
    try:
        from .db import get_repositories
        repos = get_repositories(conn)
    except Exception as exc:
        result.errors.append(f"Could not load repositories: {exc}")
        return result

    if not repos:
        result.errors.append("No repositories found in workspace.")
        return result

    repo_paths: list[str] = []
    for r in repos:
        rpath = r.path if hasattr(r, "path") else r.get("path", "")
        if rpath and Path(rpath).exists():
            repo_paths.append(rpath)

    if not repo_paths:
        result.errors.append("No repository paths exist on disk.")
        return result

    # ── Query expansion ──────────────────────────────────────────────
    if expand_query:
        terms = _expand_query(query)
    else:
        terms = query.split()

    # Always include the original query words.
    original_terms = set(query.lower().split())
    for t in original_terms:
        if t not in terms:
            terms.append(t)
    result.expanded_terms = terms

    # ── Check if ripgrep is available ────────────────────────────────
    try:
        subprocess.run(["rg", "--version"], capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        result.errors.append("ripgrep (rg) is not installed. Install with: sudo apt install ripgrep")
        return result

    # ── Run search ─────────────────────────────────────────────────
    raw_matches = _ripgrep_search(repo_paths, terms)

    if not raw_matches:
        result.total_matches = 0
        result.elapsed_ms = int((time.monotonic() - start) * 1000)
        return result

    # ── Rerank ───────────────────────────────────────────────────────
    if rerank:
        raw_matches = _rerank_results(query, raw_matches)

    # ── Group by repo, limit total ────────────────────────────────────
    result.total_matches = len(raw_matches)
    for m in raw_matches[:max_results]:
        repo = m["repo"]
        if repo not in result.matches_by_repo:
            result.matches_by_repo[repo] = []
        result.matches_by_repo[repo].append(SearchMatch(**m))

    result.elapsed_ms = int((time.monotonic() - start) * 1000)
    return result
