import os

with open("/home/lakshay/Projects/Friday V3/KNOWN_ISSUES.md", "a") as f:
    f.write("\n## 15. Crash in Context and Session commands [PHASE 5]\n\n")
    f.write("Commands `friday context`, `friday context build`, `friday sessions`, and `friday timeline` crash with:\n")
    f.write("`NameError: name 'ContextEngine' is not defined. Did you mean: '_context_engine'?` in `src/friday/cli.py`.\n\n")
    f.write("## 16. Inconsistent wording in `friday graph generate` error message\n\n")
    f.write("When running `friday graph generate \"<goal>\"` for a goal that is not approved, the error message states: `error: Initiative '<goal>' is not approved. Run friday review pending approve <goal> first.` This terminology is confusing because the argument is a Plan/Goal (as seen in `friday plans`), not necessarily an Initiative.\n")
