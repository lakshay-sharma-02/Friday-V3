# FRIDAY — Complete Implementation Catalog

This directory contains a prompt for each domain, describing what to build, *why* it matters, and *how* to build it right. Organized by the gap analysis from the full feature map.

## Order of Implementation (recommended)

### Tier 1 — High Impact, Moderate Effort
Start here. These change how FRIDAY *feels* the most per unit of work.

1. **Presence & Attention** (`presence-attention.md`) — Intelligent interrupt system. The single highest-leverage change. FRIDAY knowing WHEN to speak changes everything.
2. **Daily Operations** (`daily-operations.md`) — Morning briefing + spontaneous code review. The "morning partner" ritual makes FRIDAY feel alive.
3. **System Intelligence** (`system-intelligence.md`) — Live telemetry + process monitoring. Makes FRIDAY feel *inside* your machine.

### Tier 2 — High Impact, More Effort
These require more infrastructure but are very FRIDAY-feeling.

4. **Analysis & Insight** (`analysis-insight.md`) — Semantic code search + change impact + codebase narrative. The "suit intelligence" features.
5. **Relationship & Personalization** (`relationship-personalization.md`) — Emotional awareness + deepening rapport. Makes the partnership feel real.
6. **Voice & Audio** (`voice-audio.md`) — STT/TTS. The most iconic modality shift. Save for after the intelligence layer is solid so FRIDAY has smart things to say.

### Tier 3 — Power User / Polish
Build these when the foundation is solid.

7. **Autonomous Agency** (`autonomous-agency.md`) — Persistent missions + sub-agents + adaptive plans. Advanced autonomy.
8. **Sandbox & Safety** (`sandbox-safety.md`) — What-if simulation + dry-run + rollback. Safety infrastructure.
9. **Collaboration & External** (`collaboration-external.md`) — Remote guidance + translation + meeting transcription + pair assist.
10. **Presentation & Interface** (`presentation-interface.md`) — HUD + web interface + rich reports. The visual layer.

## Principles for Every Feature

1. **LLM-optional** — Every feature works without an LLM. The LLM enhances, the deterministic path is the core.
2. **Graceful degradation** — If a dependency is missing, the feature degrades, it doesn't crash.
3. **Append-only** — No data deletion. History is preserved.
4. **No new required dependencies** — Optional deps only. The project builds and runs with zero new installs.
5. **Each feature is testable** — Every module gets a test file. Deterministic paths are unit-tested.
6. **Existing architecture laws apply** — Downward-only dependencies, no circular imports, deterministic core, no data loss. See `docs/CORE_ARCHITECTURE_LAWS.md`.

## How to Use These Prompts

1. Pick the feature you want to build
2. Read the prompt to Claude Code (paste into the conversation or `claude -p "$(cat prompt)"`)
3. Claude Code will implement following the design decisions in the prompt
4. Run the test suite after each feature: `python -m pytest tests/`
5. Don't build features in parallel — each one builds on the foundation
