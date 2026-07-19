# Friday V3 Dogfooding Report
**Date:** 2026-07-14  
**Test Scope:** Complete command suite with fresh database  
**Database State:** Clean slate (removed `~/.friday/friday.db` before testing)

## Executive Summary

Tested 60+ commands across 6 categories: setup, context, knowledge, engineering queries, trend queries, and meta queries. **Critical finding:** Knowledge engine produces zero knowledge on fresh ingest, causing most queries to return empty or "missing evidence" responses.

---

## Test Setup

### Initial State
```bash
rm -f ~/.friday/friday.db
friday ingest ~/Projects  # Ingested 8 of 8 repositories
friday observe            # No significant workspace changes detected
```

### Database Initialization
- **Repositories ingested:** 8 (Aether, Friday, Friday V2, Friday V3, MindWell, demo-observe, finance-tracker, vivaha)
- **Observation baseline:** 2026-07-14T11:11:13.214202+00:00
- **Initial sessions:** 1 session (0.0 min, Aether, committing)

---

## Category 1: Context Commands

| Command | Expected | Actual | Status | Notes |
|---------|----------|--------|--------|-------|
| `friday context` | Prompt to build | "Engineering context has not been built." | ✅ PASS | Correct guidance |
| `friday context build` | Build sessions | "Built 1 session(s), Created 1 new session(s)" | ✅ PASS | Successfully created context |
| `friday context` (after build) | Show context summary | "Engineering context is out of date... Sessions: 1, Active time: 0.0 min" | ⚠️ WARN | Immediately out of date after build |
| `friday sessions` | List sessions | "2026-07-14T11:11 \| 0m \| committing \| Aether" | ✅ PASS | Session tracked correctly |
| `friday timeline` | Show timeline | "[2026-07-14T11:11] 0m \| committing \| worked on Aether" | ✅ PASS | Timeline rendered |

**Issues:**
1. **Context immediately stale:** After `context build`, running `context` reports "out of date" — suggests timestamp comparison issue
2. **Zero active time:** Session shows 0.0 minutes despite commit activity

---

## Category 2: Knowledge Commands

| Command | Expected | Actual | Status | Notes |
|---------|----------|--------|--------|-------|
| `friday knowledge` | Prompt to build | "No knowledge accumulated yet." | ✅ PASS | Correct guidance |
| `friday knowledge build` | Generate knowledge | "Total knowledge: 0, Created: 0, Candidates: 0" | ❌ FAIL | Built nothing |
| `friday knowledge` (after build) | Show knowledge | "No knowledge accumulated yet." | ❌ FAIL | Still empty |
| `friday knowledge verify` | Verify entries | "No knowledge to verify." | ❌ FAIL | Nothing to verify |
| `friday knowledge explain 1` | Show entry 1 | "error: knowledge not found: 1" | ❌ FAIL | No entry exists |
| `friday knowledge explain 2-5` | Show entries | "error: knowledge not found: N" | ❌ FAIL | No entries exist |

**Critical Issue:**
- **Knowledge engine produces zero output:** Despite successful ingest of 8 repos, `knowledge build` creates 0 knowledge entries
- **Root cause hypothesis:** Likely requires observation history (temporal data) to generate knowledge, but fresh ingest has no history
- **Blocker:** All knowledge-dependent queries fail or return empty results

---

## Category 3: Engineering Knowledge Queries

| Query | Expected Answer | Actual Answer | Status |
|-------|----------------|---------------|--------|
| "What engineering knowledge have you accumulated?" | List of learned patterns | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What stable engineering knowledge do you have?" | Stable patterns | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What have you learned about my engineering?" | Engineering habits/style | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What do you know about my projects now?" | Project summaries | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What long-term engineering trends have you observed?" | Trends over time | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What recurring engineering habits have you learned?" | Repeated decisions | **Partial success:** "you repeatedly choose a shared architecture (MindWell + vivaha: Both are built on React/Supabase)" | ⚠️ PARTIAL |
| "Which technologies am I consistently investing in?" | Tech stack focus | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "How has my engineering direction evolved?" | Evolution analysis | "No observation history... current direction: unknown" | ❌ FAIL |
| "What project relationships have become stronger?" | Project coupling | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "Which knowledge is weakly supported?" | Low-confidence claims | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |

**Success Case:**
- **Habits query:** One query returned actual insight about React/Supabase pattern (based on 5 of 8 repos)
- **Why it worked:** Likely derived from static analysis (README/package.json), not knowledge engine

**Failure Pattern:**
- Most queries report "based on 0 of 8 repositories"
- Missing evidence always includes: "Friday V2: missing detected technologies; demo-observe: missing detected technologies; observation history"
- Suggests technology detection failed for some repos

---

## Category 4: Recent Work Queries

| Query | Expected Answer | Actual Answer | Status |
|-------|----------------|---------------|--------|
| "What am I working on?" | Project themes/purposes | **Success:** Lists themes (AI infrastructure, Developer tooling, etc.) with confidence levels | ✅ PASS |
| "What have I been working on?" | Recent activity | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What have I been building?" | Project purposes | **Success:** Same as "working on" — themes + descriptions | ✅ PASS |
| "What do you know about what I'm building?" | Project overview | **Success:** Same detailed breakdown | ✅ PASS |
| "What engineering knowledge do you have?" | Knowledge summary | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |

**Observation:**
- Queries about **current state** work (themes, purposes from READMEs)
- Queries about **recent activity** fail (require observation history)
- Split between static (works) vs temporal (fails) analysis

---

## Category 5: Trend & Evolution Queries

| Query | Expected Answer | Actual Answer | Status |
|-------|----------------|---------------|--------|
| "How has my engineering changed?" | Evolution analysis | "No observation history... current direction: unknown" | ❌ FAIL |
| "How have my interests evolved?" | Interest drift | "No observation history... current direction: unknown" | ❌ FAIL |
| "Which technologies are becoming more important?" | Tech momentum | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "Which technologies are becoming less important?" | Tech decline | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What trends are strengthening?" | Growing patterns | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What trends are fading?" | Weakening patterns | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What has remained stable?" | Stable patterns | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |

**100% Failure Rate:**
- All evolution/trend queries require temporal data
- Fresh database has no history to compare against
- Need multiple observations over time to detect drift

---

## Category 6: Relationship & Pattern Queries

| Query | Expected Answer | Actual Answer | Status |
|-------|----------------|---------------|--------|
| "Which projects reinforce each other?" | Synergy analysis | "I don't have enough evidence... Try rephrasing, or set FRIDAY_LLM_MODEL" | ❌ FAIL |
| "Which projects depend on each other?" | Dependency graph | "I don't have enough evidence... Try rephrasing, or set FRIDAY_LLM_MODEL" | ❌ FAIL |
| "Which projects influence each other?" | Influence map | "I don't have enough evidence... Try rephrasing, or set FRIDAY_LLM_MODEL" | ❌ FAIL |
| "Which project has become infrastructure?" | Infrastructure role | "I don't have enough evidence... Try rephrasing, or set FRIDAY_LLM_MODEL" | ❌ FAIL |
| "Which projects are converging?" | Convergence detection | **Recommendation:** "Prioritize Friday — tied to 2 other project(s)" | ⚠️ WEIRD |
| "Which projects are diverging?" | Divergence detection | "I don't have enough evidence... Try rephrasing, or set FRIDAY_LLM_MODEL" | ❌ FAIL |

**Issues:**
1. **LLM fallback prompt:** System suggests setting `FRIDAY_LLM_MODEL` for "open-ended questions"
2. **Inconsistent routing:** "converging" returns a recommendation, but "depend on" fails
3. **Query intent mismatch:** User asks about convergence, system recommends priority — not the same question

---

## Category 7: Meta & Habit Queries

| Query | Expected Answer | Actual Answer | Status |
|-------|----------------|---------------|--------|
| "What engineering habits have you learned?" | Recurring decisions | "you repeatedly choose a shared architecture (React/Supabase)" | ✅ PASS |
| "What engineering patterns repeat?" | Pattern list | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What do I consistently do?" | Consistent behaviors | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What workflow keeps repeating?" | Workflow patterns | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What bottlenecks have become recurring?" | Recurring issues | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What engineering strengths keep appearing?" | Strength patterns | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What engineering belief have I abandoned?" | Abandoned patterns | "No repositories are abandoned. Every repo has a commit within 180 days" | ⚠️ MISUNDERSTAND |
| "What mistake do I keep making?" | Recurring mistakes | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What am I avoiding?" | Avoidance patterns | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What surprised you?" | Non-obvious insights | **Success:** "Commercial work is becoming dominant: vivaha carries majority of commits" | ✅ PASS |
| "What changed my mind?" | Belief changes | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What did I learn this month?" | Monthly learning | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "What engineering philosophy do I follow?" | Philosophy summary | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |
| "Who am I becoming as an engineer?" | Identity evolution | "This answer is based on 0 of 8 repositories. Missing evidence..." | ❌ FAIL |

**Successes:**
- **Habits:** Detected React/Supabase pattern
- **Surprises:** Identified vivaha's commit dominance

**Query Misinterpretation:**
- "abandoned belief" → interpreted as "abandoned repository" (semantic mismatch)

---

## Category 8: Self-Explanation Queries

| Query | Expected Answer | Actual Answer | Status |
|-------|----------------|---------------|--------|
| "Explain Friday" | Friday project description | **Returns Friday (not Friday V3):** "Friday Project Guidelines... FastAPI REST API... Python..." | ⚠️ WRONG PROJECT |
| "Explain Friday V3" | Friday V3 description | **Returns same Friday output** (duplicate) | ❌ WRONG |
| "Compare Friday and Friday V3" | Comparison analysis | **Success:** "Different goals (Guidelines vs Observation Test), FastAPI vs CLI, keep separate" | ✅ PASS |

**Issue:**
- Both "Explain Friday" and "Explain Friday V3" return the same output (Friday, not Friday V3)
- Query routing fails to distinguish between project names
- Comparison works correctly, showing they are different

---

## Category 9: Recommendation Queries

| Query | Expected Answer | Actual Answer | Status |
|-------|----------------|---------------|--------|
| "Which project should I continue?" | Priority recommendation | **Success:** "continue Friday V3... uncommitted changes, 8.5 commits/day, newest" | ✅ PASS |
| "What should I work on today?" | Daily recommendation | **Same as above** (Friday V3) | ✅ PASS |
| "Where is my engineering effort going?" | Effort distribution | **Success:** "Friday V3 (~8.5 commits/day), vivaha (~6.9/day), Friday (~4.7/day)" | ✅ PASS |
| "What kind of engineer do I seem to be?" | Engineer profile | **Success:** "AI infrastructure (Strong), Developer tooling (Medium), 9 languages, generalist" | ✅ PASS |
| "Tell me something I haven't noticed." | Non-obvious insight | **Success:** "vivaha carries majority of workspace commits" | ✅ PASS |
| "Which project should become a platform?" | Platform recommendation | **Success:** "Grow MindWell... shares engineering with vivaha" | ✅ PASS |
| "Which projects should eventually merge?" | Merge recommendation | **Success but odd:** "Don't merge Aether by default... scrutinize merge" | ⚠️ WEIRD |

**Observations:**
- Recommendation queries generally work well
- Commit velocity and git status provide sufficient signal
- "Merge" query returns defensive answer instead of direct recommendation

---

## Summary Statistics

| Category | Total Queries | Pass | Partial | Fail | Success Rate |
|----------|--------------|------|---------|------|--------------|
| Context Commands | 5 | 4 | 1 | 0 | 80% |
| Knowledge Commands | 6 | 1 | 0 | 5 | 17% |
| Engineering Knowledge | 10 | 0 | 1 | 9 | 5% |
| Recent Work | 5 | 3 | 0 | 2 | 60% |
| Trends & Evolution | 7 | 0 | 0 | 7 | 0% |
| Relationships | 6 | 0 | 1 | 5 | 8% |
| Meta & Habits | 14 | 2 | 1 | 11 | 14% |
| Self-Explanation | 3 | 1 | 0 | 2 | 33% |
| Recommendations | 7 | 6 | 1 | 0 | 86% |
| **TOTAL** | **63** | **17** | **5** | **41** | **27%** |

---

## Root Cause Analysis

### Issue 1: Knowledge Engine Produces Zero Output
**Symptom:** `friday knowledge build` creates 0 knowledge entries  
**Impact:** 41 queries fail with "based on 0 of 8 repositories"  
**Hypothesis:**
- Knowledge engine requires temporal data (observation history)
- Fresh ingest has no baseline to compare against
- Need multiple observation snapshots to detect patterns

**Test Needed:**
1. Run `friday observe` multiple times over days
2. Re-run `friday knowledge build`
3. Check if knowledge appears

### Issue 2: Technology Detection Failures
**Symptom:** "Friday V2: missing detected technologies; demo-observe: missing detected technologies"  
**Impact:** Queries report incomplete evidence  
**Possible Causes:**
- No package.json/requirements.txt/Cargo.toml in those repos
- Technology detection logic doesn't handle those project structures
- Friday V2 might use virtual env in non-standard location

### Issue 3: Query Routing Issues
**Symptoms:**
- "Explain Friday" returns Friday instead of Friday V3 when run from Friday V3 directory
- "Explain Friday V3" returns same wrong result
- Some relationship queries fail with "not enough evidence" while others succeed

**Hypothesis:**
- Query parser doesn't distinguish project names reliably
- Fallback to LLM not consistently triggered
- Need better intent classification

### Issue 4: Context Immediately Stale
**Symptom:** After `context build`, `context` reports "out of date"  
**Impact:** Confusing UX — build seems to not work  
**Possible Cause:**
- Timestamp comparison uses wrong precision
- Build completes before observation timestamp updates
- Race condition in timestamp recording

### Issue 5: Semantic Mismatches
**Examples:**
- "abandoned belief" → "abandoned repository"
- "Which projects converge?" → "Prioritize Friday" (recommendation, not convergence)

**Issue:** Query intent not correctly mapped to query handlers

---

## What Works Well

1. **Static Analysis Queries:**
   - Project themes/purposes (from READMEs)
   - Commit velocity (from git log)
   - Current work status (from git status)
   - Technology detection (when files exist)

2. **Recommendation Engine:**
   - Priority suggestions based on uncommitted work + velocity
   - Effort distribution analysis
   - Engineer profiling from domains + languages

3. **Partial Pattern Detection:**
   - Architectural patterns (React/Supabase reuse)
   - Commit dominance (vivaha insight)
   - Project relationships (when explicitly stored)

---

## Critical Blockers

### P0: Knowledge Engine Empty
- **60% of queries fail** due to zero knowledge
- Needs temporal data to function
- Documentation should warn: "Knowledge builds over time"

### P1: Technology Detection Gaps
- 2 of 8 repos have "missing detected technologies"
- Reduces evidence base for all queries
- Need investigation into Friday V2 and demo-observe structures

### P2: Query Intent Classification
- Project name disambiguation fails
- Relationship queries inconsistently routed
- Need query taxonomy and routing rules

---

## Recommendations

### Immediate Fixes
1. **Add cold-start detection:** If knowledge count = 0, display: "Knowledge accumulates over time. Run `friday observe` regularly and rebuild in a few days."
2. **Fix context staleness:** Investigate timestamp comparison logic
3. **Debug tech detection:** Why do Friday V2 and demo-observe fail?

### Short-Term Improvements
1. **Better query routing:** Distinguish "Explain X" from "Explain Y" when X ≠ Y
2. **Graceful degradation:** When knowledge = 0, fall back to static analysis with disclaimer
3. **Query examples in help:** Show which queries need history vs work immediately

### Long-Term Design
1. **Temporal data requirements:** Document which features need N days of observation
2. **Knowledge confidence UI:** Show "High/Medium/Low" confidence per answer
3. **Evidence transparency:** Link each claim to specific git commits/files

---

## Testing Gaps

### Not Tested (Requires Real History)
- Knowledge verification over time
- Drift detection (technology/project evolution)
- Pattern strengthening/weakening
- Belief changes

### Not Tested (Requires LLM)
- Open-ended queries with FRIDAY_LLM_MODEL set
- Chat mode interactions
- Conversational follow-ups

### Not Tested (Requires Multiple Users)
- Workspace sharing
- Multi-user observation merging

---

## Conclusion

Friday V3 has a **strong foundation** for static analysis (27% success on fresh database), but **temporal intelligence is completely blocked** by the knowledge engine producing zero output. The system needs:

1. **Time to accumulate data:** Most features require observation history
2. **Better cold-start UX:** Clear messaging about what works now vs later
3. **Query routing fixes:** Better intent classification and project name handling

**Next Steps:**
1. Fix P0 knowledge engine issue (investigate why `knowledge build` creates nothing)
2. Run `friday observe` daily for 1 week, then re-test all failing queries
3. Compare results to validate hypothesis about temporal data requirements
