# Knowledge Engine — Milestone 8.1

## Summary

The Knowledge Engine transforms observations and sessions into durable engineering knowledge.

**Status: COMPLETE**

## Architecture

```
Reality
  ↓
Observation (frozen)
  ↓
Context (frozen)
  ↓
Knowledge Engine
  ↓
Knowledge Store
  ↓
Brain
```

The Brain never computes knowledge. It only consumes it.

## Components

### Models (`src/friday/knowledge/models.py`)
- `Knowledge` — one piece of accumulated understanding
- `KnowledgeType` — 10 knowledge categories
- `KnowledgeStatus` — candidate → observed → verified → stable → retired
- `KnowledgeConfidence` — weak / medium / strong (evidence-driven)
- `Trend`, `Relationship` — supporting data structures

### Detection Rules

#### Trends (`src/friday/knowledge/trends.py`)
- Repository usage trends (increasing/decreasing/dormant/emerging)
- Technology trends from observations
- Based entirely on timestamps and density analysis

#### Patterns (`src/friday/knowledge/patterns.py`)
- Repeated technology usage
- Activity sequences
- Project switching patterns
- Engineering habits

#### Relationships (`src/friday/knowledge/relationships.py`)
- Project relationships from co-occurrence
- Project evolution over time
- Inferred relationship strength

### Confidence (`src/friday/knowledge/confidence.py`)
- Confidence increases through repeated evidence
- Never through LLM belief
- 3 observations → Weak
- 15 observations → Medium
- 40 observations → Strong

### Storage (`src/friday/knowledge/store.py`)
- Append-only knowledge table
- History preserved
- Query by type, subject, status, ID

### Engine (`src/friday/knowledge/engine.py`)
- `KnowledgeEngine.build()` — WRITE operation
- Idempotent — running twice changes nothing
- Merges with existing knowledge
- Updates confidence based on new evidence

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

## CLI

### Commands

```bash
friday knowledge              # List all knowledge (default)
friday knowledge build        # WRITE: derive knowledge from observations/sessions
friday knowledge list         # List all knowledge
friday knowledge explain <id> # Show one knowledge entry in detail
friday knowledge verify       # Verify integrity and show statistics
```

### Example Output

```
Engineering Trend (3):

  [✓] Friday (S)
      Friday usage is increasing
      Evidence: 42, Verified: 3x

  [·] OldProject (M)
      OldProject has become dormant
      Evidence: 18, Verified: 1x
```

## Tests

**14/14 tests passing**

### Coverage

1. ✓ Trend detection (increasing/dormant/emerging)
2. ✓ Habit detection
3. ✓ Relationship detection
4. ✓ Confidence growth
5. ✓ Repeated build idempotency
6. ✓ History preservation
7. ✓ No duplicate knowledge
8. ✓ Evidence linkage
9. ✓ Knowledge evolution

## What Knowledge Engine Does NOT Do

- ❌ No embeddings
- ❌ No vectors
- ❌ No graph database
- ❌ No semantic search
- ❌ No agents
- ❌ No planner
- ❌ No LLM
- ❌ No recommendations
- ❌ No autonomous behavior
- ❌ No predictions
- ❌ No advice

## Success Criteria

✅ Knowledge accumulates from observations and sessions  
✅ Evidence-backed (never LLM-generated)  
✅ Idempotent builds  
✅ History preserved  
✅ Confidence increases with evidence  
✅ Status evolves through verification  
✅ CLI provides read/write access  
✅ Deterministic detection rules  
✅ Database schema added  
✅ 14/14 tests passing  

## Integration

The Knowledge Engine is ready for Brain integration. The Brain can now:

1. Query stable knowledge via `KnowledgeEngine.stable_knowledge()`
2. Query by subject via `KnowledgeEngine.knowledge_by_subject(repo)`
3. Query by type via `KnowledgeEngine.knowledge_by_type(type)`

Knowledge never flows backward — the engine consumes observations/sessions but never modifies them.
