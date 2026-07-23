import re

with open("tests/test_insight.py", "r") as f:
    content = f.read()

# Fix mock_llm_insight to use friday instead of src.friday
content = content.replace('"src.friday.', '"friday.')

# Now, for all 14 tests, we need to inject monkeypatch and call a dynamic mock
# Let's create a dynamic mock function.

new_fixture = """
def _mock_llm_for_type(monkeypatch, insight_type: str):
    import json
    response = json.dumps({
        "findings": [{
            "title": "Mock Insight",
            "type": insight_type,
            "statement": "Mock Statement",
            "confidence": "Medium",
        }],
        "workspace_note": None,
    })
    def _call(_, __): return response
    monkeypatch.setattr("friday.services.llm._enabled", lambda: True)
    monkeypatch.setattr("friday.services.llm._call", _call)
    monkeypatch.setattr("friday.insight.engine.llm_enabled", lambda: True)
    monkeypatch.setattr("friday.insight.derivation.llm_enabled", lambda: True)
    monkeypatch.setattr("friday.insight.derivation.llm_call", _call)

"""

# Insert this after mock_llm_insight
idx = content.find("def test_multiple_understandings_reuse")
content = content[:idx] + new_fixture + content[idx:]

# Define mapping from test name to insight type
test_types = {
    "test_convergence": "engineering_convergence",
    "test_divergence": "engineering_divergence",
    "test_opportunity_rust_extraction": "engineering_opportunity",
    "test_risk_commercial_displacing_research": "engineering_risk",
    "test_recommendation_repeated_implementation": "engineering_recommendation",
    "test_blind_spot": "engineering_blind_spot",
    "test_debt": "engineering_debt",
    "test_momentum": "engineering_momentum",
    "test_bottleneck": "engineering_bottleneck",
    "test_focus_single_initiative": "engineering_focus",
    "test_investment_paying_off": "engineering_investment",
    "test_warning_risk_plus_weakness": "engineering_warning",
    "test_breakthrough_emerging_expertise": "engineering_breakthrough",
    "test_efficiency_recurring_pattern": "engineering_efficiency",
    "test_cross_project_reinforcement_wired": "engineering_reuse",
    "test_history_append_only": "engineering_reuse",
    "test_evolution_events_emitted": "engineering_reuse",
    "test_idempotent_rebuild": "engineering_reuse",
    "test_ephemeral_retire_when_conditions_gone": "engineering_reuse",
    "test_ephemeral_reactivate_after_return": "engineering_reuse",
    "test_multi_project_reuse": "engineering_reuse",
    "test_brain_compatibility_provider": "engineering_reuse",
    "test_every_insight_cites_valid_ids": "engineering_reuse",
    "test_no_duplicate_insights": "engineering_reuse",
    "test_no_hallucination_semantic_titles": "engineering_reuse",
}

for test_name, itype in test_types.items():
    # Find the function definition
    pattern = r"def " + test_name + r"\(db(.*?)\):"
    
    # We replace it with: def test_name(db, monkeypatch$1):
    def repl(m):
        args = m.group(1)
        if "monkeypatch" not in args:
            if args:
                new_args = args + ", monkeypatch"
            else:
                new_args = ", monkeypatch"
        else:
            new_args = args
            
        return f"def {test_name}(db{new_args}):\n    _mock_llm_for_type(monkeypatch, '{itype}')"

    content = re.sub(pattern, repl, content)

with open("tests/test_insight.py", "w") as f:
    f.write(content)
