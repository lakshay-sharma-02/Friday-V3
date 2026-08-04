---
name: proactive
description: Surface something worth the operator's attention without being asked. Use when you notice something during a task — a schedule conflict, a deadline approaching, a project state change, a risk — that the operator would want to know about NOW, outside the current exchange.
---

# Proactive

When you notice something worth surfacing, write a **notice file**:

`vault/notices/<unix-ts>-<slug>.md`

```
# Notice

- **at**: <ISO timestamp>
- **id**: <unix timestamp>

<one or two plain sentences — what, why it matters>
```

## Rules

- **Only when it matters.** Notices interrupt — the HUD shows them and
  the notifier speaks them. Reserve them for real value: schedule
  conflicts, missed deadlines, broken builds, security risks.
- **One notice per event.** Don't pile on; the operator reads one.
- **Keep it short.** One or two sentences. The HUD shows the file name
  and the first lines.
- **Never spam.** If you already wrote the same notice, don't repeat.
- Still answer the operator's actual request in your reply; the notice
  is extra, not a replacement.

## Related

- `vault/notices/` — the notice dir (read before writing, never edit others)
- `vault/raw/` — turn log (do not edit)
