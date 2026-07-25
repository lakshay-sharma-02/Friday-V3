"""Meta-Engine — Friday's self-improvement loop.

Sits above Execution. Treats Friday's own codebase as its object of work,
using the same Plan -> TaskGraph -> Execute machinery Friday already uses.

Submodules:
  gap_analyzer    — detect + score capability gaps from runtime_results
  si_planner      — generate Plans for the top-scored gap
  sandbox         — execute self-modifying code in isolated worktree
  verification    — test gates before deploy
  deploy          — human-approved merge into worker registry
  loop            — background meta-loop daemon + insight integration
"""
