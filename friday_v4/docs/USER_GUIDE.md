# Friday V4 — User Guide (Use the Real Product)

> Everything below was verified live on 2026-08-01. If a command behaves
> differently, file it — that's a bug.

State lives at `~/.friday/v4.db` (schema v3, 9 tables). V3 data
(`~/.friday/friday.db`) is read through the read-only bridge when present
and never written.

---

## 1. One-line health check

```bash
friday4 status
```

Shows all 10 subsystems: daemon, voice, desktop, security, proactive,
intelligence, web, collab, db, v3 bridge. Everything degrades gracefully —
a missing subsystem prints `◐`/`✘`, never crashes.

## 2. Talk to Friday (the brain — Wave 9)

**One-shot:**
```bash
friday4 talk "git status"                # executes, audited, exit 0
friday4 talk "run the tests"             # pytest via gate → sandbox → audit
friday4 talk "ship the auth refactor"    # creates a mission (planner)
friday4 talk "hello"                     # greeting
friday4 talk --json "git log -1"         # machine-readable output
```

**Interactive REPL:**
```bash
friday4 talk                            # no text → conversational session
```

**Manual mission steps** (Friday never invents a result):
```bash
friday4 talk --manual <mission_id> "done"
```

Exit codes: `0` ok / `1` failed / `2` denied / `3` usage. Everything is
recorded verbatim into the conversation log — the brain learns from what
you actually said.

## 3. Ask Friday (evidence-cited answers — Wave 9 reasoning)

```bash
friday4 ask "what's the status of my projects"   # missions + recent actions
friday4 ask "what did I do recently"             # audit trail + V3 observations
friday4 ask "who are you"                        # Friday's self-declaration
friday4 ask "who am I"                           # your own words, quoted back
friday4 ask "how's the auth refactor going"      # mission progress
friday4 ask "what do you know about Rust"        # stored memory facts
```

**No answer without evidence** — empty evidence → honest "I don't know
yet". Every answer cites its source (`v4.actions`, `v4.exchanges`, …).

## 4. Memory & identity (Wave 10)

```bash
friday4 persona remember "call me Lakshay"       # explicit-consent learning
friday4 persona profile                          # what Friday remembers about you
friday4 memory store operator.prefers "Rust for performance-critical code"
friday4 memory list / recall <key> / forget <key>
friday4 relationship status                      # depth → tone → verbosity
friday4 relationship refresh
friday4 skills list                              # shadow → verified → promoted
friday4 skills learn                             # form a skill from your patterns
friday4 skills promote <skill>                   # explicit approval to promote
```

`friday4 ask "who am I"` quotes your stored facts back with provenance.

## 5. Voice

```bash
friday4 voice talk          # interactive voice session (hotword + push-to-talk)
friday4 voice setup         # audio wizard
friday4 voice status        # provider + hardware diagnostics
friday4 voice test          # speak a phrase aloud
```

Spoken "run the tests" flows through the same brain as typed — voice
confirmations are asked aloud (TTS → STT) and fail closed on silence.

## 6. Ambient presence

```bash
friday4 daemon start        # observer + notifier + sampler + security + memory
                            #   sweep + skill learner + relationship refresh
friday4 daemon status
friday4 web                 # http://127.0.0.1:8899/ — live dashboard
```

Dashboard APIs (all read-only except `/api/talk` + `/api/scan`):
`/api/overview`, `/api/security`, `/api/ambient`, `/api/projects`,
`/api/memory`, `/api/relationship`, `/api/skills`, `/api/talk`.

## 7. Security & ops

```bash
friday4 security scan [path] [--threshold high] [--json]
friday4 security status        # tool availability
friday4 doctor                 # one-command subsystem diagnostics
friday4 status db              # DB schema + row counts
friday4 execute --list         # available executors
```

## 8. Collaboration (multi-instance)

```bash
friday4 collab start           # UDP beacon + TCP sync (pure stdlib)
friday4 collab status / peers / obs / share / perms
```

## 9. Desktop control

```bash
friday4 desktop status / windows / switch / focus / launch / screenshot
friday4 desktop platforms
```

## Suggested first hour

```bash
friday4 status                                  # see the whole system
friday4 persona remember "call me <your name>"  # teach identity
friday4 memory store operator.prefers "..."     # store a fact
friday4 ask "who am I"                          # Friday knows you now
friday4 talk "git status"                       # say it, Friday does it
friday4 daemon start                            # ambient presence
friday4 web                                     # dashboard
```
