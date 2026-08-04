---
name: schedule
description: Manage the operator's agenda. Use when asked to add, view, list, move, or clear schedule items, appointments, meetings, deadlines, or reminders.
---

# Schedule

The agenda lives at `vault/wiki/schedule.md` (create it on first use).

## Format

Each item is a bullet line:

```
- 2026-08-05 09:00 — Standup (15 min)
```

Convention: `YYYY-MM-DD HH:MM — Title (duration)`.

## Rules

- **Read first.** If `vault/wiki/schedule.md` exists, read it before editing so you append, not overwrite.
- **Append**, don't rewrite — unless the operator asks to clear/remove.
- **Confirm what you added** in your reply: the date/time and the item.
- If the operator gives a relative time ("tomorrow", "next monday"), resolve it to an absolute `YYYY-MM-DD` in the file.
- Link the file from related notes with `[[schedule]]`.

## Related

- `vault/wiki/me.md` — operator preferences
- `vault/raw/` — turn log (do not edit)
