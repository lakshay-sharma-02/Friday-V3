"""Markdown skills — V5's SKILL.md port (Wave 2).

V5 proved that skills are *just files*: ``.claude/skills/<name>/SKILL.md``
with frontmatter (``name`` + ``description`` — what triggers the load)
and a markdown how-to body. V5's routing *was* Claude reading the
description, pulling in the md, and following it.

V6 keeps the files and the Claude path (the ``CLAUDE:`` bridge embeds a
matched skill's body when routing), but — never-crash law — adds a
**deterministic floor**: :class:`MarkdownSkillLibrary` discovers and
parses the same SKILL.md files with pure stdlib, so the brain can
always name, list, and match skills without the network or the SDK.

Layout:

    markdown_skills/<name>/SKILL.md   ← bundled skills (package data)
    ~/.friday/v6_skills/<name>/SKILL.md   ← operator-authored (wins on
                                            name collision)

Matching (the floor):

- :meth:`MarkdownSkillLibrary.explicit_name` — ``"use the schedule
  skill"`` / ``"run your research skill"`` — a known skill name plus
  the word "skill" is an *unambiguous* invocation (never hijacks plain
  work: "run the tests" has no skill name in it).
- :meth:`MarkdownSkillLibrary.match` — description-token scoring for
  softer triggers (``"add standup to my agenda"`` → schedule).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v6.skills.markdown")

#: Default operator skills dir (overrides bundled skills by name).
#: ``FRIDAY_V6_SKILLS_DIR`` lets tests (and operators) point the
#: operator dir elsewhere without touching the CLI. Read lazily in
#: ``MarkdownSkillLibrary.__init__`` — module-level evaluation would
#: freeze the env at import time and break hermetic tests.
DEFAULT_USER_SKILLS_DIR = Path.home() / ".friday" / "v6_skills"

#: Tokens ignored in description matching (the floor must be precise,
#: not noisy — a wrong skill match is worse than no match).
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with",
    "is", "are", "be", "it", "this", "that", "you", "your", "when", "asked",
    "use", "uses", "using", "what", "how", "do", "does", "done", "not",
    "but", "if", "as", "at", "by", "from", "into", "than", "then", "will",
    "would", "can", "could", "should", "may", "might", "please", "me", "my",
    "want", "need", "get", "make", "give", "show", "tell", "say", "like",
    "about", "out", "up", "down", "over", "under", "also", "very", "just",
})

#: Words that mark an explicit skill invocation ("use the X skill").
_SKILL_WORD = ("skill", "skills")


@dataclass
class MarkdownSkill:
    """One SKILL.md file: frontmatter + body (the deterministic view)."""

    name: str
    description: str
    body: str                       # markdown body (after frontmatter)
    path: Path
    source: str = "bundled"         # "bundled" | "user"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "path": str(self.path),
        }


def _parse_skill_file(path: Path, source: str) -> Optional[MarkdownSkill]:
    """Parse one SKILL.md into a :class:`MarkdownSkill` (never raises).

    Frontmatter is the V5 contract: a leading ``---`` block with
    ``name:`` and ``description:`` lines, then the markdown body. Pure
    stdlib parse — no YAML dependency. A missing/unreadable/malformed
    file returns None (the never-crash law): discovery skips it.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.debug(f"skill unreadable {path}: {exc}")
        return None
    text = text.lstrip("\ufeff \t\n")
    if not text.startswith("---"):
        logger.debug(f"skill {path}: no frontmatter, skipped")
        return None
    end = text.find("\n---", 3)
    if end < 0:
        logger.debug(f"skill {path}: unterminated frontmatter, skipped")
        return None
    fm = text[3:end].strip()
    body = text[end + 4:].strip()
    meta: dict[str, str] = {}
    for line in fm.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    name = meta.get("name", "").strip()
    description = meta.get("description", "").strip()
    if not name or not description:
        logger.debug(f"skill {path}: missing name/description, skipped")
        return None
    return MarkdownSkill(name=name, description=description, body=body,
                         path=path, source=source)


def _tokens(text: str) -> set[str]:
    """Significant lowercase tokens from text (floor matching vocabulary)."""
    words = re.split(r"\W+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


class MarkdownSkillLibrary:
    """Discover + match SKILL.md files (bundled + operator-authored).

    Pure stdlib, never crashes, hermetic: the bundled dir is resolved
    from this module's package location (no import-time I/O beyond
    ``Path``), and tests inject tmp dirs for the user skills.
    """

    def __init__(self, bundled_dir: Optional[Path] = None,
                 user_dir: Optional[Path] = None) -> None:
        self.bundled_dir = Path(bundled_dir) if bundled_dir else \
            Path(__file__).parent / "markdown_skills"
        self.user_dir = Path(user_dir) if user_dir else \
            Path(os.environ.get("FRIDAY_V6_SKILLS_DIR",
                                str(DEFAULT_USER_SKILLS_DIR)))
        #: (signature, skills) cache — skills are static files; discovery
        #: re-parses only when a SKILL.md's (mtime, size) changed. The NL
        #: router constructs a library per utterance, so this keeps the
        #: hot path to stat calls, never full file reads.
        self._cache: Optional[tuple[tuple, dict[str, MarkdownSkill]]] = None

    # ── discovery ─────────────────────────────────────────────────────

    def _signature(self) -> tuple:
        """A cheap fingerprint of every SKILL.md (mtime, size) — no reads."""
        sig: list = []
        for base in (self.bundled_dir, self.user_dir):
            if not base.is_dir():
                sig.append((str(base), -1, -1))
                continue
            try:
                for child in sorted(base.iterdir()):
                    if not child.is_dir():
                        continue
                    skill_file = child / "SKILL.md"
                    if not skill_file.is_file():
                        continue
                    try:
                        st = skill_file.stat()
                        sig.append((str(skill_file), st.st_mtime_ns,
                                    st.st_size))
                    except Exception as exc:  # defensive
                        logger.debug(f"skill stat failed: {exc}")
            except Exception as exc:  # defensive — never crash
                logger.debug(f"skill scan failed in {base}: {exc}")
        return tuple(sig)

    def discover(self) -> dict[str, MarkdownSkill]:
        """All skills by name; operator-authored wins on collision.

        Scans ``<dir>/<name>/SKILL.md`` under the bundled dir and the
        user dir. Cached: re-parses only when a SKILL.md's (mtime,
        size) changes, so per-utterance routing stays cheap. Never
        raises — unreadable/malformed files are skipped and an empty
        library is a valid library.
        """
        sig = self._signature()
        if self._cache is not None and self._cache[0] == sig:
            return self._cache[1]
        found: dict[str, MarkdownSkill] = {}
        for base, source in ((self.bundled_dir, "bundled"),
                             (self.user_dir, "user")):
            if not base.is_dir():
                continue
            try:
                for child in sorted(base.iterdir()):
                    if not child.is_dir():
                        continue
                    skill = _parse_skill_file(child / "SKILL.md", source)
                    if skill is not None:
                        found[skill.name] = skill
            except Exception as exc:  # defensive — never crash
                logger.debug(f"skill scan failed in {base}: {exc}")
        self._cache = (sig, found)
        return found

    def list(self) -> list[MarkdownSkill]:
        """All skills, sorted by name."""
        return sorted(self.discover().values(), key=lambda s: s.name)

    def get(self, name: str) -> Optional[MarkdownSkill]:
        return self.discover().get((name or "").strip())

    # ── matching (the deterministic floor) ────────────────────────────

    def explicit_name(self, text: str) -> Optional[MarkdownSkill]:
        """A known skill named *adjacently* to "skill" in the utterance.

        ``"use the schedule skill"`` / ``"run your research skill"``
        — the pattern ``<name> skill(s)`` is an *unambiguous*
        invocation. Adjacency (not mere co-occurrence) keeps this tight:
        "please remember the deploy skill" names an unknown skill (no
        bundled ``deploy``), and "how do I execute the skills checklist"
        is not an execute-skill invocation. Never hijacks plain work:
        "run the tests" contains no skill name at all.
        """
        lower = (text or "").lower()
        for skill in self.list():
            name = skill.name.lower()
            if any(f"{name} {w}" in lower for w in _SKILL_WORD):
                return skill
        return None

    def match(self, text: str, min_tokens: int = 2) -> Optional[MarkdownSkill]:
        """Best skill by description-token overlap, or None.

        The floor for softer triggers ("add standup to my agenda" →
        schedule). Requires at least ``min_tokens`` shared significant
        tokens so a single coincidental word never forces a match; a
        tie keeps the alphabetically-first skill (stable, testable).
        """
        utterance = _tokens(text)
        if not utterance:
            return None
        best: Optional[MarkdownSkill] = None
        best_score = 0
        for skill in self.list():
            haystack = _tokens(f"{skill.name} {skill.description}")
            score = len(utterance & haystack)
            if score > best_score:
                best, best_score = skill, score
        if best is None or best_score < min_tokens:
            return None
        return best


__all__ = ["DEFAULT_USER_SKILLS_DIR", "MarkdownSkill",
           "MarkdownSkillLibrary", "_parse_skill_file"]
