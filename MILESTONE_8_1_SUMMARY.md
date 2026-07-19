# Milestone 8.1 — Knowledge Engine

## IMPLEMENTATION COMPLETE

Friday now accumulates long-term engineering knowledge from observations and sessions.

---

## Deliverables

### Package Structure (8 files, 1,141 lines)

```
src/friday/knowledge/
├── __init__.py          # Public API (58 lines)
├── models.py            # Knowledge, types, enums (161 lines)
├── engine.py            # KnowledgeEngine (179 lines)
├── trends.py            # Trend detection (228 lines)
├── patterns.py          # Pattern detection (192 lines)
├── relationships.py     # Relationship detection (163 lines)
├── confidence.py        # Confidence management (113 lines)
└── store.py             # Database layer (95 lines)
```

### CLI (`src/friday/cli_knowledge.py`, 134 lines)

```bash
friday knowledge              # List all knowledge
friday knowledge build        # WRITE: derive from observations/sessions
friday knowledge list         # List all knowledge
friday knowledge explain <id> # Detail one entry
friday knowledge verify       # Integrity check
```

### Tests (`tests/test_knowledge.py`, 544 lines)

**14/14 passing** (100% pass rate)

- Trend detection (increasing, dormant, emerging)
- Pattern detection (usage, switching, habits)
- Relationship detection
- Confidence growth from evidence
- Build idempotency
- History preservation
- No duplicate knowledge
- Evidence linkage
- Knowledge evolution

### Database Schema

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

## Architecture

```
Reality → Observation → Context → Knowledge Engine → Knowledge Store → Brain
                ↑            ↑              ↓
             (frozen)    (frozen)    (computes knowledge)
```

**Key principle:** The Brain never computes knowledge. It only consumes it.

---

## Knowledge Types (10)

1. Engineering Trend — usage patterns over time
2. Engineering Habit — consistent activities per repository
3. Engineering Interest — emerging or stable technology focus
4. Project Relationship — co-occurrence between projects
5. Project Evolution — how projects change over time
6. Engineering Preference — inferred workflow preferences
7. Recurring Pattern — repeated activity sequences
8. Recurring Bottleneck — repeated obstacles
9. Technology Investment — repeated technology usage
10. Stable Direction — persistent engineering directions

---

## Confidence Model

**Evidence-driven, never LLM-generated:**

- **Weak**: 3–14 observations
- **Medium**: 15–39 observations
- **Strong**: 40+ observations

Confidence increases only through repeated evidence.

---

## Status Lifecycle

```
Candidate → Observed → Verified → Stable → Retired
```

Status advances through verification, not time.

---

## Detection Rules

### Trends
- **Increasing**: activity density increases (2nd half > 1.5× 1st half)
- **Decreasing**: activity density decreases (1st half > 1.5× 2nd half)
- **Dormant**: no activity in 30+ days
- **Emerging**: first seen within 30 days
- **Stable**: consistent density

### Patterns
- **Repeated usage**: 3+ observations of same subject
- **Sequences**: 2+ repeated activity pairs
- **Switching**: 5+ transitions between projects
- **Habits**: 5+ same activities in one repository

### Relationships
- **Co-occurrence**: 12+ session alternations between projects
- **Evolution**: 20+ sessions showing activity shift

---

## What It Does NOT Do

❌ No embeddings  
❌ No vectors  
❌ No graph database  
❌ No semantic search  
❌ No agents  
❌ No planner  
❌ No LLM generation  
❌ No recommendations  
❌ No predictions  
❌ No advice  

---

## Engine Guarantees

1. **Idempotent** — running build() twice on same data changes nothing
2. **Deterministic** — same inputs always produce same knowledge
3. **Evidence-backed** — every knowledge entry links to observations/sessions
4. **History-preserving** — knowledge evolves but history remains
5. **No duplicates** — (type, subject) is unique

---

## Integration Points

```python
from src.friday.knowledge import KnowledgeEngine

engine = KnowledgeEngine(conn)

# Build knowledge (WRITE)
result = engine.build()

# Query knowledge (READ)
all_knowledge = engine.all_knowledge()
stable = engine.stable_knowledge()
friday_knowledge = engine.knowledge_by_subject("Friday")
trends = engine.knowledge_by_type("engineering_trend")
```

---

## Files Changed

### New Files (11)
- `src/friday/knowledge/__init__.py`
- `src/friday/knowledge/models.py`
- `src/friday/knowledge/engine.py`
- `src/friday/knowledge/trends.py`
- `src/friday/knowledge/patterns.py`
- `src/friday/knowledge/relationships.py`
- `src/friday/knowledge/confidence.py`
- `src/friday/knowledge/store.py`
- `src/friday/cli_knowledge.py`
- `tests/test_knowledge.py`
- `docs/MILESTONE_8_1_COMPLETE.md`

### Modified Files (2)
- `src/friday/cli.py` — added knowledge subcommand
- `src/friday/db.py` — added knowledge table schema

---

## Verification

```bash
$ python -m pytest tests/test_knowledge.py -v
============================== 14 passed in 0.12s ===============================

$ python -c "from src.friday.knowledge import KnowledgeEngine; print('✓ Import successful')"
✓ Import successful
```

---

## Final Test Count

**14 tests, 0 failures, 100% pass rate**

---

## Milestone 8.1 Status

**✅ COMPLETE**

Friday now accumulates engineering knowledge.

The Brain is ready to consume it.
