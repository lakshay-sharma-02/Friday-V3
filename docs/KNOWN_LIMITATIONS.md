# Friday Core v1.0 — Known Limitations

**Status:** Production-ready with documented constraints  
**Last Updated:** 2026-07-14  

This document records intentional design boundaries and known limitations of Friday Core v1.0. These are NOT bugs — they are explicit non-goals or deferred features with understood tradeoffs.

---

## 1. Single-Instance Assumption

**Limitation:** Friday assumes one instance per user per workspace.

**Undefined behavior:**
- Running `friday observe` simultaneously from two terminals on the same workspace
- Running Friday on a synced directory (Dropbox, Google Drive, iCloud) with multiple machines writing to the same database
- Multiple users running Friday on the same shared workspace

**Impact:** Observation ID collisions, race conditions in sessions table, partial writes from competing transactions.

**Why this is intentional:** Multi-instance coordination requires distributed locking, conflict resolution, or a central coordination service. These add significant architectural complexity for a use case (collaborative workspaces) that is not a V3 goal.

**Workarounds:**
1. Use separate databases per machine (`FRIDAY_DB` environment variable)
2. Manually coordinate: only one machine runs `friday observe` at a time
3. Use separate workspace directories per user

**Future:** If collaborative/org-scale becomes a requirement, this will need a V4 architecture review (multi-tenant schema, observation deduplication, access control).

---

## 2. Clock Assumptions

**Limitation:** Friday assumes system clock is monotonic and reasonably accurate.

**Undefined behavior:**
- System clock manually set backward (e.g., from 2026-07-14 10:00 to 2026-07-14 09:00)
- Severe NTP failure causing time to jump backward
- Running `friday observe` after clock rewind

**Impact:** `latest_observations()` may return wrong prior state (compares via `MAX(observed_at)`, could get "future" run). Observation diffs will be incorrect.

**Why this is intentional:** Defending against clock skew requires vector clocks or logical timestamps, which add complexity. System clock monotonicity is a reasonable OS-level guarantee for single-machine systems.

**Recovery:** If clock rewinds and produces bad observations:
1. Wait for clock to advance past the future timestamp
2. Manually delete future observations: `DELETE FROM observations WHERE observed_at > '<current_time>'`
3. Re-run `friday observe`

**Future:** Could add clock skew detection (warn if `now() < latest_observation_time`), but not planned for V3.

---

## 3. Observation Growth (No Retention Policy)

**Limitation:** Observations table is append-only with no automatic pruning.

**Growth rate:** ~1000 observations/day typical usage → 365K/year → 3.65M/decade.

**Impact:**
- Unbounded disk growth (SQLite handles millions of rows well, but disk usage grows indefinitely)
- `observation_state_as_of()` query performance degrades over time (though still sub-second for years of data)
- No built-in way to "compact" old observations

**Why retention is deferred:** Observation retention requires:
1. Defining retention policy (keep last N days? aggregate old data into summaries?)
2. Ensuring `as_of` queries remain meaningful after pruning
3. Deciding whether to keep "landmark" observations (project creation, major milestones)
4. Testing that determinism holds after pruning/re-observing

This is **non-trivial design work** best done with real usage patterns. M1-M7 focused on correctness and architecture; retention is an operational concern for M8+.

**Current mitigation:** SQLite handles large tables efficiently. User's dogfooding DB (81,698 observations) performs well.

**Future (M8 Knowledge Growth):** Will design and implement:
- `friday prune observations --keep-days=90` command
- Optional monthly aggregation of old observations
- Explicit documentation of what breaks after pruning

**Workaround (manual):**
```sql
-- Keep last 90 days, delete older
DELETE FROM observations 
WHERE observed_at < date('now', '-90 days');

-- Rebuild sessions after pruning
friday context build
```

---

## 4. No Crash Recovery Guarantees

**Limitation:** If observation/context build crashes mid-write, partial state may persist.

**Current mitigation:**
- **As of v1.0:** BEGIN/COMMIT/ROLLBACK wrappers ensure atomic writes within a transaction
- Observation IDs are deterministic (`observed_at:source:subject:aspect`)
- Session IDs are deterministic (`built_at:primary_repo:start_time`)
- Re-running is idempotent (`INSERT OR REPLACE`)

**Remaining edge cases:**
- Process killed (SIGKILL) before commit → transaction rolled back automatically by SQLite
- System crash during SQLite write → SQLite journal recovery handles this
- Database file corruption (disk failure) → no automatic recovery

**Why this is acceptable:** SQLite's transaction model + deterministic IDs provide strong crash recovery. Re-running `friday observe` or `friday context build` produces correct state.

**Recovery procedure:** Just re-run the command. Idempotency ensures correctness.

**Future:** Could add integrity checks (`friday doctor` command to validate DB state), but not critical for V3.

---

## 5. Scaling Limits (Linear to ~100 Repos)

**Limitation:** Current design assumes ≤100 repositories per workspace.

**Measured performance:**
- 16 repos → observation run ~3s, context build ~1s, ask query ~3s
- 100 repos (projected) → observation ~20s, context ~5s, ask ~15s (acceptable)
- 1000 repos (projected) → observation ~200s, ask ~180s (unacceptable)

**Bottlenecks at 1000+ repos:**
- `all_repositories(conn)` loads all repos into memory
- Portfolio theme analysis iterates ALL repos
- Evidence assembly scans ALL repos for subject matches

**Why this is acceptable for V3:** Target users are individual engineers with 10-50 active projects. 1000-repo scale is org-wide, not individual workspace.

**Future optimization (if needed):**
```python
# DB-level filtering instead of Python iteration
def repositories_matching(conn, subjects: list[str]) -> list[Repository]

# Limit theme analysis to top N active repos
def portfolio_themes(conn, limit: int = 50) -> list[ThemeResult]

# Paginated context queries
def sessions_for_day(conn, day: str, offset: int, limit: int)
```

---

## 6. No Multi-Tenant Support

**Limitation:** Friday V3 is designed for single-user workspaces. No user isolation, no access control, no per-user observation streams.

**Why this is intentional:** Org-scale Friday requires:
- Multi-tenant DB schema (user_id foreign keys everywhere)
- Observation deduplication (same repo observed by multiple users)
- Access control (who can see which repos)
- Authentication/authorization layer
- Shared vs. private knowledge distinction

This is a **major architecture change**, not an incremental feature. V3 explicitly focuses on single-user workspace intelligence.

**Future:** If org-scale becomes a requirement, expect V4 architecture review and redesign.

---

## 7. Observer Ordering (No Guarantees)

**Limitation:** Observer registration order is insertion-order from `default_registry()`, but observers MUST NOT depend on other observers' results from the same run.

**Contract:** Each observer reads ONLY from:
- Database (prior runs' persisted observations)
- Environment (filesystem, git, APIs, logs)

**Why this matters:** If an observer depends on another observer's output from the SAME run, the system becomes order-dependent and non-deterministic.

**Enforcement:** Documented contract in `observation_architecture.md`. No runtime enforcement (observers are trusted).

**Future:** Could add validation that observers don't call `latest_observations()` during their own `collect()` (would read incomplete state), but not critical for V3.

---

## 8. No Distributed/Remote Observation

**Limitation:** All observers run locally. No remote observation agents, no distributed workspace tracking, no cloud-based observation service.

**Why this is intentional:** Remote observation requires:
- Network-based observer protocol
- Authentication/authorization for remote agents
- Handling network failures, timeouts, partial results
- Coordinating observations from multiple machines

V3 is deliberately local-first and deterministic. Remote observation is a future architecture extension.

---

## 9. No Observation Replay/Time-Travel

**Limitation:** Cannot query "what did the workspace look like on 2026-06-01?" beyond what `observation_state_as_of()` provides.

**Current capability:** `observation_state_as_of(conn, source, timestamp)` returns observations current at a given time, but:
- Only works if observations exist at that timestamp
- No "reconstruct workspace state" from observation history
- No "replay engineering sessions from date range"

**Why this is deferred:** Time-travel queries require:
- Observation history guaranteed to be complete (no gaps)
- Retention policy that preserves historical observations
- Query model for "workspace state at time T"

This is valuable for "how did this project evolve?" queries, but requires observation retention to be solved first (see #3).

**Future (post-M8):** Once retention is designed, could add `friday timeline --from=2026-06-01 --to=2026-06-30` for historical analysis.

---

## 10. No Observation Conflict Resolution

**Limitation:** If two observations have the same ID (`observed_at:source:subject:aspect`) in one run, the last write wins (SQLite `INSERT OR REPLACE`).

**When this could happen:**
- Observer bug emits duplicate observations with same (subject, aspect)
- Manual database manipulation

**Why this is acceptable:** Observers are trusted and deterministic. Duplicate (subject, aspect) in one run is a bug, not a normal case.

**Detection:** None currently. Observers should never emit duplicates.

**Future:** Could add validation in `insert_observations()` to detect and fail loudly on duplicates within one run, but not critical for V3.

---

## Summary

These limitations are **intentional design boundaries** for Friday V3. They reflect:

1. **Single-user focus:** No multi-tenant, no distributed coordination
2. **Local-first:** All observation is local, no remote agents
3. **Deferred operational concerns:** Retention, time-travel, large-scale optimizations deferred to M8+
4. **Trust-based:** Observers are trusted, no runtime validation of observer contract

**None of these block production deployment.** They are documented constraints that users can work within.

**Changes require architecture review:** Removing any of these limitations (especially #1, #2, #6) requires significant design work and may warrant a V4 major version.
