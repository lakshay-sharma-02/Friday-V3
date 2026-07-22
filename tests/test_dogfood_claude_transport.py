"""Milestone 10.1 — Claude Code worker transport regression.

Friday V3 dogfooding surfaced a real invocation bug: Claude Code's
`--print` mode HANGS (times out) when a multiline prompt is passed
as an argv argument in a non-interactive subprocess (no TTY). It only
reads multiline prompts reliably from stdin.

This guard proves the worker transports the prompt via stdin, never argv,
so nobody "optimizes" it back into argv six months from now.
"""

from __future__ import annotations

from friday.runtime.executors import ClaudeCodeWorker


def _fake_task(title="Implement backend logic",
               description="Produce a concrete design with interfaces.",
               acceptance_criteria=(
                   "Change implemented and builds; unit tests green.",
                   "'Implement backend logic' satisfies the milestone.")):
    """Minimal task-shaped object with the fields ClaudeCodeWorker reads."""
    return type("_T", (), {
        "title": title,
        "description": description,
        "acceptance_criteria": list(acceptance_criteria),
    })()


def test_multiline_prompt_uses_stdin_not_argv():
    inv = ClaudeCodeWorker(workspace=".").build_invocation(_fake_task())
    multiline_prompt = inv.stdin or ""

    # Transport: prompt goes through stdin, argv stays minimal
    # (argv[0] is the PATH-resolved binary, not the bare name).
    assert inv.argv[0].endswith("claude"), inv.argv
    assert inv.argv[1:] == ["--print", "--output-format", "json",
                             "--dangerously-skip-permissions", 
                             "--model", "oc/deepseek-v4-flash-free"], \
        inv.argv
    assert inv.stdin is not None and inv.stdin.strip(), "prompt must be on stdin"

    # The multiline prompt must NOT appear in argv — claude --print
    # hangs headless when it must seek file/tool permission approval.
    argv_blob = " ".join(inv.argv)
    assert multiline_prompt not in argv_blob, (
        "multiline prompt leaked into argv — claude --print hangs on this")
    # Headless file-writing tasks need permission/trust bypass; the
    # documented --print + skip-permissions combo is what runs
    # unattended. Mistaking codex's flag here regresses to a hang.
    assert "--dangerously-skip-permissions" in inv.argv, \
        "headless file tasks need --dangerously-skip-permissions"

    # Sanity: the composed prompt is complete (title + criteria present).
    assert "# Implement backend logic" in multiline_prompt
    assert "unit tests green." in multiline_prompt
