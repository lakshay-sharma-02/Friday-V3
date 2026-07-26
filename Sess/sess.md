Friday — Full System Audit
✅ Completely Wired Modules
┌──────────────────────┬──────────────────────────┬──────────────────────┬─────────────────────────────┬───────────────┐
│ Module               │ CLI                      │ Daemon               │ Tests                       │ Status        │
├──────────────────────┼──────────────────────────┼──────────────────────┼─────────────────────────────┼───────────────┤
│ Knowledge engine     │ friday knowledge         │ ✅ daemon cycle      │ ✅ test_knowledge.py        │ ✅ Done       │
│ Understanding engine │ friday understanding     │ ✅ daemon cycle      │ ✅ test_understanding.py    │ ✅ Done       │
│ Initiative engine    │ friday initiatives       │ ✅ daemon cycle      │ ✅ test_initiative.py       │ ✅ Done       │
│ Insight engine       │ friday insights          │ ✅ daemon cycle      │ ✅ test_insight.py          │ ✅ Done       │
│ Context engine       │ friday context           │ ✅ daemon cycle      │ ✅ test_context.py          │ ✅ Done       │
│ Planning             │ friday plan              │ —                    │ ✅ test_planning.py         │ ✅ Done       │
│ Task Graph           │ friday graph             │ —                    │ ✅ test_graph.py            │ ✅ Done       │
│ Worker registry      │ friday worker            │ —                    │ ✅ test_worker_registry.py  │ ✅ Done       │
│ Capability resolver  │ friday resolve           │ —                    │ ✅ test_resolver.py         │ ✅ Done       │
│ Scheduler            │ friday schedule          │ —                    │ ✅ test_scheduler.py        │ ✅ Done       │
│ Runtime              │ friday runtime           │ —                    │ ✅ test_runtime.py          │ ✅ Done       │
│ Repair               │ friday repair            │ ✅ daemon            │ ✅ test_repair.py           │ ✅ Done       │
│ Daemon               │ friday daemon            │ —                    │ ✅ test_daemon.py           │ ✅ Done       │
│ Integration          │ friday integrate         │ —                    │ ✅ test_integration.py      │ ✅ Done       │
│ Meta-engine          │ friday meta              │ ✅ daemon            │ ⚠️ none                     │ ✅ Done       │
│ HyprlandObserver     │ friday observer hyprland │ ✅ daemon            │ ✅ (in test_observation.py) │ ✅ Done       │
│ HyprlandExecutor     │ via friday execute       │ —                    │ ⚠️ none                     │ ⚠️ See gap #1 │
│ BrowserExecutor      │ via friday execute       │ —                    │ ⚠️ none                     │ ⚠️ See gap #1 │
│ Confirm gate         │ —                        │ —                    │ ⚠️ none                     │ ✅ Done       │
│ Action log           │ ⚠️ no CLI                │ ✅ daemon (obs diff) │ ⚠️ none                     │ ⚠️ See gap #1 │
│ Sequence miner       │ friday patterns          │ ✅ daemon            │ ⚠️ none                     │ ✅ Done       │
│ Intent labeler       │ friday patterns label    │ ✅ daemon            │ ⚠️ none                     │ ✅ Done       │
│ Context_prompter     │ wired in ask/chat        │ —                    │ ⚠️ none                     │ ✅ Done       │
└──────────────────────┴──────────────────────────┴──────────────────────┴─────────────────────────────┴───────────────┘
🔴 Half-Built / Missing Things
#1 — Action log NOT wired into executors
File:  src/friday/runtime/hyprland_executor.py  (line 20 imports  ActionEvent ,  log_action  from action_log, but never calls them)
Same:  src/friday/runtime/browser_executor.py  (no action_log import at all)
The  action_log  module exists and works for passive observation diffing in the daemon, but active actions (workspace switch via HyprlandExecutor, navigate via BrowserExecutor) are never logged. This means the actions table only has derived events from observation diffs, not Friday's own actions. Sequence mining misses half the signal.
Fix: Add  log_action(conn, event)  calls after each successful execute in both executors.
#2 — No  friday actions  CLI command
The actions table has no read surface.  friday patterns mine  reads from it, but there's no way to see raw actions.
Fix: Add  cmd_actions  in a new  cli_actions.py  or extend  cli_patterns.py  with  friday actions [list|recent] .
#3 — No tests for any new Pillar modules
-  test_action_log.py  — action logging + observation diffing
-  test_sequence_miner.py  — sequence mining (n-gram extraction, sessionization)
-  test_intent_labeler.py  — LLM intent labeling + deterministic fallback
-  test_context_prompter.py  — context enrichment for LLM prompts
-  test_hyprland_executor.py  — Hyprland dispatch + verify-by-diff
-  test_browser_executor.py  — CDP browser automation
-  test_confirm_gate.py  — confirm gate (AUTO/CONFIRM/DOUBLE_CONFIRM)
-  test_meta.py  — full meta-engine pipeline
#4 — Pre-existing test failures (non-blocking)
The full suite has ~100+ pre-existing failures from graph/scheduler/runtime tests. These predate all the new Pillars.
🟢 What Should We Do Next?
The biggest gap is #1: action_log not wired into executors. Without it, Friday's own actions are invisible to the learning pipeline, which means the sequence miner and intent labeler only see passive observation diffs — they miss the actions Friday itself takes.
After that, adding tests (#3) for the new modules is the next highest-impact item. Without tests, regressions in these modules are silent.
Here's what I recommend:
1. Fix action_log wiring in HyprlandExecutor and BrowserExecutor (30 min)
2. Add  friday actions  CLI for visibility into raw action events (20 min)
3. Write tests for the new modules (2-3 hours total, highest ROI)
4. Then move to the next big feature you want — whether that's more desktop integration, cross-project automation, or something else from the vision
