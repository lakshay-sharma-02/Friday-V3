"""Cross-Project Knowledge Correlation.

Two-pass correlation between all ingested repositories:
1. Structural pass (cheap) — compares error-handling fingerprints, module
   boundaries, config structure, dependency overlap. Weighted by recency of
   activity.
2. Semantic pass (LLM) — for pairs above a structural threshold, reads recent
   commit messages and project docs to find conceptual overlap.

Results are surfaced as Insights (type=OPPORTUNITY) in the existing engine.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from .db import (
    connect,
    get_repositories,
    get_project_docs,
    get_all_project_docs,
    upsert_project_doc,
    insert_correlation_result,
    get_repo_commit_count_recent,
    insert_insight,
    now_iso,
)
from .insight.models import Insight, InsightType, InsightStatus, InsightConfidence

#: Pairs with adjusted structural score >= this enter the LLM semantic pass.
_SEMANTIC_THRESHOLD = 0.6
#: Only semantic confidence >= this creates an Insight.
_INSIGHT_THRESHOLD = 0.8
#: Recency half-life in days — a repo with zero commits in this window has
#: its volatility multiplier decayed to a small epsilon.
_RECENCY_WINDOW_DAYS = 30
#: Max structural score a pair can have before recency weighting.
_MAX_STRUCTURAL = 10.0

# Patterns for project doc discovery
_DOC_PATTERNS = [
    re.compile(r"docs?/.*prd", re.IGNORECASE),
    re.compile(r"docs?/.*architecture", re.IGNORECASE),
    re.compile(r"docs?/.*design", re.IGNORECASE),
    re.compile(r"docs?/.*spec", re.IGNORECASE),
    re.compile(r"prd\.md$", re.IGNORECASE),
    re.compile(r"architecture\.md$", re.IGNORECASE),
    re.compile(r"design\.md$", re.IGNORECASE),
    re.compile(r"spec\.md$", re.IGNORECASE),
    re.compile(r"CHANGELOG\.md$"),
    re.compile(r"README\.md$"),
]

# ---------------------------------------------------------------------------
# Doc scanner — discovers and ingests project docs from the filesystem
# ---------------------------------------------------------------------------


def scan_project_docs(conn) -> int:
    """Scan all ingested repos for PRD/design/architecture docs.

    Discovers markdown files matching known patterns, computes checksums,
    and upserts new/changed docs into ``project_docs``.

    Returns the count of new or updated docs.
    """
    import hashlib

    repos = get_repositories(conn)
    changes = 0

    for repo in repos:
        repo_path = repo.path
        if not repo_path or not Path(repo_path).exists():
            continue
        root = Path(repo_path)
        # Walk docs/ directory and repo root for matching files.
        candidates: list[Path] = []
        docs_dir = root / "docs"
        if docs_dir.exists() and docs_dir.is_dir():
            candidates.extend(docs_dir.rglob("*.md"))
        # Also check repo root for PRD/design docs.
        candidates.extend(root.glob("*PRD*.md"))
        candidates.extend(root.glob("*prd*.md"))
        candidates.extend(root.glob("ARCHITECTURE*.md"))
        candidates.extend(root.glob("architecture*.md"))
        candidates.extend(root.glob("CHANGELOG*.md"))
        candidates.extend(root.glob("README.md"))

        seen = set()
        for fp in candidates:
            if not fp.is_file():
                continue
            resolved = str(fp.relative_to(root))
            if resolved in seen:
                continue
            seen.add(resolved)

            # Check if it matches a doc pattern.
            if not any(p.search(resolved) for p in _DOC_PATTERNS):
                continue

            try:
                content = fp.read_text(encoding="utf-8", errors="ignore")
            except (OSError, IOError):
                continue
            if not content.strip():
                continue

            checksum = hashlib.sha256(content.encode()).hexdigest()[:16]
            # Map filename to doc_type.
            low = resolved.lower()
            if "prd" in low:
                doc_type = "prd"
            elif "architecture" in low:
                doc_type = "architecture"
            elif "changelog" in low:
                doc_type = "changelog"
            elif "design" in low or "spec" in low:
                doc_type = "design"
            else:
                doc_type = "readme"

            title = fp.stem.replace("_", " ").replace("-", " ").title()

            upsert_project_doc(conn, {
                "repo_id": repo.id,
                "path": resolved,
                "title": title,
                "content": content,
                "doc_type": doc_type,
                "checksum": checksum,
            })
            changes += 1

    return changes


# ---------------------------------------------------------------------------
# Structural pass — compare code patterns across repos
# ---------------------------------------------------------------------------


def structural_pass(conn) -> list[dict]:
    """Run structural correlation across all repo pairs.

    Returns scored pairs sorted by adjusted score descending.
    Each pair dict::

        {
            "repo_a_id": int, "repo_b_id": int,
            "repo_a_name": str, "repo_b_name": str,
            "structural_score": float,  # raw 0-MAX_STRUCTURAL
            "volatility": float,        # 0.0-1.0 recency multiplier
            "adjusted_score": float,    # structural_score * volatility
            "evidence": list[str],
        }
    """
    repos = get_repositories(conn)
    results: list[dict] = []

    # Precompute commit activity for recency weighting.
    recent_commits: dict[int, int] = {}
    max_commits = 0
    for r in repos:
        if r.id is None:
            continue
        cnt = get_repo_commit_count_recent(conn, r.id, _RECENCY_WINDOW_DAYS)
        recent_commits[r.id] = cnt
        if cnt > max_commits:
            max_commits = cnt

    for i in range(len(repos)):
        a = repos[i]
        if a.id is None:
            continue
        for j in range(i + 1, len(repos)):
            b = repos[j]
            if b.id is None:
                continue

            score, evidence = _compare_pair(conn, a, b)
            if score <= 0:
                continue

            # Recency/volatility multiplier.
            ca = recent_commits.get(a.id, 0)
            cb = recent_commits.get(b.id, 0)
            top_activity = max(ca, cb)
            if max_commits > 0:
                volatility = top_activity / max_commits
            else:
                volatility = 0.01  # epsilon — no activity data

            adjusted = score * volatility

            results.append({
                "repo_a_id": a.id,
                "repo_b_id": b.id,
                "repo_a_name": a.name,
                "repo_b_name": b.name,
                "structural_score": round(score, 3),
                "volatility": round(volatility, 3),
                "adjusted_score": round(adjusted, 3),
                "evidence": evidence,
            })

    results.sort(key=lambda r: r["adjusted_score"], reverse=True)
    return results


def _compare_pair(conn, a, b) -> tuple[float, list[str]]:
    """Compare two repositories structurally. Returns (score, evidence_list)."""
    evidence: list[str] = []
    score = 0.0

    # 1. Language overlap (weighted, not just boolean).
    try:
        a_langs = {r["language"]: r["file_count"] for r in
                   conn.execute("SELECT language, file_count FROM languages WHERE repo_id = ?",
                                (a.id,)).fetchall()}
        b_langs = {r["language"]: r["file_count"] for r in
                   conn.execute("SELECT language, file_count FROM languages WHERE repo_id = ?",
                                (b.id,)).fetchall()}
        shared_langs = set(a_langs) & set(b_langs)
        if shared_langs:
            # Weight by min file count — a shared language with 100+ files in both is stronger.
            total_weight = sum(min(a_langs.get(l, 0), b_langs.get(l, 0)) for l in shared_langs)
            if total_weight > 0:
                lang_score = min(2.0, total_weight / 200.0)
                score += lang_score
                top = sorted(shared_langs, key=lambda l: min(a_langs[l], b_langs[l]), reverse=True)[:3]
                evidence.append(f"Shared languages: {', '.join(top)}")
    except Exception:
        pass

    # 2. Shared technologies (tech stack overlap).
    try:
        a_techs = {r["tech"] for r in conn.execute(
            "SELECT tech FROM technologies WHERE repo_id = ?", (a.id,)).fetchall()}
        b_techs = {r["tech"] for r in conn.execute(
            "SELECT tech FROM technologies WHERE repo_id = ?", (b.id,)).fetchall()}
        shared_tech = a_techs & b_techs
        if shared_tech:
            t = len(shared_tech)
            score += min(3.0, t * 0.5)
            evidence.append(f"Shared tech: {', '.join(sorted(shared_tech)[:5])}")
    except Exception:
        pass

    # 3. Config structure similarity (key naming conventions).
    try:
        a_cfgs = {r["path"] for r in conn.execute(
            "SELECT path FROM project_docs WHERE repo_id = ? AND doc_type = 'readme'",
            (a.id,)).fetchall()}
        # Use file-level signals from the filesystem.
        a_root = Path(a.path) if a.path else None
        b_root = Path(b.path) if b.path else None
        if a_root and b_root and a_root.exists() and b_root.exists():
            a_configs = set()
            b_configs = set()
            for pat in ("*.json", "*.yaml", "*.yml", "*.toml", "*.cfg", "*.conf"):
                a_configs.update(f.name for f in a_root.glob(pat) if f.is_file())
                b_configs.update(f.name for f in b_root.glob(pat) if f.is_file())
            shared_cfg = a_configs & b_configs
            if shared_cfg:
                score += min(1.0, len(shared_cfg) * 0.2)
                evidence.append(f"Shared config files: {', '.join(sorted(shared_cfg)[:3])}")
    except Exception:
        pass

    # 4. Error-handling similarity (custom exception classes in code).
    try:
        # Scan Python files for exception class definitions as a proxy.
        a_exceptions = _find_custom_exceptions(Path(a.path)) if a.path else set()
        b_exceptions = _find_custom_exceptions(Path(b.path)) if b.path else set()
        shared_ex = a_exceptions & b_exceptions
        if shared_ex:
            score += min(1.0, len(shared_ex) * 0.3)
            evidence.append(f"Shared exception patterns: {', '.join(sorted(shared_ex)[:3])}")
    except Exception:
        pass

    return min(score, _MAX_STRUCTURAL), evidence


def _find_custom_exceptions(root: Path) -> set[str]:
    """Find custom exception class names in Python source files."""
    exceptions: set[str] = set()
    if not root.exists():
        return exceptions
    pattern = re.compile(r"^\s*class\s+(\w+Error)\s*\(", re.MULTILINE)
    for py_file in root.rglob("*.py"):
        if ".venv" in str(py_file) or "__pycache__" in str(py_file):
            continue
        try:
            text = py_file.read_text(encoding="utf-8", errors="ignore")
        except (OSError, IOError):
            continue
        for m in pattern.finditer(text):
            exceptions.add(m.group(1))
    return exceptions


# ---------------------------------------------------------------------------
# Semantic pass — LLM judgment on conceptual overlap
# ---------------------------------------------------------------------------


def semantic_pass(conn, pairs: list[dict]) -> list[dict]:
    """Run LLM semantic analysis on repo pairs above the structural threshold.

    For each pair, gathers recent commit context + project docs and asks the
    LLM whether the two projects share a conceptual domain.

    Returns enriched pairs with ``semantic_score``, ``semantic_reason``,
    ``semantic_label``, ``semantic_confidence``.
    """
    for pair in pairs:
        if pair["adjusted_score"] < _SEMANTIC_THRESHOLD:
            continue

        a_id = pair["repo_a_id"]
        b_id = pair["repo_b_id"]

        # Gather context for both repos.
        a_docs = get_project_docs(conn, a_id)
        b_docs = get_project_docs(conn, b_id)

        a_summary = _repo_context(conn, a_id, a_docs, pair["repo_a_name"])
        b_summary = _repo_context(conn, b_id, b_docs, pair["repo_b_name"])

        result = _call_llm_semantic(a_summary, b_summary)
        if result:
            pair["semantic_score"] = result.get("score", 0.0)
            pair["semantic_reason"] = result.get("reason", "")
            pair["semantic_label"] = result.get("label", "")
            pair["semantic_confidence"] = result.get("confidence", "low")

    return pairs


def _repo_context(conn, repo_id: int, docs: list[dict], name: str) -> str:
    """Build a compact summary of a repo for the LLM semantic pass."""
    parts = [f"Project: {name}"]

    # Recent commit messages (from snapshots if available, else README).
    try:
        rows = conn.execute(
            "SELECT observed_at FROM snapshots WHERE repo_path = "
            "(SELECT path FROM repositories WHERE id = ?) "
            "ORDER BY observed_at DESC LIMIT 10",
            (repo_id,)).fetchall()
        if rows:
            dates = [r["observed_at"][:10] for r in rows if r["observed_at"]]
            if dates:
                parts.append(f"Recent activity: {', '.join(dates)}")
    except Exception:
        pass

    # Project docs (truncated).
    for d in docs[:3]:
        content = d.get("content", "")[:500]
        parts.append(f"[{d.get('doc_type', 'doc')}] {d.get('title', '')}:\n{content}")

    return "\n\n".join(parts)


_SEMANTIC_SYSTEM_PROMPT = (
    "You analyze pairs of software projects and determine if they share a "
    "conceptual domain or could benefit from integration.\n\n"
    "Given descriptions of two projects, output ONLY valid JSON with:\n"
    '- "score": float 0.0-1.0 (how conceptually related they are)\n'
    '- "reason": str (one sentence explaining the relationship)\n'
    '- "label": str (short label like "payment processing" or "data pipeline")\n'
    '- "confidence": str ("high", "medium", or "low")\n\n'
    "Be conservative — only return high scores when there's genuine structural "
    "or semantic overlap, not superficial similarities.\n"
    "Respond with ONLY the JSON object, no markdown, no explanation."
)


def _call_llm_semantic(a_summary: str, b_summary: str) -> Optional[dict]:
    """Call the LLM for semantic analysis of two repos."""
    try:
        from .services.llm import _call_structured

        user = (
            f"Project A:\n{a_summary}\n\n"
            f"Project B:\n{b_summary}\n\n"
            "Are these projects conceptually related? Output JSON only."
        )
        data = _call_structured(
            _SEMANTIC_SYSTEM_PROMPT,
            user,
            required_keys=["score"],
        )
        if isinstance(data, dict) and "score" in data:
            return data
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Suggestion engine — promote high-confidence correlations to Insights
# ---------------------------------------------------------------------------


def suggest(conn, results: list[dict]) -> list[str]:
    """Promote high-confidence correlations to Insight objects.

    Only pairs with ``semantic_score >= _INSIGHT_THRESHOLD`` (0.8) create
    Insights. Medium/low scores are logged to ``correlation_results`` but
    never surfaced.

    Returns list of Insight ids created.
    """
    created: list[str] = []
    now = now_iso()

    for r in results:
        semantic_score = r.get("semantic_score") or r.get("adjusted_score", 0)
        if semantic_score < _INSIGHT_THRESHOLD:
            continue

        label = r.get("semantic_label") or r.get("repo_a_name", "") + "↔" + r.get("repo_b_name", "")
        reason = r.get("semantic_reason") or "Structural similarity detected"
        a_name = r.get("repo_a_name", "")
        b_name = r.get("repo_b_name", "")

        # Persist the correlation result regardless of threshold.
        insert_correlation_result(conn, {
            "repo_a_id": r["repo_a_id"],
            "repo_b_id": r["repo_b_id"],
            "structural_score": r.get("structural_score", 0),
            "semantic_score": r.get("semantic_score"),
            "semantic_reason": r.get("semantic_reason"),
            "semantic_label": label,
            "semantic_confidence": r.get("semantic_confidence", "low"),
            "volatility": r.get("volatility", 0),
        })

        # Only high-confidence pairs create an Insight.
        conf_str = r.get("semantic_confidence", "low")
        if semantic_score < _INSIGHT_THRESHOLD:
            continue

        confidence = (
            InsightConfidence.STRONG if semantic_score >= 0.9
            else InsightConfidence.MEDIUM if semantic_score >= 0.8
            else InsightConfidence.WEAK
        )

        insight_id = f"correlate:{a_name}:{b_name}:{now[:10]}"
        ins = Insight(
            id=insight_id,
            type=InsightType.OPPORTUNITY,
            title=f"Cross-project correlation: {label}",
            statement=(
                f"{a_name} and {b_name} show strong conceptual overlap "
                f"(score={semantic_score:.2f}). {reason} "
                f"Run `friday correlate --detail {a_name} {b_name}` for details."
            ),
            status=InsightStatus.CANDIDATE,
            confidence=confidence,
            understanding_ids=[],
            initiative_ids=[],
            knowledge_ids=[],
            build_at=now,
            created_at=now,
            updated_at=now,
        )
        insert_insight(conn, [ins.to_row()])
        created.append(insight_id)

    return created


# ---------------------------------------------------------------------------
# Orchestration — run the full pipeline
# ---------------------------------------------------------------------------


def run_correlation(conn) -> list[dict]:
    """Run the full cross-project correlation pipeline.

    1. Scan for new/changed project docs.
    2. Structural pass over all repo pairs (recency-weighted).
    3. Semantic pass (LLM) for pairs above threshold.
    4. Suggest — promote high-confidence results to Insights.

    Returns list of high-confidence correlation dicts that became Insights.
    """
    # Step 1: Doc scan.
    doc_changes = scan_project_docs(conn)
    if doc_changes:
        from .daemon import _log
        _log(f"Cross-project: {doc_changes} project doc(s) ingested.")

    # Step 2: Structural pass.
    pairs = structural_pass(conn)
    if not pairs:
        return []

    # Step 3: Semantic pass — only pairs above threshold.
    high_pairs = [p for p in pairs if p["adjusted_score"] >= _SEMANTIC_THRESHOLD]
    if high_pairs:
        high_pairs = semantic_pass(conn, high_pairs)

    # Step 4: Suggest.
    all_results = high_pairs + [p for p in pairs if p["adjusted_score"] < _SEMANTIC_THRESHOLD]
    created = suggest(conn, all_results)

    # Return the subset that became Insights.
    return [p for p in high_pairs if (p.get("semantic_score") or 0) >= _INSIGHT_THRESHOLD]


# ---------------------------------------------------------------------------
# CLI-friendly report
# ---------------------------------------------------------------------------


def format_correlations(results: list[dict]) -> str:
    """Render correlation results as a human-readable report."""
    if not results:
        return "No cross-project correlations found."

    lines = ["Cross-Project Correlations", "=" * 40, ""]
    for i, r in enumerate(results[:20], 1):
        a = r.get("repo_a_name", "?")
        b = r.get("repo_b_name", "?")
        score = r.get("adjusted_score", r.get("structural_score", 0))
        label = r.get("semantic_label", "")
        reason = r.get("semantic_reason", "")
        vol = r.get("volatility", 0)

        label_str = f" — {label}" if label else ""
        lines.append(f"{i}. {a} ↔ {b}{label_str}")
        lines.append(f"   Score: {score:.3f} (volatility={vol:.3f})")
        if reason:
            lines.append(f"   {reason}")
        lines.append("")

    if len(results) > 20:
        lines.append(f"... and {len(results) - 20} more")
    return "\n".join("\n".join(lines))
