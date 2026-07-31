# Friday V4 — The Vision

> **Not a tool you type at. A partner you speak with.**
>
> This is what it means to build Tony Stark's Friday for software engineering.

---

## The Core Difference

**V3 was a powerful CLI tool with 40+ commands that you had to learn.**
```
$ friday observe
$ friday ask "what changed?"
$ friday knowledge build
$ friday initiatives build
$ friday understanding build
$ friday insights build
$ friday plan "deploy the server"
$ friday graph ...
$ friday resolve ...
$ friday schedule ...
$ friday runtime ...
$ friday repair ...
```

**V4 is a partner you talk to.**
```
🎤 You: "Hey Friday, what's going on?"
🎧 Friday: "3 repos changed, 2 new initiatives, 1 vulnerability found."
         You didn't type a single command.
```

That's the difference. V3 made you learn **its language**. V4 learns **yours**.

---

## What Friday V4 Looks Like

### The Interface

There is no "interface." That's the point.

You don't open a Friday app. You don't navigate to a Friday dashboard. Friday is
**ambient** — always there, never in the way, interrupting only when it matters.

```
┌─────────────────────────────────────────────────────────┐
│  Your Desktop                                            │
│                                                          │
│  ┌────────────────────┐  ┌──────────────────────────┐   │
│  │ VS Code             │  │ Terminal                  │   │
│  │                     │  │                           │   │
│  │  Friday is here →  │  │  $ git push               │   │
│  │  [Status bar icon]  │  │  [Friday speaks:          │   │
│  │                     │  │   "Review the PR first?   │   │
│  │  Friday is here →   │  │    There are 3 failing    │   │
│  │  [Sidebar shows     │  │     tests on main."]      │   │
│  │   feed + security]  │  │                           │   │
│  └────────────────────┘  └──────────────────────────┘   │
│                                                          │
│  ┌──── System Tray ────┐                                 │
│  │  ◆ Friday ● Active  │  <-- Click for quick status    │
│  └─────────────────────┘                                 │
└─────────────────────────────────────────────────────────┘
```

**The interfaces (in priority order):**

1. **Your voice** — "Hey Friday..." — primary interaction
2. **Your system tray** — glanceable status, click for quick actions
3. **Your notifications** — Friday taps you on the shoulder when it matters
4. **Your editor** — inline hints, status bar, quick actions
5. **Your terminal** — `friday` command for when you want to type
6. **Your phone** — push notifications, voice input on the go

---

### A Day With Friday V4

**Morning — Ambient Briefing**

You sit down at your desk. Before you touch the keyboard:

```
🎧 Friday: "Good morning. While you were away, codebuff had 8 commits
           from 3 contributors. Aether's build is failing on main —
           looks like a type error in the scheduler. MindWell's
           dependencies have 2 high-severity vulnerabilities.
           
           I've already scanned everything and prepared a summary.
           Want me to walk through it?"
```

You didn't ask. Friday knows your patterns — you always check status first thing.

**Mid-morning — Proactive Help**

You open `main.py` in VS Code. Friday notices:

```
🎤 You: (opening main.py, saying nothing)
🎧 Friday: "You're about to work on the auth module. The last time you
           edited this file, you also updated tests/test_auth.py.
           I ran those tests — they pass. Want me to open the test
           file too?"
```

Friday learned this pattern from watching you work. Not ML. Just observation.

**Mid-day — Security Alert**

You're in a meeting. Your phone buzzes silently:

```
📱 Friday notification:
   "CRITICAL: CVE-2026-XXXX found in your production dependency
    'requests' v2.31.0. Remote code execution. Patch available.
    I've created a repair plan — review when you're back."
```

Your phone. Not a Slack message. Not an email. Friday found it, understood its
severity, and queued it for when you have time.

**Afternoon — Natural Conversation**

```
🎤 You: "Friday, what's the deal with vivaha and MindWell?"

🎧 Friday: "They share a surprising amount of architecture. Both use
           React + Supabase, both have similar auth flows, and both
           define the same data model for user profiles. I actually
           flagged this as a cross-project correlation last week.
           
           You could probably share components between them — I
           analyzed the integration cost and it's about 3 days of work."
```

No CLI commands. No `friday ask` + `friday suggest` + `friday correlate`.
Just a natural question and a complete answer.

**Evening — Wrap-up**

```
🎤 You: "Alright Friday, wrap it up for the day."

🎧 Friday: "Here's your day:
           - 4 files edited, 12 tests passing
           - 1 PR reviewed and merged
           - 2 vulnerabilities fixed
           - 1 new skill formed from your git workflow
           
           I'll run full security scans overnight. See you tomorrow."
```

---

## The MCU Friday Feature Map

Every MCU Friday capability, translated to software engineering:

### 1. 🗣️ Voice Interface (Iron Man talks to Friday)

**MCU:** Tony speaks naturally, Friday responds aloud.
**V4:** "Hey Friday, what's the build status?" → spoken answer.

**How it works:**
- Microphone listens for "Hey Friday" (hotword)
- Speech is transcribed locally (Whisper) — no cloud needed
- Text routes through V3's persona engine (already has conversation history,
  name learning, preference extraction, memory)
- Response is spoken aloud via local TTS (Piper)
- You can interrupt Friday mid-sentence

**No typing required. Ever.**

### 2. 📊 Holographic HUD → Ambient Awareness

**MCU:** Friday shows suit diagnostics, threat assessments, mission data.
**V4:** Friday tells you what matters *when* it matters.

**How it works:**
- Background daemon observes everything (git, files, desktop, calendar)
- When something important happens, Friday *speaks up*
- Things that can wait are queued for your next check-in
- Nothing pops up in your face unless it's critical

**No dashboard to check. No commands to run. Friday brings the news to you.**

### 3. 🏠 Environment Control → Desktop Command

**MCU:** "Friday, lights." "Friday, suit mode."
**V4:** "Friday, switch to workspace 3." "Friday, open the project."

**How it works:**
```
🎤 You: "Friday, open codebuff in VS Code."
🎧 Friday: [Opens VS Code, loads codebuff project]
🎧 Friday: "Opened. 12 new commits since you last looked."
```

- Controls windows, workspaces, apps across your desktop
- Knows what you're working on (active window, open files, clipboard)
- Can launch, close, switch, arrange — anything you'd do with a mouse

**Your desktop, commanded by voice.**

### 4. 🤖 Suit Diagnostics → Code Health

**MCU:** Friday runs real-time diagnostics on the Iron Man suit.
**V4:** Friday runs real-time diagnostics on your codebase.

**How it works:**
```
🎤 Friday: "I noticed test coverage dropped 3% this week.
           The 'auth' module is the main contributor.
           Want me to suggest test cases?"
```

- Continuous quality monitoring (coverage, complexity, churn)
- Dependency vulnerability scanning on every change
- Secret detection (no more accidentally committing API keys)
- Performance regression detection

**Friday is your code's guardian.**

### 5. 🔬 Lab Analysis → Research & Synthesis

**MCU:** Friday analyzes materials, runs simulations, suggests solutions.
**V4:** Friday analyzes codebases, runs impact assessments, suggests approaches.

**How it works:**
```
🎤 You: "Friday, analyze the integration cost between vivaha and MindWell."

🎧 Friday: "I already did that last week. The shared auth system would
           save about 3 days of duplicated work. The main risk is
           MindWell's custom session handling — it diverges from
           vivaha's approach. Want the full breakdown?"
```

- Architecture analysis (already exists in V3)
- Cross-project integration analysis (already exists in V3)
- Impact assessment of changes
- Pattern matching across repos

**Friday has already done the research before you asked.**

### 6. 🚁 Mission Control → Multi-Task Orchestration

**MCU:** Friday coordinates the Iron Legion drones.
**V4:** Friday coordinates your development tasks.

**How it works:**
```
🎤 You: "Friday, I need to ship the auth refactor by Friday."

🎧 Friday: "Got it. I've created a plan:
           - Day 1: Migrate session handling (4 files)
           - Day 2: Update tests (estimated 8 files)
           - Day 3: Security audit + deploy
           
           Want me to start prepping the test files?"
```

- Breaks goals into tasks (V3's planning pipeline)
- Schedules work across available time
- Executes tasks autonomously when safe
- Reports progress proactively

**Friday plans. Friday executes. You direct.**

### 7. 🧠 Learning & Adaptation

**MCU:** Friday learns Tony's preferences, work style, and personality.
**V4:** Friday learns how you code, what you care about, and how you work.

**How it works:**

```
Phase 1 — Observation:
  Friday watches what you do:
  - "He always runs tests before pushing"
  - "He prefers Python over JavaScript for backend work"
  - "He checks security scans at the end of the day"
  - "He names him with under_scores, not camelCase"

Phase 2 — Pattern Recognition:
  Friday connects the dots:
  - "Editing main.py → opening test_main.py → running pytest"
  - "Monday mornings → checking PRs from Friday afternoon"
  - "After a failed deploy → running rollback script"

Phase 3 — Anticipation:
  Friday acts on patterns:
  - [You open main.py]
  - Friday: "Tests are already running for you."
  - [You finish editing]
  - Friday: "I've staged your changes and run the linter."
```

**No configuration files. No "training mode." Friday learns by watching.**

### 8. 🔐 Security

**MCU:** Friday protects the suit from threats.
**V4:** Friday protects your code from vulnerabilities.

**How it works:**
- Every dependency change triggers an audit
- Every commit is scanned for secrets
- Weekly full security reports
- Proactive alerts for critical CVEs

**Friday is your security guard.**

### 9. 👤 Personality & Relationship

**MCU:** Friday knows Tony — his preferences, his history, his mood.
**V4:** Friday knows you.

**How it works:**
```
🎤 You: "Friday, who am I?"

🎧 Friday: "You're Lakshay. You prefer Rust for performance-critical
           code, Python for tooling. You've been working on Friday V4
           for the past 3 weeks. You like concise answers in the
           morning and detailed explanations in the afternoon.
           
           You're not a morning person, so I kept the briefing short."
```

- Learns your name naturally ("Call me Lakshay" → remembers)
- Learns your preferences ("I prefer Python" → adapts)
- Adapts tone based on relationship depth
- Remembers context across sessions
- Never forgets what you told it

**Friday gets to know you over time, like a real partner.**

---

## What's Different From V3

### Simplicity

| Dimension | V3 (Old Way) | V4 (New Way) |
|-----------|-------------|-------------|
| **Commands to learn** | 40+ CLI commands | **1 voice command**: "Hey Friday" |
| **Primary interaction** | Typing | **Speaking** |
| **How you check status** | `friday daemon status` | **"Hey Friday, what's new?"** |
| **How you run a task** | `friday execute --goal "..."` | **"Friday, deploy the server."** |
| **How you learn something** | `friday ask "what is X?"` | **"Friday, what's X?"** |
| **How you configure** | Edit config files | **"Friday, call me Lakshay."** |
| **How you control desktop** | Hyprland keybindings | **"Friday, switch workspace."** |

You don't need to learn Friday's language. Friday learns yours.

### Learning vs Configuration

| V3 | V4 |
|----|----|
| You configure preferences | Friday learns preferences by watching |
| You type `friday profile set` | You say "I prefer Python" once |
| You run commands manually | Friday predicts what you want and offers |
| You check the daemon status | Friday speaks up when something changes |
| You initiate every interaction | Friday initiates when it matters |

### What V3 Did Right (Kept in V4)

V3's core is genuinely brilliant. V4 doesn't throw it away — it hides it.

| V3 Engine | V4 Surface |
|-----------|-----------|
| Observation Engine | Friday notices things automatically |
| Knowledge Engine | Friday knows your projects |
| Persona Engine | Friday talks like a partner |
| Planning Pipeline | Friday breaks down goals |
| Executors | Friday takes action |
| Ambient Feed | Friday's memory of events |
| Memory Engine | Friday remembers what you said |

**V3 is the brain. V4 is the voice, eyes, and hands.**

---

## A Concrete Comparison

### V3: Doing a Security Check

```
$ friday knowledge build
$ friday security scan         # hypothetical V3 command
$ friday initiatives build
$ friday insights build
$ friday suggest
> Output: 27 lines of structured data
> You read through it manually
> You decide what to do
```

### V4: Doing a Security Check

```
🎤 You: "Friday, check my security."

🎧 Friday: "I already did. 2 vulnerabilities found:
           - requests v2.31.0 (HIGH) — fixed in v2.32.0
           - pyyaml v6.0 (MEDIUM) — fixed in v6.0.1
           
           I've prepared the upgrade commands. Want me to run them?"

🎤 You: "Go ahead."

🎧 Friday: "Upgrades complete. Both tested and passing. Anything else?"
```

**V3: 5 commands, 27 lines of output, you do the work.**
**V4: 6 words, Friday does the work.**

---

## What Friday V4 Cannot Do (Yet)

These are intentional non-goals. Just like V3 had known limitations,
V4 has boundaries:

1. **No cloud sync** — Everything runs locally. Your data stays on your machine.
2. **No code generation** — Friday analyzes, suggests, and executes safe tasks.
   It doesn't write your features for you.
3. **No autonomous push** — Friday will never push to production without your
   explicit approval.
4. **No general AI** — Friday is an engineering partner, not a chatbot.
5. **No video/audio calling** — Phone notifications only.

But unlike V3's limitations (which were architectural), these are **policy**
decisions. They can change if you want them to.

---

## The Two-Sentence Summary

**V3 made you learn 40 CLI commands to talk to an AI.**
**V4 makes the AI learn your one natural language.**

Friday V4 doesn't have a learning curve. It has a conversation.

---

*This is the vision. Everything we build — every module, every file, every
test — exists to make this feeling real. If a feature doesn't bring us closer
to "talking to Friday like Tony Stark," it doesn't belong in V4.*
