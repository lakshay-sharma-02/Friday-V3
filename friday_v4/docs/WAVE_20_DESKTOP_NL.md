# Wave 20 — Desktop Natural Language ✅ SHIPPED (2026-08)

> **The directive:** the desktop is a surface, not a script. Friday
> should understand *"open chrome on workspace 3 and open whatsapp"*,
> *"open youtube and cristiano ronaldo channel in it"*, *"open c++
> compiler of programiz on it"* — compound, qualified, natural — and
> hand everything it can't do to the agentic arms instead of pretending
> with a hardcoded workflow.
>
> Governance: [THE MCU FRIDAY STANDARD](MCU_FRIDAY_STANDARD.md) — the
> ten laws, the five acceptance tests.

---

## Why this wave

Waves 1–19 built the surfaces (voice, desktop, mobile, web), the safe
core, and the brain. But desktop control was **fragmented and shallow**:

- `desktop_text_command` — the shared handler behind `friday4 talk`,
  the web dashboard, and (Wave 19) the phone Chat tab — spoke only
  *single, simple* commands: one verb + one target.
- The voice router ran its **own, separate legacy parser** (`_DESKTOP_ACTIONS`,
  `_handle_open`…) — so voice couldn't do the compound things text could,
  and the two parsers drifted.
- Anything the desktop layer didn't recognize got **web-searched**, even
  when it was clearly *work*: "open a python venv and install requests"
  searched Google instead of executing.

The user's test suite for "handle everything":

> "if i say open chrome on workspace 3 it should be able to do that
> right? if i say open whatsapp it should be able to open brave and open
> web.whatsapp.com in it right? if i say open youtube and cristiano
> ronaldo channel in it it should be able to do it right? if i say open
> c++ compiler of programiz on it it should be able to do it right?"

And the follow-up that defines the ceiling:

> "bro not just my examples it should be able to handle everything like
> these like legit everything like Tony Stark's Friday we don't need
> hardcoded workflows"

**The answer is not a bigger catalog.** The answer is a fast
deterministic interpreter for the *natural* cases, and a clean
**fall-through to the agentic brain** (mission planner / Claude Code
executor) for everything else.

---

## What shipped

### 1. A real NL desktop interpreter (`desktop/wm_abstraction.py`)

`desktop_text_command` is now a compound, qualified NL interpreter —
one desktop language for every surface:

| Capability | Example | Behavior |
|-----------|---------|----------|
| **Compound commands** | `open chrome on workspace 3 and open whatsapp` | split into two commands, executed in order, replies joined |
| **Workspace targeting** | `open chrome on workspace 3` | `switch_workspace(3)` before opening |
| **Browser qualifier** | `open youtube in firefox` | opens the URL in Firefox |
| **"in it" chaining** | `open youtube and cristiano ronaldo channel in it` | the previous command's browser is remembered in command context |
| **Web destinations** | `open whatsapp` → `web.whatsapp.com` | known-site map with canonical display names ("WhatsApp", "YouTube") |
| **Site search** | `open youtube and cristiano ronaldo channel` | `youtube.com/results?search_query=…` |
| **Explicit web search** | `search for X` / `look up X` / `google X` / `find X` | real web search, always |
| **Noun-phrase search** | `open c++ compiler of programiz on it` | web search fallback for a query, not an app |
| **Honest app launch** | `open brave` / `launch spotify` | launch is **gated on a resolvable binary** — never claims "Launching X" when X isn't installed (adapters' `sh -c` can't tell) |
| **Task fall-through** | `open a python venv and install requests` | returns `""` → the utterance **falls through to the brain** (EXECUTE via Claude Code / PLAN mission) |
| **Explicit URL** | `open https://…` | opens the URL |
| **Read queries** | `what's on my screen` | desktop status text |

Splitting rules matter: `_split_desktop_commands` only splits on
connectors when the *next token is a desktop verb*, so
"open youtube and cristiano ronaldo channel" stays ONE command (the
"and" joins the search phrase) while "open chrome and open whatsapp"
splits. Multi-word connectors (`and then`, `and also`) are treated as
units. Task phrases ("clone the repo and open it in my editor") are
never split — they fall through whole.

`DesktopAbstraction.open_url(url, browser=)` was added to the base
interface (adapters may override, e.g. macOS `open -a`); `_open_in_browser`
prefers the WM hook and falls back to launching a browser binary.

### 2. The classifier routes work to the brain (`nlu/intent.py`)

Two surgical changes stop the desktop layer from swallowing *tasks*:

- **Task-verb / task-noun tie-break in `_fallback_classify`:** when the
  leading verb is a desktop verb (`open`/`launch`/`start`/`run`/`go`/
  `create`) and the utterance contains a task verb ("install", "clone",
  "set up"…) or task noun ("project", "venv", "repo", "bot"…) the intent
  flips from DESKTOP to **PLAN/EXECUTE** — a decisive +2 (not a tie),
  so "open a fresh project for a discord bot" becomes a mission.
- **Agentic markers in the resolver:** task phrases resolve to
  `action_type="claude"` — an empty-command EXECUTE delegates to the
  **Claude Code executor** (the open-ended arm, Wave 18), not "what
  would you like me to run?".

Resulting routing for the "handle everything" suite:

```
'open whatsapp'                                    -> desktop
'open chrome on workspace 3'                       -> desktop
'open youtube and cristiano ronaldo channel in it' -> desktop
'open c++ compiler of programiz on it'             -> desktop
'search for the best rust web framework'           -> desktop (search)
'open a python venv and install requests'          -> execute (claude)
'open a fresh project for a discord bot'           -> plan (mission)
'clone the repo and open it in my editor'          -> execute (claude)
'set up a fresh project for a discord bot'         -> plan (mission)
'organize my downloads folder by file type'        -> execute (claude)
```

### 3. Voice routes through the SAME interpreter (`voice/router.py`)

The voice router's legacy desktop parser was deleted; `_try_desktop_command`
now delegates to the shared `desktop_text_command` — but **gated on the
shared NLU**: only a genuine `desktop` intent reaches the interpreter
("yes, run it" is `accept`, never "Opened a web search for 'it'").
One desktop language across voice, CLI, web, and phone.

---

### 4. The app-learning loop — "open my todo app" (`desktop/app_aliases.py`)

Wave 20 follow-up (user directive: *"teach it once, then always"*).
Friday remembers personal apps, persisted to
`~/.friday/v4_desktop_aliases.json` (pure stdlib, atomic writes):

- **Teaching frames** (parsed by `parse_learning_phrase`):
  - `my todo app is obsidian` / `todo app is obsidian` (app-like names
    only — "my code is broken" never trips this frame)
  - `use obsidian for my todo app`
  - `set my todo app to obsidian`
  - `open my todo app with obsidian` — teaches AND opens in one breath
- **Honesty law:** only *resolvable* binaries are learned
  (`shutil.which` / existing absolute path); an unresolvable command
  gets "I couldn't find 'X' on this machine" and nothing is saved.
- **Resolution order** in `_resolve_app`: learned aliases → builtin
  aliases → PATH → app-suffix stripping ("spotify app" → "spotify").
- **Unknown personal apps are taught, not web-searched:** "open my
  todo app" the first time answers "I don't know what 'todo app' is
  yet. Teach me once — say 'my todo app is <command>'…" instead of a
  useless web search of the operator's own app. "open c++ compiler of
  programiz" still web-searches (not personal).
- **Every surface:** the deterministic classifier routes learning
  phrases to DESKTOP (voice/offline), and the NL router pre-dispatch
  hook routes them to the desktop handler even when an LLM would call
  them ASK (CLI/web/phone).
- **CLI:** `friday4 desktop aliases` / `teach <name> <command>` /
  `forget <name>`.
- **Forgetting:** "forget" is one command away (`forget_alias`).

### 5. Cross-machine continuity — aliases ride the collab bus

Follow-up (user directive: *"cross-machine continuity — sync learned
app aliases via the collab layer"*). An alias taught on the laptop
works on the desktop:

- **Publish:** `aliases_as_observations` turns every alias into a
  collab observation keyed `alias:<name>` (source
  `v4.app_aliases`), so the CRDT's per-source:subject:aspect LWW
  reconciles concurrent teaching — whoever taught last wins.
- **Merge:** `apply_collab_observations` merges peer aliases into the
  local store (never crashes on foreign sources / malformed entries).
- **Safety:** `_resolve_app` only launches a learned/synced binary
  that resolves *on this machine* — a synced alias for an app that
  isn't installed here falls through gracefully instead of firing a
  dead launch.
- **CLI:** `friday4 desktop aliases-sync` — push local aliases, sync
  with peers, merge remote aliases; collab absent/offline degrades to
  a message, never a crash.

## The "everything" contract (no hardcoded workflows)

The desktop layer is explicitly **not** a closed catalog:

1. Fast deterministic cases (open/focus/switch/workspace/browser/known
   sites/site-search/explicit search) are handled inline — these are
   instant and offline.
2. Explicit searches and noun-phrase queries web-search.
3. **Everything that reads like work falls through** — `desktop_text_command`
   returns `""`, the intent is PLAN/EXECUTE, and the mission planner
   (ClaudePlanner, Wave 18) or the Claude Code executor picks it up
   through the same permission gate. No new hardcoded workflow; the
   ceiling is whatever the agentic arms can do, which grows independently.

---

## Tests

`tests/test_hardening_nl.py` (TestHardeningDesktop, hermetic — WM and
`shutil.which` patched):

- All five user examples resolve correctly (workspace + compound +
  whatsapp-destination + youtube site-search + programiz web-search).
- Compound splitting: `and then` / `also` connectors; no split when the
  connector joins a search phrase; **task phrases never split**.
- Task fall-through: "open a python venv and install requests", "open a
  fresh project for a discord bot", "clone the repo and open it in my
  editor" all return `""` from the interpreter (the brain's job).
- Explicit search → real search URL; site search → youtube results URL
  with the exact query.
- Launch honesty: an unresolvable binary never claims "Launching X";
  a resolvable one (patched `shutil.which`) does.
- Install-gated regression + the existing accept-vs-desktop guard
  ("yes, run it" with nothing pending speaks the honest answer).

`tests/test_nlu.py` / `tests/test_nl_router.py` / `tests/test_voice.py`
cover the classifier tie-break and the voice router's NLU-gated
delegation.

**Count:** 1172 → full suite green (incl. 71 voice tests) with the new
classifier + interpreter + regression tests.

## Close-out

Desktop control is now *natural language*, not a phrase list: compound
commands, workspace and browser qualifiers, web destinations, site
search, honest install-gated launches, explicit searches — and an
**open-ended fall-through to the agentic brain** so Friday never
web-searches work the arms could do. Voice, CLI, web, and phone all
speak the same desktop language. ROADMAP / MASTER_PLAN / PLAN updated to
✅ SHIPPED; docs site rebuilt.
