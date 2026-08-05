---
name: remember
description: Capture a fact about the operator or their world into the wiki. Use when the operator states a preference, a fact about themselves, a person, a project, or anything they want remembered.
---

# Remember

Capture durable facts as wiki notes. These are the notes the engine greps when answering later.

## Files

- `~/.friday/v6_vault/wiki/me.md` — about the operator: preferences, habits, constraints, identity.
- `~/.friday/v6_vault/wiki/people.md` — people: roles, relationships, key facts (one `## Name` section each).
- `~/.friday/v6_vault/wiki/projects.md` — projects: purpose, stack, links to notes.

## Rules

- **Distill, don't transcribe.** Turn "I prefer dark mode and I hate meetings on Fridays" into clean bullets, not a quote log.
- **One fact, findable.** Put each durable fact where a future question would look.
- **Link related** notes (`[[me]]`, `[[projects]]`, `[[people]]`) so the graph connects.
- Keep `me.md` as bullets; keep `people.md` as per-person sections.
- Confirm in your reply what you remembered.

## Related

- `~/.friday/v6_vault/raw/` — the verbatim turn log (do not edit)
