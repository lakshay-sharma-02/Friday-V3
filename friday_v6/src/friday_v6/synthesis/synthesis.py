"""Deterministic evidence-cited synthesis (Wave 11 §3.2).

``synthesize`` composes a list of cited findings into a structured
report. It never invents a paragraph — every section is exactly the
findings given (or the honest "nothing yet" when empty). Deterministic:
same findings in, same report out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("friday_v6.synthesis.synthesis")


@dataclass
class SynthesisReport:
    """A deterministic, evidence-cited report."""

    title: str
    sections: dict[str, list[str]] = field(default_factory=dict)
    generated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "generated_at": self.generated_at,
            "sections": self.sections,
        }

    def render(self) -> str:
        """Render as text (for CLI/voice — never invents content)."""
        lines = [f"# {self.title}", ""]
        for section, findings in self.sections.items():
            lines.append(f"## {section}")
            if findings:
                lines.extend(f"- {f}" for f in findings)
            else:
                lines.append("- nothing yet")
            lines.append("")
        return "\n".join(lines).strip()


def synthesize(title: str, sections: dict[str, list[str]],
               generated_at: str = "") -> SynthesisReport:
    """Compose cited findings into a :class:`SynthesisReport`.

    Args:
        title: Report title.
        sections: {section heading: [cited findings]}. Findings are
            evidence lines — the caller supplies them; this layer never
            invents any.
        generated_at: ISO timestamp for the report (defaults to "now").

    Returns:
        A deterministic report. Empty sections render as "nothing yet".
    """
    from .. import db
    when = generated_at or db.now_iso()
    clean = {k: [str(f) for f in v] for k, v in sections.items()}
    return SynthesisReport(title=title, sections=clean,
                           generated_at=when)


__all__ = ["SynthesisReport", "synthesize"]
