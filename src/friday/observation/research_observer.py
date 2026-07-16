"""ResearchObserver (Milestone 7.6).

A NEW observer for the frozen Observation Engine. It observes *engineering
research activity* — documentation, RFCs, papers, specs, API references, blogs,
talks the user intentionally opens — and emits deterministic engineering
observations that plug into the existing engine. No engine, context, or brain
changes.

DESIGN (privacy-first, metadata-only):

  This observer is a PURE READER. It reads a list of research *resource events*
  through one of several interchangeable providers and maps each to Observation
  facts:

    - FixtureProvider     — offline list of resource dicts (default; tests).
    - BrowserExportProvider  / BookmarkProvider / HistoryProvider
                            — future connectors reading an explicit export file
                              (the user must OPT IN by pointing FRIDAY_RESEARCH_
                              EXPORT at it; nothing is read automatically).

  Only whitelisted METADATA is ever read or emitted: url, host, title, timestamp,
  visit duration, category, language, repeated visits, bookmark, read completion.
  PAGE CONTENTS, query strings, cookies, form data, and personal/social/email
  browsing are NEVER read and structurally cannot be emitted. Providers that pull
  from a browser export apply a domain/category WHITELIST and ignore everything
  else (social media, email, personal sites).

Observations emitted per resource (subject = url, or host when url absent):
  host, title, category, language, visited_at, visit_duration_s,
  repeated_visits, bookmarked, read_completion.

Run-level engineering signals (evidence-backed, no LLM):
  researching_<topic>   (Inferred)  e.g. researching_operating_systems,
                        reading_ai_infrastructure, repeated_rust_learning,
                        heavy_database_research, authentication_research,
                        networking_research, compiler_research,
                        filesystem_research, kernel_research.
  The topic taxonomy is a frozen deterministic table derived from the
  resource category + host + language metadata. No speculation.

Confidence follows the Observation Engine vocabulary (Observed/Derived/Inferred).
No LLM, no embeddings, no planner, no agents, no browser extension, no daemon.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol
from urllib.parse import urlparse

from .interface import Health, Observer, ObserverHealth
from .model import Confidence, Observation

# --- Config ----------------------------------------------------------------

# Colon-separated explicit engineering categories or domains to accept.
RESEARCH_EXPORT_ENV = "FRIDAY_RESEARCH_EXPORT"
# At least this many resources in a topic to infer "researching_<topic>".
RESEARCH_TOPIC_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Classification (deterministic, frozen, no LLM)
# ---------------------------------------------------------------------------


class Category:
    DOCUMENTATION = "Documentation"
    RFC = "RFC"
    RESEARCH_PAPER = "Research Paper"
    SPECIFICATION = "Specification"
    API_REFERENCE = "API Reference"
    TUTORIAL = "Tutorial"
    BLOG = "Blog"
    ISSUE_DISCUSSION = "Issue Discussion"
    PR_DISCUSSION = "Pull Request Discussion"
    CONFERENCE_TALK = "Conference Talk"
    VIDEO = "Video"
    UNKNOWN = "Unknown"


# Host substring -> category hint (deterministic, no LLM).
HOST_CATEGORY = {
    "docs.rs": Category.DOCUMENTATION,
    "doc.rust-lang.org": Category.DOCUMENTATION,
    "developer.mozilla.org": Category.DOCUMENTATION,
    "docs.python.org": Category.DOCUMENTATION,
    "www.kernel.org": Category.DOCUMENTATION,
    "datatracker.ietf.org": Category.RFC,
    "tools.ietf.org": Category.RFC,
    "rfc-editor.org": Category.RFC,
    "arxiv.org": Category.RESEARCH_PAPER,
    "dl.acm.org": Category.RESEARCH_PAPER,
    "link.springer.com": Category.RESEARCH_PAPER,
    "spec.openapis.org": Category.SPECIFICATION,
    "w3.org": Category.SPECIFICATION,
    "developer.mozilla.org": Category.DOCUMENTATION,
    "docs.github.com": Category.DOCUMENTATION,
    "platform.openai.com": Category.API_REFERENCE,
    "api.": Category.API_REFERENCE,
    "supabase.com": Category.API_REFERENCE,
    "youtube.com": Category.VIDEO,
    "youtu.be": Category.VIDEO,
}


def classify_research(host: str, category: Optional[str] = None) -> str:
    """Deterministic host/category -> Category. Unknown maps to Unknown."""
    if category and category in vars(Category).values():
        return category
    h = (host or "").lower()
    for needle, cat in HOST_CATEGORY.items():
        if needle in h:
            return cat
    return Category.UNKNOWN


# Topic taxonomy: (category, host-substring, language) -> engineering topic.
# Frozen table; no LLM. Used to derive researching_<topic> signals.
def topic_of(host: str, category: str, language: Optional[str]) -> Optional[str]:
    h = (host or "").lower()
    cat = category or Category.UNKNOWN
    lang = (language or "").lower()

    # Language-driven learning topics.
    if lang in ("rust", "rs"):
        return "rust_learning"
    if lang in ("c", "cpp", "c++"):
        return "systems_programming"

    # Host/category driven research topics.
    if "kernel.org" in h or "linux" in h:
        return "operating_systems"
    if "openai" in h or "anthropic" in h or "ai" in h and "infra" in h \
            or cat == Category.RESEARCH_PAPER and ("ai" in h or "ml" in h):
        return "ai_infrastructure"
    if "postgres" in h or "supabase" in h or "mysql" in h or "sqlite" in h \
            or "database" in h or "db." in h:
        return "databases"
    if "auth" in h or "oauth" in h or "jwt" in h:
        return "authentication"
    if "network" in h or "tcp" in h or "http" in h and "docs" in h:
        return "networking"
    if "llvm" in h or "compiler" in h or "rustc" in h:
        return "compiler"
    if "filesystem" in h or "fs." in h or "ext4" in h or "zfs" in h:
        return "filesystem"
    if "kernel" in h:
        return "kernel"
    if cat in (Category.RFC, Category.SPECIFICATION):
        return "standards"
    if cat in (Category.RESEARCH_PAPER,):
        return "research_papers"
    return None


# ---------------------------------------------------------------------------
# Research resource model
# ---------------------------------------------------------------------------


class ResearchResource:
    """One engineering resource the user intentionally opened.

    Built from a provider dict. Only metadata; never page contents.
    """

    def __init__(
        self,
        url: str,
        title: str = "",
        timestamp: Optional[str] = None,
        duration_s: Optional[float] = None,
        category: Optional[str] = None,
        language: Optional[str] = None,
        bookmarked: bool = False,
        read_completion: Optional[float] = None,
        repeated_visits: int = 1,
    ) -> None:
        # Sanitize the URL up front: drop query string and fragment so secrets
        # (token=..., #session) never enter any observation. Host + path only.
        self.url = _strip_query_fragment(url)
        self.title = title or ""
        self.timestamp = timestamp
        self.duration_s = duration_s
        self.language = language
        self.bookmarked = bookmarked
        self.read_completion = read_completion
        self.repeated_visits = repeated_visits or 1
        parsed = urlparse(self.url or "")
        self.host = parsed.netloc or (self.url or "")
        self.category = classify_research(self.host, category)

    @classmethod
    def from_dict(cls, d: dict) -> "ResearchResource":
        return cls(
            url=d.get("url", ""),
            title=d.get("title", ""),
            timestamp=d.get("timestamp"),
            duration_s=d.get("duration_s"),
            category=d.get("category"),
            language=d.get("language"),
            bookmarked=bool(d.get("bookmarked", False)),
            read_completion=d.get("read_completion"),
            repeated_visits=d.get("repeated_visits", 1),
        )


# ---------------------------------------------------------------------------
# Provider seam (mirrors GitHubObserver)
# ---------------------------------------------------------------------------


class ResearchProvider(Protocol):
    def fetch(self) -> list[dict]:
        ...

    def describe(self) -> str:
        ...


def _configured_export() -> Optional[Path]:
    raw = os.environ.get(RESEARCH_EXPORT_ENV)
    return Path(raw).expanduser() if raw else None


class FixtureProvider:
    """Offline provider: returns pre-built resource dicts or a JSON file."""

    def __init__(self, resources: list[dict] | Path) -> None:
        self._source = resources

    def fetch(self) -> list[dict]:
        if isinstance(self._source, Path):
            return _load_export(self._source)
        return list(self._source)

    def describe(self) -> str:
        if isinstance(self._source, Path):
            return f"fixture: {self._source}"
        return f"fixture: {len(self._source)} resource(s)"


class ExportProvider:
    """Reads an explicit OPT-IN export file (browser/bookmarks/history).

    Applies a domain/category WHITELIST so only engineering resources are ever
    observed; personal, social, email, and unknown browsing is dropped. Never
    reads page contents, query strings, cookies, or form data.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def fetch(self) -> list[dict]:
        raw = _load_export(self.path)
        return [r for r in raw if _is_engineering(r)]

    def describe(self) -> str:
        return f"export: {self.path}"


# Substrings that mark NON-engineering browsing — always ignored by exports.
NON_ENGINEERING = (
    "mail.", "gmail", "outlook", "facebook", "twitter", "x.com", "instagram",
    "tiktok", "reddit.com", "netflix", "bank", "amazon.com", "shopping", "ebay",
)


def _is_engineering(resource: dict) -> bool:
    host = (resource.get("host") or urlparse(resource.get("url", "")).netloc).lower()
    cat = resource.get("category")
    # Explicit engineering category -> accept.
    if cat and cat in vars(Category).values() and cat != Category.UNKNOWN:
        return True
    # Explicit whitelist of known engineering hosts.
    for needle in HOST_CATEGORY:
        if needle and needle in host:
            return True
    # Explicitly non-engineering hosts -> reject.
    for needle in NON_ENGINEERING:
        if needle and needle in host:
            return False
    # Unknown host + no engineering category -> reject (whitelist, not scrape).
    return False


def _load_export(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError, TypeError):
        return []
    if isinstance(data, dict):
        # Common browser-export shape: {"urls": [...]} or {"resources": [...]}.
        for key in ("urls", "resources", "items"):
            if isinstance(data.get(key), list):
                return [d for d in data[key] if isinstance(d, dict)]
        return [data] if isinstance(data, dict) else []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def default_provider() -> ResearchProvider:
    export = _configured_export()
    if export:
        return ExportProvider(export)
    return FixtureProvider([])  # healthy: nothing configured to observe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_query_fragment(url: str) -> str:
    """Drop query string and fragment; keep only scheme://host/path.

    Query strings routinely carry tokens/secrets; they must never be stored.
    """
    try:
        p = urlparse(url or "")
    except (ValueError, TypeError):
        return ""
    cleaned = p._replace(query="", fragment="").geturl()
    return cleaned


def _coerce_duration(value) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# The observer
# ---------------------------------------------------------------------------


class ResearchObserver(Observer):
    name = "research"

    def __init__(self, provider: Optional[ResearchProvider] = None) -> None:
        # A provider is the ONLY input. Tests inject FixtureProvider.
        self.provider = provider or default_provider()
        self._at = _now()

    # --- Observer interface --------------------------------------------------

    def health(self, conn) -> ObserverHealth:
        resources = self._safe_fetch()
        method = self.provider.describe()
        if not resources:
            return ObserverHealth(
                True, Health.HEALTHY, method,
                "no research resources configured to observe.")
        return ObserverHealth(True, Health.HEALTHY, method,
                              f"observing {len(resources)} resource(s).")

    def collect(self, conn) -> list[Observation]:
        resources = [ResearchResource.from_dict(d) for d in self._safe_fetch()]
        observations: list[Observation] = []
        self._at = _now()
        best: Optional[str] = None
        for r in resources:
            if r.timestamp and (best is None or r.timestamp > best):
                best = r.timestamp
        if best:
            self._at = best
        for r in resources:
            observations.extend(self._resource_facts(r))
        observations.extend(self._signals(resources))
        observations.append(self._ws(len(resources)))
        return observations

    def summarize(self, conn) -> str:
        resources = [ResearchResource.from_dict(d) for d in self._safe_fetch()]
        by_cat: dict[str, int] = {}
        domains: dict[str, int] = {}
        for r in resources:
            by_cat[r.category] = by_cat.get(r.category, 0) + 1
            if r.host:
                domains[r.host] = domains.get(r.host, 0) + 1
        top = ", ".join(h for h, _ in sorted(domains.items(),
                                             key=lambda kv: kv[1], reverse=True)[:4])
        cat_lines = "\n".join(
            f"{label}\n{by_cat[c]}" for c, label in (
                (Category.DOCUMENTATION, "Documentation"),
                (Category.RFC, "RFCs"),
                (Category.RESEARCH_PAPER, "Research papers"),
                (Category.API_REFERENCE, "API references"),
                (Category.SPECIFICATION, "Specifications"),
                (Category.TUTORIAL, "Tutorials"),
                (Category.BLOG, "Blogs"),
                (Category.VIDEO, "Videos"),
            ) if c in by_cat)
        return (
            "Research Observer\n"
            "Healthy\n"
            f"Engineering resources\n{len(resources)}\n"
            f"{cat_lines}\n"
            f"Top domains\n{top or '(none)'}"
        )

    # --- internals ----------------------------------------------------------

    def _safe_fetch(self) -> list[dict]:
        try:
            return self.provider.fetch()
        except Exception:
            return []

    def _obs(self, subject, aspect, value, conf, cause=None) -> Observation:
        return Observation(
            source=self.name, subject=subject, aspect=aspect, value=str(value),
            confidence=conf, observed_at=self._at, scope="", cause=cause,
        )

    def _resource_facts(self, r: ResearchResource) -> list[Observation]:
        subj = r.url or r.host or "research"
        rows = [
            self._obs(subj, "host", r.host, Confidence.OBSERVED),
            self._obs(subj, "title", r.title, Confidence.OBSERVED),
            self._obs(subj, "category", r.category, Confidence.OBSERVED),
            self._obs(subj, "language", r.language or "", Confidence.OBSERVED),
            self._obs(subj, "visited_at", r.timestamp or "", Confidence.OBSERVED),
            self._obs(subj, "repeated_visits", r.repeated_visits,
                      Confidence.OBSERVED),
            self._obs(subj, "bookmarked", "true" if r.bookmarked else "false",
                      Confidence.OBSERVED),
        ]
        dur = _coerce_duration(r.duration_s)
        if dur is not None:
            rows.append(self._obs(subj, "visit_duration_s", f"{dur:.1f}",
                                  Confidence.OBSERVED))
        if r.read_completion is not None:
            rows.append(self._obs(subj, "read_completion",
                                  f"{float(r.read_completion):.2f}",
                                  Confidence.OBSERVED))
        return rows

    def _signals(self, resources: list[ResearchResource]) -> list[Observation]:
        topic_counts: dict[str, int] = {}
        for r in resources:
            topic = topic_of(r.host, r.category, r.language)
            if topic:
                topic_counts[topic] = topic_counts.get(topic, 0) + 1
        rows: list[Observation] = []
        for topic, count in sorted(topic_counts.items()):
            if count >= RESEARCH_TOPIC_THRESHOLD:
                label = ("repeated_" + topic if topic.endswith("_learning")
                         else "researching_" + topic)
                rows.append(self._obs(
                    "research", label, "true", Confidence.INFERRED,
                    cause=f"{count} engineering resource(s) about {topic} "
                          f"(>= {RESEARCH_TOPIC_THRESHOLD})."))
        return rows

    def _ws(self, n: int) -> Observation:
        return Observation(
            source=self.name, subject="research", aspect="resources",
            value=str(n), confidence=Confidence.OBSERVED, observed_at=self._at,
            scope="", cause=None,
        )
