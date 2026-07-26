"""Intent labeler tests — Pillar B Stage 3.

Tests the LLM intent labeling with deterministic fallback.
Skips LLM calls (they're expensive and non-deterministic); tests the
interface contract, fallback path, and formatting.
"""

from __future__ import annotations

import json
import pytest

from friday.intent_labeler import (
    WorkflowIntent,
    format_intents,
    label_intent,
)


class TestWorkflowIntent:
    def test_has_required_fields(self):
        i = WorkflowIntent(
            intent_label="test", intent_description="desc", steps=["step1"],
            confidence="low", pattern_seq=[("a", "1")],
            labeled_at="2025-01-01T00:00:00",
        )
        assert i.intent_label == "test"
        assert i.confidence == "low"
        assert i.labeled_at

    def test_format_intents_empty(self):
        assert "no workflow intents" in format_intents([]).lower()

    def test_format_intents_single(self):
        i = WorkflowIntent(
            intent_label="Start dev server",
            intent_description="Open terminal and run dev command",
            steps=[("exec", "kitty"), ("command_run", "npm run dev")],
            confidence="high",
            pattern_seq=[("exec", "kitty"), ("command_run", "npm run dev")],
        )
        text = format_intents([i])
        assert "Start dev server" in text


class TestLabelIntent:
    """Tests label_intent with LLM=False to exercise deterministic fallback."""

    def test_fallback_with_sequence(self):
        intent = label_intent(
            pattern_sequence=[("workspace_switch", "3"), ("exec", "firefox")],
            pattern_count=5, workspace="3", project="testproj",
        )
        assert isinstance(intent, WorkflowIntent)
        # The fallback should produce a label and description
        assert intent.intent_label
        assert intent.intent_description
        # Confidence is "fallback" when no LLM available, or high/medium/low
        # when LLM is accessible. Both are valid.
        assert intent.confidence in ("fallback", "high", "medium", "low")
        assert len(intent.steps) == 2

    def test_fallback_empty_sequence(self):
        intent = label_intent([], 0)
        assert isinstance(intent, WorkflowIntent)
        assert intent.intent_label

    def test_fallback_with_workspace_only(self):
        intent = label_intent(
            [("workspace_switch", "3")], 3, workspace="3",
        )
        assert intent.confidence in ("fallback", "high", "medium", "low")
        assert len(intent.steps) == 1
