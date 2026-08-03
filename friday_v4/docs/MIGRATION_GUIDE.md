# Friday V4 — Migration Guide (V3 → V4)

> **Wave 12 deliverable.** V4 is the product; V3 is legacy heritage. This
> guide moves an existing V3 workspace to V4 with zero data loss and zero
> risk — V4 never writes V3's DB, so nothing is destroyed.

---

## 1. The mental model

| | V3 (`friday`) | V4 (`friday4`) |
|---|---|---|
| CLI | `friday …` | `friday4 …` |
| State DB | `~/.friday/friday.db` | `~/.friday/v4.db` |
| Daemon | `friday daemon` | `friday4 daemon` |
| Relationship | the product (frozen) | **read-only heritage data** |

V4 runs fully standalone. V3's DB is optional legacy *data*: when present,
V4's `V3DataSource` (`proactive/v3source.py`) reads observations, actions,
and ambient events **read-only** (`mode=ro`) and surfaces them in the web
dashboard, anticipation, and briefings. Missing V3 → V4 works anyway.

## 2. Before you start

```bash
# Back up V3 state (cheap insurance; V4 never touches it, but backup anyway)
cp -r ~/.friday ~/.friday.bak-$(date +%F)
```

V3 and V4 can coexist: both read `~/.friday`, neither writes the other's
DB. There is no uninstall step for V3 required.

## 3. Install V4

```bash
git clone <your-v4-repo> friday_v4 && cd friday_v4
./install.sh --full        # venv + pip install -e + doctor
```

Or by hand: `pip install -e .` (Python ≥ 3.12, pure-stdlib core).

## 4. Verify

```bash
friday4 status      # all 10 subsystems, graceful ◐/✘ when absent
friday4 doctor      # tool availability + last scan state
friday4 web         # dashboard — the V3 bridge card shows live V3 data
```

## 5. First-day migration checklist

| V3 habit | V4 replacement |
|----------|----------------|
| `friday talk` | `friday4 talk "…"` (NL brain: gate → sandbox → audit) |
| `friday ask …` | `friday4 ask "…"` (evidence-cited, no fabrication) |
| `friday daemon start` | `friday4 daemon start` (one process: observer, notifier, sampler, security, memory, skills, ambient push) |
| `friday security scan` | `friday4 security scan [path] [--threshold high] [--json]` |
| V3 ambient feed (written) | **read-only** via the V3 bridge; V4 events push through `ambient/` (Wave 11) |
| `friday memory …` | `friday4 memory store/recall/forget/list/status` |
| V3 missions/executors | `friday4 talk "ship the auth refactor"`, `friday4 execute <type> …` |
| V3 CLI-only everything | Every capability has an NL path (Law 1) + CLI debug hatch |

## 6. What V4 does NOT do

- ✗ Never imports `from friday import …` (single exception: read-only
  sqlite access in `v3source.py`).
- ✗ Never shells into the V3 CLI.
- ✗ Never writes `~/.friday/friday.db`.
- ✗ Does not gate on V3's tests or laws.

## 7. Going back

You can't "lose" V3 by migrating — V4 never writes V3 data. If you want to
keep using V3 for a while, run both daemons side by side (they share only
reads). When you're ready, retire V3 with `rm -rf <v3 install>` — V4 keeps
working, and its ambient feed simply shows fewer legacy events.

## 8. New V4 commands you'll reach for

```bash
friday4 talk "git status"                  # say it, Friday does it
friday4 ask "who am I"                     # persona + memory, cited
friday4 research analyze ~/Projects/x      # Wave 11 architecture analysis
friday4 research correlate a b             # cross-project integration cost
friday4 research briefing morning|evening  # real-state briefing
friday4 research report --daily|--weekly   # deterministic cited reports
friday4 execute ssh host "df -h"           # Wave 12 remote executor
friday4 daemon start                       # ambient presence + push
friday4 web                                # live dashboard
```
