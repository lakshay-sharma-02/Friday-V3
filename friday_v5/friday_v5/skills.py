"""Skills — the routing table, as plain markdown.

Each skill is ``<dir>/.claude/skills/<name>/SKILL.md`` with YAML
frontmatter (``name`` + ``description``). Claude's tool-calling does
the routing: it reads a skill's md when the description matches the
moment. This module only enumerates them so the engine can (a) tell
Claude they exist and (b) verify a load works. No keyword classifier —
the model decides.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

SKILL_FILENAME = "SKILL.md"
_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_YAML = re.compile(r"^\s*(\w+)\s*:\s*(.+)\s*$", re.MULTILINE)


class Skill:
    __slots__ = ("name", "description", "path", "body")

    def __init__(self, name: str, description: str, path: Path,
                 body: str) -> None:
        self.name = name
        self.description = description
        self.path = path
        self.body = body

    def render(self) -> str:
        """The full md a skill consumes — for embedding in a prompt."""
        return f"## Skill: {self.name}\n{self.description}\n\n{self.body}"


def _parse(path: Path) -> Optional[Skill]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FRONT.match(text)
    if not m:
        return None
    front = dict(_YAML.findall(m.group(1)))
    name = (front.get("name") or path.parent.name).strip()
    description = (front.get("description") or "").strip()
    body = text[m.end():].strip()
    if not description:
        return None
    return Skill(name, description, path, body)


def find_skill_dir(cwd: Optional[Path | str] = None) -> Path:
    """The ``.claude/skills`` directory — nearest up from cwd, else the
    default project location."""
    start = Path(cwd) if cwd else Path.cwd()
    for parent in [start] + list(start.parents):
        candidate = parent / ".claude" / "skills"
        if candidate.is_dir():
            return candidate
    return Path.home() / ".friday" / "v5" / "skills"


def load_skills(cwd: Optional[Path | str] = None) -> list[Skill]:
    """All valid skills in the nearest skills dir, alphabetical."""
    base = find_skill_dir(cwd)
    out: list[Skill] = []
    for f in sorted(base.glob(f"*/{SKILL_FILENAME}")):
        skill = _parse(f)
        if skill is not None:
            out.append(skill)
    return out


def render_all(cwd: Optional[Path | str] = None) -> str:
    """One block naming every skill + description — the routing table
    the engine embeds so Claude knows what it can do."""
    skills = load_skills(cwd)
    if not skills:
        return "(no skills found)"
    return "\n\n".join(
        f"- {s.name}: {s.description}" for s in skills)
