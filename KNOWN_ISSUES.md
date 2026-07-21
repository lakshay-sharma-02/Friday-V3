# Known Issues

Found during Recovery & Quality Sprint, Phase 0–5.

## 1. `dogfood_run/` (460K, root dir)

Old dogfood test output (err/out pairs for ~100 CLI runs). Historical record
but not referenced by code. Awaiting user decision: gitignore, archive, or
delete.

## 2. `mission_journal_*.json` not gitignored directly

`.gitignore` now covers `mission_journal_sess:*.json`. The new journal format
uses `mission_journal_sess:<session_id>.json` — same pattern, same gitignore
entry. If the journal filename format changes, the gitignore must follow.

## 3. `friday ask` offline output is plain block-dumps

The deterministic answer path (`_deterministic_answer`) returns evidence blocks
as-is — no prose synthesis, just raw lines. With no LLM configured, answers
are informative but not conversational. This is by design (per REDTEAM_AUDIT.md),
but some users may find it terse. Fix: improve `_deterministic_answer` to
produce basic framing around block output.

## 4. Resolver only checks `worker.availability` for active workers

In `score_worker()`, the availability penalty is only checked after the
`worker.status == "active"` gate. An inactive worker with `availability !=
"available"` is already excluded by the status check. This is fine for now
because `rank_workers()` excludes inactive workers before scoring. But if
the status/availability semantics change, the gate order matters.

## 5. `friday workers` auto-bootstraps but `friday worker list` may still show stale

Workers are auto-bootstrapped when `cmd_workers` runs, but the DB connection
opens a new session each time. Built-in workers with external-availability
state (`worker:claude`, etc.) will show as `available` until `friday capability
discover` updates them. No harm — they self-correct on first discover.

## 6. Smoke test uses `friday` CLI (not `python -m friday`)

No `__main__.py` exists. The entry point is `friday.cli:main`. Run via `friday`
CLI only. Not a bug, just a note in case someone tries `python -m friday`.
