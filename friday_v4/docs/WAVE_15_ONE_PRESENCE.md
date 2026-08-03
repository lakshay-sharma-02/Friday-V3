# Wave 15 — One Presence (cross-surface continuity) ✅ SHIPPED

> **The sentence clause:** *single presence* — "MCU FRIDAY is a single
> presence that speaks natural language …" Same memory, same context,
> same conversation across terminal / web / voice / desktop / mobile.
> One Friday, not five tools.
>
> **This wave (shipped 2026-08):** the shared context session (slice 1),
> web-dashboard chat polish (slice 2), ambient push reaching every
> surface (slice 3), and mobile transport via the durable queue (slice 4).
> "What did we talk about this morning?" is answered from any surface
> with the actual conversation. Parent docs:
> [THE MCU FRIDAY STANDARD](MCU_FRIDAY_STANDARD.md) Law 10 (one
> presence, every surface) · [THE FRIDAY MASTER
> PLAN](MASTER_PLAN.md) Wave 15.

---

## 1. The design

### One session follows you across surfaces

Before, every conversational surface called ``start_session(surface)``
per utterance — one session row per exchange, so "the conversation" was
fragmented across hundreds of sessions and surfaces. The Wave 15
principle is literal:

> **The shared session is the presence.** All conversational surfaces
> (talk, ask, voice, web chat) append to the **same** ``sessions`` row
> — ``surface='shared'``, one per UTC day, created on first use that
> day. A conversation started in the terminal *is* the conversation
> heard in voice and read on the phone.

``db.get_or_create_shared_session(conn)`` returns today's shared
session (creating it on first use). The day-boundary comparison is
lexical (``substr(started_at, 1, 10)`` on the ISO date) — no dependence
on SQLite's ``date()`` parser for microsecond + timezone timestamps.
Midnight rolls the presence into a fresh session; each day stays
addressable (``list_sessions``, relationship counts).

Wired into the three conversational loggers:

| Logger | Before | After |
|---|---|---|
| `nl_router._log_exchange` (talk + voice + web chat + mobile talk — every surface routes through `TextCommandHandler`) | `start_session("talk")` per utterance | `get_or_create_shared_session` |
| `cli_ask._log_exchange` (`friday4 ask`) | `start_session("ask")` per Q&A | `get_or_create_shared_session` |
| `persona/learn.record_statement` ("who am I" material) | `start_session(surface)` per statement | `get_or_create_shared_session` |

**Why this is safe:** the relationship depth formula weights
*exchanges*, *missions*, and *facts* — the ``sessions`` counter is not
part of the score (it is display-only), so one-session-per-day cannot
regress depth.

### Time-window recall (the MCU test)

``conversation_provider`` (``reasoning/providers.py``) now answers
time-windowed questions by filtering the log to the window instead of
the last-N turns:

| You ask | Window (UTC) |
|---|---|
| "what did we talk about **this morning**?" | today 00:00 → 12:00 |
| "…this afternoon?" | today 12:00 → 18:00 |
| "…this evening?" / "tonight" | today 18:00 → tomorrow |
| "…today?" / "earlier today" | today 00:00 → now |
| "…yesterday?" | yesterday → today |
| "…last night?" | yesterday 18:00 → today |
| "…this week?" | Monday → now |
| "…last week?" | last Monday → this Monday |
| (no window) | recent-N (existing behavior) |

``db.recent_exchanges_since(conn, since, until, limit)`` is the window
query (newest first, exclusive end). An empty window → ``None`` → the
honest "I don't know yet" (never fabricated). ``now`` is injectable in
the helper for deterministic tests; timestamps are stored UTC, so
windows are computed UTC.

### Classification fix

"what did we talk about **yesterday**?" contains ACTIVITY's bare
"yesterday" trigger, so it used to classify ACTIVITY and get the wrong
provider. The CONVERSATION rule now precedes ACTIVITY — time-windowed
conversation questions land on the conversation provider. "what did we
**do** yesterday" has no conversation trigger and stays ACTIVITY
(regression-tested).

---

## 2. Slices 2–4 — the browser, the bus, the phone

### Slice 2 — web-dashboard chat polish (resume the thread in the browser)

The Wave 9 chat card was stateless: it started empty every load, so the
terminal conversation never showed up in the browser. Now:

- **`web/dashboard.conversation_state()`** — today's shared-session
  exchanges (oldest first) via a read-only probe.
- **`GET /api/conversation`** — the server route.
- **Chat hydrate** — the dashboard fetches ``/api/conversation`` on
  load and renders the shared thread into the chat log (with a
  "↳ resuming today's shared conversation" note), so a conversation
  started in the terminal continues *visibly* in the browser. The chat
  still sends through ``/api/talk`` → the same ``nl_router`` brain.

### Slice 3 — ambient push reaches every surface

Wave 11 gave us the bus + durable queue + web SSE. This wave wires the
*surfaces* onto it:

- **Wildcard subscribe** — ``AmbientBus.subscribe("*", fn)`` delivers
  every event (topic-specific subscribers still work unchanged). A
  surface that wants all push traffic subscribes once instead of
  enumerating topics.
- **`speak_channel(speak_fn, min_priority=CRITICAL)`** — a voice
  channel that speaks events at/above CRITICAL via the pipeline's TTS.
- **`desktop_channel(notify_fn, min_priority=IMPORTANT)`** — a desktop
  channel that raises a banner for IMPORTANT+ events.
- **Daemon wiring** — ``AmbientWorker.wire_channels(speak_fn,
  notify_fn)`` subscribes the live voice pipeline and desktop notifier
  to the shared bus (when present; missing surfaces degrade silently).
  The web SSE stream and the mobile push transport already read the
  durable queue directly.

### Slice 4 — mobile transport via the durable queue

The phone becomes another surface of the *same* Friday:

- **`mobile/push.py`** — ``PushNotificationService``: a durable-queue
  consumer that replays ``ambient_events_since(cursor)`` to a pluggable
  transporter (``file_transporter`` writes a JSONL outbox for
  tests/offline inspection; a companion app plugs FCM/APNS/webhook in).
  The rowid cursor persists across restarts (a reconnecting phone
  misses nothing; **sequential** consumers re-deliver nothing — the
  CLI and the daemon share one cursor file, so concurrent manual+
  daemon drains are advisory-only, never a source of duplicates in
  normal use); a failing transporter never wedges the queue;
  ``min_priority`` filters what's *pushed* while the cursor still
  advances.
- **`mobile/api.py`** — ``MobileAPI`` + ``create_api_server``: a
  pure-stdlib HTTP server for the companion —
  ``GET /api/status``, ``GET /api/conversation`` (the shared thread),
  ``POST /api/talk`` (the same ``nl_router`` brain — one command
  language everywhere), and ``GET /api/events`` (SSE over the durable
  queue, replayable since a cursor).
- **`friday4 mobile serve|push`** — run the companion API
  (``friday4 mobile serve --port 8900``) or drain the queue to the
  transporter (``friday4 mobile push --once``). The unified ``friday4
  status`` gains a ``mobile`` row.
- **`mobile/__init__.py`** — the Wave 7 stub is closed: real exports,
  ``is_available()`` → True.

---

## 3. Wiring table (the Wiring Law)

| Consumer | Wired? | How |
|---|---|---|
| `friday4 talk` (nl_router `_log_exchange`) | ✅ | shared session |
| `friday4 ask` (cli_ask `_log_exchange`) | ✅ | shared session |
| Voice (`VoiceRouter` → `TextCommandHandler`) | ✅ (inherited) | shared session via nl_router |
| Web chat (`web/dashboard` → `nl_router`) | ✅ (inherited) | shared session via nl_router + **chat hydrate** |
| Mobile talk (`mobile/api` → `nl_router`) | ✅ | shared session via nl_router |
| Persona learning (`record_statement`) | ✅ | shared session |
| Reasoning `conversation_provider` | ✅ | time-window recall + `recent_exchanges_since` |
| Question classifier | ✅ | CONVERSATION precedes ACTIVITY |
| Web SSE (`/api/events`) | ✅ (Wave 11) | durable queue |
| Voice channel (speak CRITICAL) | ✅ | `speak_channel` via `AmbientWorker.wire_channels` |
| Desktop channel (banner IMPORTANT+) | ✅ | `desktop_channel` via `AmbientWorker.wire_channels` |
| Mobile push (durable queue) | ✅ | `PushNotificationService` + `/api/events` SSE |
| **Mobile push on a schedule** | ✅ | daemon `MobilePushWorker` (drains the queue every `mobile_push_interval`, persisted cursor) |
| **Operator-configurable destination** | ✅ | `--mobile-push-hook "<cmd>"` / `--mobile-push-file <path>` (CLI) **or** `mobile_push` section of `~/.friday/v4_config.json` (`hook`/`file_path`/`interval`/`priority`/`enabled`) **or** `FRIDAY_V4_MOBILE_PUSH_*` env — the daemon's worker pipes each notification's JSON to the hook's stdin / appends it to the outbox |
| `friday4 mobile` CLI + `status` probe | ✅ | `cli_mobile` + `_probe_mobile` |
| `friday4 daemon start` mobile flags | ✅ | `--no-mobile-push` · `--mobile-push-interval` · `--mobile-push-priority` · `--mobile-push-hook` · `--mobile-push-file` |

**Checklist:** every entry point reaches the shared thread (talk / ask /
voice / web / mobile) ✅ · time-driven daemon work (surface channel
wiring) ✅ · reasoning cites the new state (windowed recall) ✅ ·
CLI surface (`friday4 mobile serve|push`) ✅ · hermetic tests ✅.

---

## 4. MCU test (Law 10, five-moment continuity)

> "what did we talk about this morning?" — from **voice** — returns the
> conversation that happened in the **terminal** this morning.

Verified end-to-end in `tests/test_wave15_one_presence.py`
(`TestMcuOnePresence.test_terminal_conversation_recalled_from_voice`):
a terminal utterance this morning → a voice-surface ask → the reasoning
conversation provider cites the terminal exchange
(``v4.exchanges`` evidence).

**One presence proven across every surface:** `TestWebConversation`
shows the terminal thread hydrating the *browser*; `TestMobileAPI`
shows the terminal thread read from the *phone*; `TestSharedSession`
shows talk + voice + persona all appending to ONE row.

---

## 5. What actually shipped (close-out)

**Slice 1 — shared context session:**
1. **`db.get_or_create_shared_session(conn, now=None)`** — the one
   presence: one ``surface='shared'`` session per UTC day, created on
   first use; lexical day comparison; deterministic ``now``.
2. **`db.recent_exchanges_since(conn, since, until=None, limit)`** —
   the time-window conversation query (newest first, exclusive end).
3. **`_conversation_window(text, now=None)` + windowed
   `conversation_provider`** — "this morning/afternoon/evening/tonight/
   today/yesterday/last night/this week/last week"; empty window stays
   an honest unknown.
4. **Classifier reorder** — CONVERSATION precedes ACTIVITY.
5. **Shared-session loggers** — `nl_router`, `cli_ask`,
   `persona/learn.record_statement`.

**Slice 2 — web chat polish:**
6. **`web/dashboard.conversation_state()` + `GET /api/conversation` +
   chat hydrate** — the browser resumes today's shared thread.

**Slice 3 — ambient push to every surface:**
7. **`AmbientBus.subscribe("*")` wildcard** — surface-wide push.
8. **`speak_channel` / `desktop_channel` builders** — voice + desktop
   surface channels (priority-gated, guarded).
9. **`AmbientWorker.wire_channels`** — the daemon subscribes the live
   voice pipeline and desktop notifier to the shared bus.

**Slice 4 — mobile transport:**
10. **`mobile/push.py`** — `PushNotificationService` (cursor-persisted
    durable-queue consumer), `file_transporter` JSONL outbox.
11. **`mobile/api.py`** — `MobileAPI` + `create_api_server` (status /
    conversation / talk / SSE).
12. **`mobile/__init__.py`** — stub closed, `is_available()` → True.
13. **`cli_mobile.py`** — `friday4 mobile serve|push`; wired into the
    integrated CLI + `_SUBCOMMANDS` + `_probe_mobile` status row.
14. **`daemon.MobilePushWorker`** — the phone gets pushed *without
    manual `friday4 mobile push`*: a scheduled component that drains
    the durable queue every ``mobile_push_interval`` (default 60s),
    wrapping ``PushNotificationService`` with the same injectable
    transporter/priority/state-file/db-path, ``last_report`` for
    status, and the never-crash contract. ``DaemonConfig`` gains
    ``mobile_push`` / ``mobile_push_interval`` /
    ``mobile_push_priority``; ``friday4 daemon start`` gains
    ``--no-mobile-push`` / ``--mobile-push-interval`` /
    ``--mobile-push-priority``; the daemon status row ``mobile``
    reports it. **Priority default:** 0 (push everything, including
    the ROUTINE briefing — the phone is a full surface); set
    ``--mobile-push-priority 1`` to push only IMPORTANT+ events.
    ``friday4 mobile push`` prints an informational hint when the
    daemon is running (the daemon owns the schedule; the CLI stays a
    manual hatch).
15. **Operator-configurable push hook (close-out 2):**
    ``mobile/push.command_transporter(command)`` — a transporter that
    pipes each notification's JSON to a **shell command's stdin** (the
    operator's own destination: ``curl -s -X POST -d @-``
    https://ntfy.sh/…, ``cat >> ~/friday-push.log``, …); never
    raises (missing binary / nonzero exit / timeout log and the poll
    continues). ``config.MobilePushConfig`` — the ``mobile_push``
    section of ``~/.friday/v4_config.json`` (``hook`` / ``file_path``
    / ``interval`` / ``priority`` / ``enabled``) + ``FRIDAY_V4_MOBILE_PUSH_*``
    env overrides. ``MobilePushWorker(hook=…, file_path=…)`` — an
    explicit injected transporter wins, then hook, then file_path,
    then the default logger. ``friday4 daemon start`` gains
    ``--mobile-push-hook`` / ``--mobile-push-file`` (CLI flags win;
    the config file fills the rest).

**Tests:** `tests/test_wave15_one_presence.py` (13 hermetic — slice 1)
+ `tests/test_wave15_transport.py` (38 hermetic — slices 2–4: push
cursor/replay/filter/restart, companion API + HTTP server, web
hydrate, wildcard subscribe, surface channels, daemon wiring, the
scheduled `MobilePushWorker` — deliver / graceful / lifecycle /
status row / builds-when-enabled / disables-when-flagged /
shutdown-stops — **and the hook suite** — stdin JSON delivery,
missing-binary never-crashes, timeout-bounded, worker hook / file_path
wiring, injected-transporter-wins). Full suite green.

---

## 6. What we learned

- **Lexical day comparison beats SQLite date()** — timestamps carry
  microseconds and a UTC offset; ``substr(created_at, 1, 10)`` is
  bulletproof and index-friendly.
- **The engine's never-crash guard hid a bug** — a missing ``datetime``
  import in the provider surfaced as "I don't know yet" (the engine
  swallowed the exception). The window-bounds unit test caught it; the
  lesson is that providers must be unit-tested directly, not only
  through ``answer()``.
- **One-session-per-day is a feature for depth** — relationship depth
  ignores session count, and a daily thread makes "this morning" a real
  query boundary rather than an arbitrary last-N slice.
- **A durable queue is the natural mobile transport** — the phone
  doesn't need a bespoke push service: the same rowid-cursor replay the
  web SSE uses gives a companion "miss nothing, re-deliver nothing"
  semantics for free, and a pluggable transporter keeps FCM/APNS an
  app-side detail.
- **Status probes are part of the CLI contract** — adding a subsystem
  probe changes the "All subsystems ready" outcome; the status test
  must stub it like every other probe.
