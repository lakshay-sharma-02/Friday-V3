# Milestone 8.1 — Knowledge Engine

## COMPLETE

**Friday now accumulates engineering knowledge from observations and sessions.**

---

## Architecture

```
Reality → Observation → Context → Knowledge Engine → Knowledge Store → Brain
```

The Brain never computes knowledge. It only consumes it.

---

## Package Structure

```
src/friday/knowledge/
├── __init__.py          # Public API
├── models.py            # Knowledge, KnowledgeType, KnowledgeStatus, KnowledgeConfidence
├── engine.py            # KnowledgeEngine (build + queries)
├── trends.py            # Trend detection (increasing/stable/decreasing/dormant/emerging)
├── patterns.py          # Repeated usage, sequences, switching, habits
├── relationships.py     # Project relationships and evolution
├── confidence.py        # Evidence-based confidence updates
└── store.py             # Database layer
```

**8 files, 948 lines of code**

---

## Database Schema

```sql
CREATE TABLE knowledge (
    id                  TEXT PRIMARY KEY,
    type                TEXT NOT NULL,
    subject             TEXT NOT NULL,
    statement           TEXT NOT NULL,
    confidence          TEXT NOT NULL,
    evidence_ids        TEXT NOT NULL,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    last_verified       TEXT,
    verification_count  INTEGER NOT NULL DEFAULT 0
);
```

---

## CLI Commands

```bash
friday knowledge              # List all knowledge
friday knowledge build        # WRITE: derive from observations/sessions
friday knowledge list         # List all knowledge
friday knowledge explain <id> # Detail one entry
friday knowledge verify       # Integrity check
```

---

## Knowledge Types

1. **Engineering Trend** — repository/technology usage patterns over time
2. **Engineering Habit** — consistent activity patterns per repository
3. **Engineering Interest** — emerging or stable technology focus
4. **Project Relationship** — co-occurrence patterns between projects
5. **Project Evolution** — how projects change over time
6. **Engineering Preference** — inferred workflow preferences
7. **Recurring Pattern** — repeated sequences of activities
8. **Recurring Bottleneck** — repeated obstacles
9. **Technology Investment** — repeated technology usage
10. **Stable Direction** — persistent engineering directions

---

## Confidence Model

- **Weak**: 3–14 observations
- **Medium**: 15–39 observations
- **Strong**: 40+ observations

Confidence increases through repeated evidence, never through LLM belief.

---

## Status Lifecycle

```
Candidate → Observed → Verified → Stable → Retired
```

- **Candidate**: Detected but not yet verified
- **Observed**: Sufficient evidence, not yet verified
- **Verified**: Verified at least once
- **Stable**: Strong confidence + verified 3+ times
- **Retired**: No longer active

---

## Detection Rules

### Trends
- Increasing: activity density increases over time
- Decreasing: activity density decreases over time
- Dormant: no activity in 30+ days
- Emerging: first seen within 30 days
- Stable: consistent activity density

### Patterns
- Repeated usage: 3+ observations of same subject
- Activity sequences: 2+ repeated activity pairs
- Project switching: 5+ transitions between projects
- Habits: 5+ occurrences of same activity in one repository

### Relationships
- Co-occurrence: 12+ sessions alternating between projects
- Evolution: 20+ sessions showing activity shift

---

## Tests

**14/14 passing**

1. ✅ Trend detection (increasing)
2. ✅ Trend detection (dormant)
3. ✅ Trend detection (emerging)
4. ✅ Repeated usage detection
5. ✅ Project switching detection
6. ✅ Habit detection
7. ✅ Project relationship detection
8. ✅ Confidence from evidence count
9. ✅ Verification increases status
10. ✅ Build idempotency
11. ✅ History preservation
12. ✅ No duplicate knowledge
13. ✅ Evidence linkage
14. ✅ Knowledge evolution

---

## What It Does NOT Do

❌ No embeddings  
❌ No vectors  
❌ No graph database  
❌ No semantic search  
❌ No agents  
❌ No planner  
❌ No LLM  
❌ No recommendations  
❌ No predictions  
❌ No advice  

---

## Integration with Brain

The Brain can now query knowledge:

```python
from src.friday.knowledge import KnowledgeEngine

engine = KnowledgeEngine(conn)

# All knowledge
knowledge = engine.all_knowledge()

# Only stable, verified knowledge
stable = engine.stable_knowledge()

# By subject (repository)
friday_knowledge = engine.knowledge_by_subject("Friday")

# By type
trends = engine.knowledge_by_type("engineering_trend")
```

Knowledge flows one way: Observations → Context → Knowledge → Brain

---

## Verification

```bash
# Build knowledge from existing observations/sessions
friday knowledge build

# List accumulated knowledge
friday knowledge

# Verify integrity
friday knowledge verify
```

---

## Status

✅ Models defined  
✅ Detection rules implemented  
✅ Confidence management  
✅ Storage layer  
✅ Engine (idempotent, deterministic)  
✅ Database schema  
✅ CLI integration  
✅ 14/14 tests passing  
✅ Documentation complete  

**Milestone 8.1 COMPLETE**
