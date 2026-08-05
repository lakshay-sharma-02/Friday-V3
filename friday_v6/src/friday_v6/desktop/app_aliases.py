"""Persistent app-alias learning — "open my todo app" remembers the binary.

The NL desktop layer (``desktop_text_command``, Wave 20) resolves natural
names through a fixed alias map + ``PATH``. This module adds the *learning
loop* on top: when Friday doesn't know an app, the operator teaches it
once — "my todo app is obsidian", "use obsidian for my todo app", "open
my todo app with obsidian" — and the mapping is persisted to
``~/.friday/v4_desktop_aliases.json``. From then on "open my todo app"
resolves to obsidian, always, on every surface (voice, CLI, web, phone).

Design rules:
- Pure stdlib, JSON file in ``~/.friday`` (the V3/V4 convention).
- Atomic writes (temp file + rename) — a crash never corrupts the store.
- Only *resolvable* binaries are learned: ``learn_alias`` validates the
  binary via ``shutil.which`` (or an existing absolute path), so Friday
  never remembers an app that isn't installed.
- Names are normalized ("my todo app" → "todo app") so the teaching
  phrase and the later "open …" phrase agree on the key.
- Never crashes: a missing/corrupt store reads as empty.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

#: Default store location — follows the ``~/.friday`` data convention
#: (same dir as ``v4_config.json`` and ``v4_mobile_pair.json``).
_DEFAULT_ALIAS_FILE = Path.home() / ".friday" / "v4_desktop_aliases.json"

#: The collab-observation source that carries aliases across machines —
#: "my todo app is obsidian" taught on the laptop lands on the desktop
#: through the collab CRDT (source/subject/aspect LWW), so "open my todo
#: app" works on every instance (one presence).
ALIAS_OBSERVATION_SOURCE = "v4.app_aliases"

#: Fillers stripped from a natural app name before storing/looking up.
_NAME_FILLERS = ("my ", "the ", "a ", "an ")

#: Pronoun/filler names that are never worth learning — "use sudo for
#: that" must not store a junk alias for "that" (the frame regexes are
#: loose enough to catch such config-ish phrasings).
_PRONOMINAL_NAMES = {
    "that", "this", "it", "them", "those", "these", "stuff",
    "things", "something", "the rest", "more", "everything", "all",
}

#: Words that mark a name as an *app* ("my todo app", "notes app") — used
#: to tell "open my todo app" (learn me) from "open c++ compiler of
#: programiz" (web-search me) and "my code is broken" (not learning).
_APP_SUFFIXES = (
    "app", "apps", "application", "program", "tool", "editor",
    "software", "launcher", "client", "player",
)

#: Learning frames → (regex with named groups name/binary, name_app_like).
#: ``name_app_like=True`` requires the name to *sound like an app* so
#: "my code is broken" never trips the "X is Y" frame.
_LEARNING_FRAMES: tuple[tuple[str, bool], ...] = (
    # "use obsidian for my todo app" / "use obsidian for todo app"
    (r"^use\s+(?P<binary>\S+)\s+for\s+(?:my\s+|the\s+)?(?P<name>.+)$",
     False),
    # "set my todo app to obsidian" / "set todo app to obsidian"
    (r"^set\s+(?:my\s+|the\s+)?(?P<name>.+?)\s+to\s+(?P<binary>\S+)$",
     False),
    # "open my todo app with obsidian" — a command that also teaches
    (r"^open\s+(?:my\s+|the\s+)?(?P<name>.+?)\s+with\s+(?P<binary>\S+)$",
     False),
    # "my todo app is obsidian" / "todo app is obsidian"
    (r"^(?:my\s+|the\s+)?(?P<name>.+?)\s+(?:is|as)\s+"
     r"(?P<binary>\S+)$", True),
)


def store_path(path: Optional[os.PathLike | str] = None) -> Path:
    """Resolve the alias store path (default ``~/.friday/…``)."""
    return Path(path) if path else _DEFAULT_ALIAS_FILE


def _read(path: Optional[os.PathLike | str] = None) -> dict[str, str]:
    """Read the store; a missing/corrupt file reads as empty (never crash)."""
    file = store_path(path)
    try:
        if not file.exists():
            return {}
        data = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {str(k).strip().lower(): str(v) for k, v in data.items()
                if str(k).strip()}
    except Exception:
        return {}


def _write(data: dict[str, str],
           path: Optional[os.PathLike | str] = None) -> None:
    """Write the store atomically (temp file + rename)."""
    file = store_path(path)
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        tmp = file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(file)
    except Exception:
        # Never crash a voice utterance because the store couldn't write.
        return


def normalize_name(target: str) -> str:
    """Normalize a natural name to the stored key.

    "my todo app" → "todo app"; "My Todo APP" → "todo app"; "the notes"
    → "notes". Lowercase, filler-stripped, whitespace-collapsed.
    """
    low = re.sub(r"\s+", " ", (target or "").strip().lower())
    for filler in _NAME_FILLERS:
        if low.startswith(filler):
            low = low[len(filler):].strip()
    return low


def is_app_like(name: str) -> bool:
    """Whether a name sounds like an app ("todo app", "notes tool")."""
    low = normalize_name(name)
    if not low or low in ("app", "application", "program", "tool",
                          "editor"):
        return False
    return any(low.endswith(suffix) for suffix in _APP_SUFFIXES)


def _valid_binary(binary: str) -> bool:
    """A plausible binary token: no spaces, has a letter, not numeric."""
    b = binary.strip()
    if not b or re.search(r"\s", b):
        return False
    if not re.search(r"[A-Za-z]", b):
        return False
    return True


def resolve_binary(binary: str) -> Optional[str]:
    """The resolvable binary for a taught command, or None.

    Absolute paths must exist; bare names must resolve on ``PATH``. Only
    resolvable binaries are ever learned (honesty law). Never crashes:
    ``shutil.which`` can raise on a pathological ``PATH``.
    """
    b = binary.strip()
    if not _valid_binary(b):
        return None
    try:
        if os.path.isabs(b):
            return b if os.path.exists(b) else None
        return shutil.which(b)
    except Exception:
        return None


def parse_learning_phrase(text: str) -> Optional[tuple[str, str]]:
    """Extract (name, binary) from a teaching utterance, or None.

    Understands the frames in ``_LEARNING_FRAMES``; the "is/as" frame
    additionally requires an app-like name so "my code is broken" is NOT
    a learning phrase. The binary is not validated here — the caller
    decides whether to save it.
    """
    low = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not low:
        return None
    for pattern, need_app_like in _LEARNING_FRAMES:
        m = re.match(pattern, low)
        if not m:
            continue
        name = normalize_name(m.group("name"))
        binary = m.group("binary").strip()
        if not name or not _valid_binary(binary):
            continue
        if name in _PRONOMINAL_NAMES:
            continue  # "use sudo for that" → not a learnable app
        if need_app_like and not is_app_like(name):
            continue
        return name, binary
    return None


def is_learning_phrase(text: str) -> bool:
    """Whether an utterance is a teaching phrase (for intent routing)."""
    return parse_learning_phrase(text) is not None


def learned_aliases(path: Optional[os.PathLike | str] = None) -> dict[str, str]:
    """All learned (name → binary) mappings."""
    return _read(path)


def resolve_learned(target: str,
                    path: Optional[os.PathLike | str] = None) -> Optional[str]:
    """The binary Friday learned for a natural name, or None."""
    return _read(path).get(normalize_name(target))


def learn_alias(name: str, binary: str,
                path: Optional[os.PathLike | str] = None) -> Optional[str]:
    """Persist a taught mapping; returns the resolved binary or None.

    Only learns when the binary actually resolves (``resolve_binary``) —
    Friday never remembers an app that isn't installed. Returns None and
    saves nothing when the binary can't be resolved.
    """
    resolved = resolve_binary(binary)
    if not resolved:
        return None
    data = _read(path)
    data[normalize_name(name)] = resolved
    _write(data, path)
    return resolved


def forget_alias(name: str,
                 path: Optional[os.PathLike | str] = None) -> bool:
    """Remove a learned mapping; True when something was removed."""
    data = _read(path)
    key = normalize_name(name)
    if key in data:
        del data[key]
        _write(data, path)
        return True
    return False


def aliases_as_observations(
        path: Optional[os.PathLike | str] = None) -> list[dict]:
    """Local aliases as collab-observation payloads (for the bus).

    Each alias becomes one observation keyed ``alias:<name>`` so the
    CRDT's per-source:subject:aspect LWW resolves concurrent edits across
    machines (whoever taught last wins).
    """
    out = []
    for name, binary in sorted(_read(path).items()):
        out.append({
            "source": ALIAS_OBSERVATION_SOURCE,
            "subject": name,
            "aspect": "binary",
            "kind": "alias",
            "payload": {"binary": binary},
        })
    return out


def apply_collab_observations(
        observations: list[dict],
        path: Optional[os.PathLike | str] = None) -> int:
    """Merge peer aliases from the collab bus into the local store.

    Trusts the peer's mapping (it was taught on another machine Friday
    trusts via the workspace ACL) but never crashes on a malformed
    entry. Binaries are NOT validated here — ``_resolve_app`` gates
    every launch on local existence, so a peer alias for an app not
    installed on this machine is stored but never launched. Returns
    the number of aliases applied/refreshed.
    """
    data = _read(path)
    applied = 0
    for obs in observations or []:
        try:
            if not isinstance(obs, dict):
                continue
            if obs.get("source") != ALIAS_OBSERVATION_SOURCE:
                continue
            name = normalize_name(str(obs.get("subject") or ""))
            payload = obs.get("payload") or {}
            binary = str(payload.get("binary") or "").strip()
            if not name or not binary:
                continue
            if data.get(name) != binary:
                data[name] = binary
                applied += 1
        except Exception:
            continue
    if applied:
        _write(data, path)
    return applied
