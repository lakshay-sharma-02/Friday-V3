---
name: execute
description: Run commands or do hands-on work (build, test, inspect files, git, scripts) and report the result. Use when the operator asks you to run, test, build, check, fix, or do something concrete in a workspace.
---

# Execute

Do the work with your normal tools (Bash, Read, Edit, Write). This skill exists to keep the discipline consistent.

## Rules

- **Ask before destructive** actions: anything that deletes, overwrites user files, pushes to a remote, or runs in production. Wait for explicit approval.
- **Prefer read-only first.** Inspect before you mutate; run the cheapest command that answers the question.
- **Report honestly.** Say what ran, the exit status, and the key output. If it failed, say so — never claim success.
- If the work produces a useful artifact (a report, a diff summary, test output), save it to `~/.friday/v6_vault/outputs/` with a dated filename and link it with `[[outputs/name]]`.
- Log the outcome in your reply; heavy logs stay in `~/.friday/v6_vault/raw/`.

## Related

- `~/.friday/v6_vault/outputs/` — artifacts directory
- `~/.friday/v6_vault/wiki/me.md` — operator's preferred tooling
