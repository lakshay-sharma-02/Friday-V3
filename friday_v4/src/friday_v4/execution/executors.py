"""Executors — gated, sandboxed, audited actions (Wave 9).

Wave 9 rule: *"gated, sandboxed, audited executors (shell/git/file/python/
testing) with undo."* Every executor runs the same pipeline:

    classify (gate) → record (audit) → confirm? → sandbox.run → finish (audit)

A single :func:`execute` entry point dispatches on ``action_type`` so
voice/CLI/web all call the same command language (the ONE NLU point,
``nlu.resolve()``, produces these action types).

Every call returns an :class:`ExecutionResult` — never raises (the
daemon law). Denied actions are still recorded in the audit trail.

Usage:
    result = execute("shell", "pytest -q",
                     cwd=Path("."), conn=conn,
                     confirm_fn=lambda d: input(f"{d}? [y/N] ") == "y")
    result.status        # approved | denied | succeeded | failed | timed_out
    result.action_id     # audit row (None when no DB)
    result.undo_payload  # JSON-able dict for UndoManager
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .audit import AuditLogger
from .gate import ConfirmFn, PermissionGate
from .sandbox import Sandbox, SandboxResult, SandboxViolation
from ..security.tooling import find_tool

logger = logging.getLogger("friday_v4.execution.executors")


# ──────────────────────────────────────────────────────────────────────
# Result model
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ExecutionResult:
    """Structured outcome of an executor run — never raises."""

    action_type: str
    status: str = "approved"          # pending|approved|denied|running|succeeded|failed|timed_out
    action_id: Optional[str] = None   # audit row id (None when no DB)
    result_code: Optional[int] = None
    output: str = ""
    undo_payload: dict = field(default_factory=dict)
    permission_level: str = "confirm"
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    def to_dict(self) -> dict:
        return {
            "action_type": self.action_type,
            "status": self.status,
            "action_id": self.action_id,
            "result_code": self.result_code,
            "output": self.output,
            "undo_payload": self.undo_payload,
            "permission_level": self.permission_level,
            "duration_ms": self.duration_ms,
        }


# ──────────────────────────────────────────────────────────────────────
# Executor implementations
# ──────────────────────────────────────────────────────────────────────


class BaseExecutor:
    """Common pipeline: gate → audit → sandbox → audit-finish.

    Subclasses implement :meth:`run` (the sandboxed operation) and set
    ``action_type`` for gate classification.
    """

    action_type = "shell"

    def __init__(self, sandbox: Optional[Sandbox] = None,
                 gate: Optional[PermissionGate] = None) -> None:
        #: An explicit sandbox (tests/embedders) wins; otherwise the
        #: sandbox is rooted at the *execution* cwd in ``execute`` so
        #: containment follows the workspace being acted on.
        self._sandbox_override = sandbox
        self.sandbox = sandbox or Sandbox()
        self.gate = gate or PermissionGate()

    def execute(self, command: str, *,
                cwd: Optional[str | Path] = None,
                conn=None,
                confirm_fn: Optional[ConfirmFn] = None,
                force: bool = False,
                goal: str = "",
                audit: Optional[AuditLogger] = None) -> ExecutionResult:
        """Run ``command`` through the full safety pipeline.

        Args:
            command: The command/payload for this executor type.
            cwd: Working directory (must be inside sandbox roots).
            conn: V4 DB connection for the audit trail (optional).
            confirm_fn: Gate confirmation callback (required for
                CONFIRM-level actions unless ``force`` is set).
            force: Explicit operator override — bypasses CONFIRM and
                NEVER gates. Use sparingly (operator-facing only).
            goal: Human-readable goal for the audit row.
            audit: Override the audit logger (mostly for tests).
        """
        audit = audit or AuditLogger(conn)
        level = self.gate.classify(self.action_type, self._gate_command(command))

        # Root the sandbox at the execution cwd unless an explicit one
        # was provided (containment follows the workspace, not the
        # daemon's process cwd).
        if self._sandbox_override is None:
            root = Path(cwd).resolve() if cwd else Path.cwd()
            self.sandbox = Sandbox(allowed_roots=[root])

        # 1. Record the attempt BEFORE the gate — denied actions are audited.
        action_id = audit.record(
            self.action_type, goal=goal, command=command,
            cwd=str(cwd or ""), permission_level=level.value)

        # 2. Gate.
        description = f"{self.action_type}: {command}" if command else self.action_type
        if not self.gate.check(level, description=description,
                               confirm_fn=confirm_fn, force=force):
            audit.deny(action_id, reason=f"denied by gate ({level.value})")
            return ExecutionResult(
                self.action_type, status="denied", action_id=action_id,
                permission_level=level.value,
                output=f"denied by gate ({level.value}): {description}")

        # 3. Sandboxed run.
        try:
            res: SandboxResult = self.run(command, cwd=cwd)
        except SandboxViolation as exc:
            audit.finish(action_id, "failed", output=str(exc))
            return ExecutionResult(
                self.action_type, status="failed", action_id=action_id,
                permission_level=level.value, output=str(exc))

        # 4. Audit the outcome.
        if res.timed_out:
            status = "timed_out"
        elif res.result_code == 0:
            status = "succeeded"
        else:
            status = "failed"
        # Structured launch failures (missing tool, escaped cwd) carry the
        # detail in ``error``; surface it in the result so callers see why.
        out = res.output or res.error
        undo_payload = self._undo_payload(command, cwd=cwd)
        audit.finish(action_id, status, result_code=res.result_code,
                     output=out, undo_payload=undo_payload)

        return ExecutionResult(
            self.action_type, status=status, action_id=action_id,
            result_code=res.result_code, output=out,
            undo_payload=undo_payload,
            permission_level=level.value, duration_ms=res.duration_ms)

    # ── Subclass hooks ────────────────────────────────────────────────

    def run(self, command: str,
            cwd: Optional[str | Path] = None) -> SandboxResult:
        """The sandboxed operation; returns a :class:`SandboxResult`."""
        raise NotImplementedError

    def _gate_command(self, command: str) -> str:
        """The command string the gate sniffs for dangerous patterns.

        Subclasses with content-bearing payloads (file writes, git
        messages) narrow this so benign *content* doesn't falsely
        escalate to NEVER. Default: the full command.
        """
        return command

    def _undo_payload(self, command: str,
                      cwd: Optional[str | Path] = None) -> dict:
        """Undo payload for a *completed* action. Most executors return
        ``{"op": "none"}`` (no undo); file/git override this."""
        return {"op": "none"}


class ShellExecutor(BaseExecutor):
    """Run a shell command inside the sandbox (argv, never ``shell=True``)."""

    action_type = "shell"

    def run(self, command: str,
            cwd: Optional[str | Path] = None) -> SandboxResult:
        try:
            args = shlex.split(command)
        except ValueError as exc:
            return SandboxResult(error=f"unparseable command: {exc}")
        if not args:
            return SandboxResult(error="empty command")
        return self.sandbox.run(args, cwd=cwd)


class GitExecutor(BaseExecutor):
    """Git operations. Read-only (status/diff/log) classify AUTO; state-
    changing ops need confirmation; push/reset --hard escalate to NEVER
    via command sniffing."""

    action_type = "git"

    def run(self, command: str,
            cwd: Optional[str | Path] = None) -> SandboxResult:
        try:
            args = ["git"] + shlex.split(command)
        except ValueError as exc:
            return SandboxResult(error=f"unparseable command: {exc}")
        return self.sandbox.run(args, cwd=cwd)

    def _gate_command(self, command: str) -> str:
        # Sniff only `git <verb> <target>` — a commit *message* that
        # mentions "push" must not escalate the whole commit to NEVER.
        try:
            parts = shlex.split(command)
        except ValueError:
            return command
        return " ".join(parts[:3]) if parts else command

    def _undo_payload(self, command: str,
                      cwd: Optional[str | Path] = None) -> dict:
        # State-changing git ops are recoverable via reflog; provide the
        # reverse command when we can guess it safely (reset to HEAD~1).
        # The undo must run in the *same repo*, so the execution cwd is
        # carried in the payload. Uses the same narrowed string the gate
        # sniffs, so a commit message mentioning "push" stays undoable.
        low = self._gate_command(command).lower()
        if any(op in low for op in ("push", "reset --hard", "clean -f")):
            return {"op": "none"}  # intentionally not undoable
        if "commit" in low:
            payload = {"op": "run",
                       "args": ["git", "reset", "--soft", "HEAD~1"]}
            if cwd:
                payload["cwd"] = str(cwd)
            return payload
        return {"op": "none"}


class FileExecutor(BaseExecutor):
    """File operations with reversible undo (content + move).

    Undo is *content-preserving*: the pre-write contents are captured
    during ``run()`` and stashed so the undo payload restores the real
    original — never an empty placeholder.
    """

    action_type = "file"

    def __init__(self, sandbox: Optional[Sandbox] = None,
                 gate: Optional[PermissionGate] = None) -> None:
        super().__init__(sandbox=sandbox, gate=gate)
        #: Last completed op's undo payload, computed from the *actual*
        #: on-disk state at write time (original contents captured then).
        self._last_undo_payload: dict = {"op": "none"}

    def run(self, command: str,
            cwd: Optional[str | Path] = None) -> SandboxResult:
        # Command grammar: "<op> <path> [<content>]" where op ∈
        # {read, write, append, delete, move}. Parsed with shlex so
        # paths with spaces survive.
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return SandboxResult(error=f"unparseable command: {exc}")
        if not parts:
            return SandboxResult(error="empty command")
        op = parts[0].lower()

        self._last_undo_payload = {"op": "none"}
        try:
            if op == "read":
                return self._read(parts)
            if op in ("write", "append"):
                return self._write(parts, append=(op == "append"))
            if op == "delete":
                return self._delete(parts)
            if op == "move":
                return self._move(parts)
        except SandboxViolation as exc:
            # Path policy violations bubble up as structured failures
            # (the caller audit-finishes with 'failed').
            raise
        except OSError as exc:
            return SandboxResult(error=f"file op failed: {exc}")
        return SandboxResult(error=f"unknown file op: {op!r}")

    def _resolve(self, parts: list[str], idx: int) -> Path:
        if len(parts) <= idx:
            raise SandboxViolation("missing path operand")
        return self.sandbox.resolve_path(parts[idx])

    def _read(self, parts: list[str]) -> SandboxResult:
        path = self._resolve(parts, 1)
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return SandboxResult(error=f"no such file: {path}")
        return SandboxResult(result_code=0, stdout=content)

    def _write(self, parts: list[str], append: bool) -> SandboxResult:
        path = self._resolve(parts, 1)
        content = " ".join(parts[2:]) if len(parts) > 2 else ""
        mode = "a" if append else "w"

        # Capture the pre-write content BEFORE touching the file so undo
        # can restore exactly what was there.
        original: Optional[str] = None
        if path.exists():
            try:
                original = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.debug(f"file undo capture failed for {path}: {exc}")

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, mode, encoding="utf-8") as fh:
            fh.write(content)

        if append and original is not None:
            # Undo an append by restoring the pre-append content.
            self._last_undo_payload = {
                "op": "restore_file", "path": str(path),
                "original": original, "encoding": "raw",
            }
        elif original is not None:
            # Overwrite: restore the previous content.
            self._last_undo_payload = {
                "op": "restore_file", "path": str(path),
                "original": original, "encoding": "raw",
            }
        else:
            # Brand-new file: undo by deleting what we created.
            self._last_undo_payload = {
                "op": "delete_file", "path": str(path),
            }
        return SandboxResult(result_code=0, stdout=f"wrote {path}")

    def _delete(self, parts: list[str]) -> SandboxResult:
        path = self._resolve(parts, 1)
        if path.exists() and path.is_file():
            try:
                original = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.debug(f"file undo capture failed for {path}: {exc}")
                original = None
            path.unlink()
            if original is not None:
                self._last_undo_payload = {
                    "op": "restore_file", "path": str(path),
                    "original": original, "encoding": "raw",
                }
            return SandboxResult(result_code=0, stdout=f"deleted {path}")
        return SandboxResult(error=f"not a file: {path}")

    def _move(self, parts: list[str]) -> SandboxResult:
        src = self._resolve(parts, 1)
        dst = self._resolve(parts, 2)
        if not src.exists():
            return SandboxResult(error=f"no such file: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        self._last_undo_payload = {
            "op": "move_file", "from": str(dst), "to": str(src),
        }
        return SandboxResult(result_code=0, stdout=f"moved {src} → {dst}")

    def _gate_command(self, command: str) -> str:
        # Sniff only the op + path — file *content* (e.g. a README that
        # mentions "push") must not escalate the write to NEVER.
        try:
            parts = shlex.split(command)
        except ValueError:
            return command
        if len(parts) >= 2:
            return " ".join(parts[:2])
        return command

    def _undo_payload(self, command: str,
                      cwd: Optional[str | Path] = None) -> dict:
        # Computed from the actual write-time state (real original
        # content), not re-parsed from the command string.
        return self._last_undo_payload


class PythonExecutor(BaseExecutor):
    """Run a python snippet or script inside the sandbox."""

    action_type = "python"

    def run(self, command: str,
            cwd: Optional[str | Path] = None) -> SandboxResult:
        if not command.strip():
            return SandboxResult(error="empty python command")
        return self.sandbox.run([sys.executable, "-c", command], cwd=cwd)


class TestingExecutor(BaseExecutor):
    """Run the project test suite (pytest) — venv-aware discovery."""

    action_type = "testing"

    def run(self, command: str,
            cwd: Optional[str | Path] = None) -> SandboxResult:
        pytest = find_tool("pytest")
        if not pytest:
            return SandboxResult(
                error="pytest not found — install dev extra "
                      "or run `pip install pytest`")
        # pytest accepts a path or flags; pass the raw command as args.
        try:
            args = shlex.split(command) if command.strip() else []
        except ValueError as exc:
            return SandboxResult(error=f"unparseable command: {exc}")
        return self.sandbox.run([pytest, *args], cwd=cwd)


class SSHExecutor(BaseExecutor):
    """Run a command on a remote host over SSH (Wave 12 network fold-in).

    The ``network/`` stub's SSH capability slots into the execution layer
    behind the same gate → sandbox → audit pipeline as everything else:
    the remote command is classified (read-only remote commands can be
    AUTO, state-changing CONFIRM, destructive escalate to NEVER via the
    shared command sniffing), run with a bounded timeout inside the
    sandbox, and audited. Remote ops are not locally reversible, so the
    undo payload is always ``{"op": "none"}`` — that is intentional.

    Command grammar: ``<[user@]host> [command...]``

        execute("ssh", "build@10.0.0.5 df -h",
                confirm_fn=..., conn=conn)

    Never raises; a missing ``ssh`` client degrades to a structured
    failure (the daemon law).
    """

    action_type = "ssh"

    def run(self, command: str,
            cwd: Optional[str | Path] = None) -> SandboxResult:
        ssh = find_tool("ssh")
        if not ssh:
            return SandboxResult(
                error="ssh client not found — install openssh-client "
                      "or add ssh to PATH")
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return SandboxResult(error=f"unparseable command: {exc}")
        if not parts:
            return SandboxResult(error="empty ssh command")
        host = parts[0]
        # Everything after the host is the remote command, passed to ssh
        # as ONE argv element (ssh joins argv[2:] with spaces).
        remote = " ".join(parts[1:]) if len(parts) > 1 else ""
        args = [ssh, host] + ([remote] if remote else [])
        return self.sandbox.run(args, cwd=cwd)

    def _undo_payload(self, command: str,
                      cwd: Optional[str | Path] = None) -> dict:
        # Remote ops are not locally reversible — no undo for ssh.
        return {"op": "none"}


# ── Claude Code hands (Wave 18) ─────────────────────────────────────

#: Destructive *task phrases* the gate sniffs in a delegated task's
#: natural-language text. Bare-word patterns like "push"/"deploy" would
#: false-positive on diagnostic requests ("figure out why the push
#: fails") — Claude Code tasks match on imperative phrases instead, so
#: "push my changes to origin" escalates to NEVER while "investigate
#: the deploy failure" stays a confirmable task.
_CLAUDE_DANGEROUS_PHRASES: tuple[str, ...] = (
    "push to", "push my", "push the", "git push",
    "deploy to", "deploy the", "deploy my", "git deploy",
    "drop database", "drop table", "truncate",
    "rm -rf", "git reset --hard", "git clean -f",
    "delete the database", "wipe the", "--force", "--yes",
)


def _claude_config() -> dict:
    """Claude Code CLI config from env (model / tools / timeout).

    ``FRIDAY_V4_CLAUDE_MODEL`` (default ``fable`` — the gateway model
    proven in this workspace; settings.json's ``ANTHROPIC_MODEL`` env is
    broken), ``FRIDAY_V4_CLAUDE_TOOLS`` (the --allowedTools allowlist),
    ``FRIDAY_V4_CLAUDE_TIMEOUT`` (seconds — Claude tasks are agentic
    and take longer than a shell command).

    Note: auth comes from Claude Code's own ``~/.claude/settings.json``
    (this machine). The sandbox strips secret-looking env vars
    (``*TOKEN*``/``*AUTH*``), so a setup that authenticates purely via
    ``ANTHROPIC_AUTH_TOKEN`` env would need that var passed through
    ``Sandbox(allowed_env=...)`` explicitly.
    """
    return {
        "model": os.environ.get("FRIDAY_V4_CLAUDE_MODEL", "fable"),
        "allowed_tools": os.environ.get(
            "FRIDAY_V4_CLAUDE_TOOLS",
            "Bash Read Edit Write Glob Grep"),
        "timeout_seconds": float(os.environ.get(
            "FRIDAY_V4_CLAUDE_TIMEOUT", "600")),
    }


class ClaudeCodeExecutor(BaseExecutor):
    """Delegate a task to the Claude Code CLI (the Wave 18 hands).

    Friday stays the brain — NLU decides *what* to do, the gate
    classifies it, the audit trail records it — and Claude Code (the
    local CLI) does the hands-on work: reads files, runs tests, edits,
    iterates, explains. The command IS the natural-language task:

        claude -p "<task>" --output-format json --model <model> \
               --allowedTools "Bash Read Edit Write Glob Grep"

    Run inside the sandbox (cwd-rooted, timeout-bounded,
    env-sanitized, stdin=/dev/null). The JSON result is parsed back
    into a structured :class:`SandboxResult`. A missing ``claude`` CLI
    degrades to a structured failure — never a crash (daemon law).

    Gate: the task text is classified with phrase-level destructive
    sniffing (see ``_CLAUDE_DANGEROUS_PHRASES``) — "push my changes"
    / "deploy to prod" escalate to NEVER (operator override only);
    everything else stays CONFIRM. The operator's confirmation gates
    the *task*; Claude Code itself gets a conservative ``--allowedTools``
    allowlist so it can't touch the network or anything outside the
    workspace without Friday's say-so.
    """

    action_type = "claude"

    def __init__(self, sandbox: Optional[Sandbox] = None,
                 gate: Optional[PermissionGate] = None,
                 model: Optional[str] = None,
                 allowed_tools: Optional[str] = None,
                 timeout_seconds: Optional[float] = None) -> None:
        gate = gate or PermissionGate(dangerous=_CLAUDE_DANGEROUS_PHRASES)
        super().__init__(sandbox=sandbox, gate=gate)
        cfg = _claude_config()
        self.model = model or cfg["model"]
        self.allowed_tools = allowed_tools or cfg["allowed_tools"]
        self.timeout_seconds = timeout_seconds or cfg["timeout_seconds"]

    def run(self, command: str,
            cwd: Optional[str | Path] = None) -> SandboxResult:
        task = (command or "").strip()
        if not task:
            return SandboxResult(error="empty task for claude executor")
        claude = find_tool("claude")
        if not claude:
            return SandboxResult(
                error="claude CLI not found — install Claude Code "
                      "(npm i -g @anthropic-ai/claude-code) or add it "
                      "to PATH")
        args = [
            claude, "-p", task, "--output-format", "json",
            "--model", self.model,
            "--allowedTools", self.allowed_tools,
        ]
        # Wave 6 — the IDE arms ride with the Claude arms: when
        # FRIDAY_V4_IDE_PREFLIGHT is opted in, Friday's own diagnostics
        # for the workspace/file the task touches are appended to
        # Claude's system prompt, so the delegated agent starts from
        # what Friday already knows (never blocks, never raises).
        ide_ctx = self._ide_context(cwd, task)
        if ide_ctx:
            args += ["--append-system-prompt", ide_ctx]
        res = self.sandbox.run(args, cwd=cwd, timeout=self.timeout_seconds)
        return self._parse_result(res)

    def _ide_context(self, cwd: Optional[str | Path],
                     task: str) -> Optional[str]:
        """IDE diagnostics context for Claude, or None (never raises).

        Explicit opt-in (``FRIDAY_V4_IDE_PREFLIGHT``), bounded (at most
        5 source files), and silent on any failure — Claude delegation
        must never depend on the IDE layer. When the task names a file
        that file is analyzed; otherwise recently-modified source files
        under ``cwd`` are sampled.
        """
        try:
            from ..desktop.ide import analyze_file, preflight_opted_in
            if not preflight_opted_in():
                return None
            root = Path(cwd).resolve() if cwd else Path.cwd()
            if not root.is_dir():
                return None
            targets: list[Path] = []
            m = re.search(r"\b([\w./\\-]+\.\w{1,10})\b", task or "")
            if m:
                named = Path(m.group(1))
                if not named.is_absolute():
                    named = root / named
                if named.is_file():
                    targets.append(named)
            if not targets:
                # Recent source files (mtime, newest first, capped).
                exts = (".py", ".pyi", ".ts", ".tsx", ".js", ".jsx",
                        ".go", ".rs", ".java", ".kt", ".c", ".h",
                        ".cpp", ".hpp", ".cs", ".rb", ".php")
                try:
                    files = sorted(
                        (f for f in root.iterdir()
                         if f.is_file() and f.suffix.lower() in exts),
                        key=lambda f: f.stat().st_mtime, reverse=True)
                    targets = files[:5]
                except OSError:
                    return None
            if not targets:
                return None
            lines: list[str] = []
            for t in targets:
                try:
                    res = analyze_file(t, cwd=root)
                except Exception:
                    continue
                if not res.diagnostics:
                    continue
                first = res.diagnostics[0].brief()
                lines.append(f"{res.display_path}: {first}")
            if not lines:
                return None
            ctx = ("Friday's preflight analysis of this workspace: "
                   + " | ".join(lines[:5])
                   + ". Consider these known issues as you work; do not "
                     "restate them as new findings.")
            return ctx[:1200]
        except Exception as exc:
            logger.debug(f"claude ide context skipped: {exc}")
            return None

    def _parse_result(self, res: SandboxResult) -> SandboxResult:
        """Map the claude CLI's JSON result onto a SandboxResult.

        ``is_error`` (API/model failure), ``error_limit``/``max_turns``
        terminal reasons, and JSON parse failures all become structured
        failures with the CLI's own message; a clean run surfaces the
        assistant's ``result`` text (with a note when some tool calls
        were denied by Claude's permission rules).
        """
        if res.timed_out:
            return res
        if res.error:
            return res
        out = (res.stdout or "").strip()
        if not out:
            return SandboxResult(result_code=1,
                                 error="claude returned no output")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            # Not JSON (e.g. a proxy warning) — surface the raw text.
            return SandboxResult(result_code=res.result_code, stdout=out)
        result_text = str(data.get("result") or "").strip()
        is_error = bool(data.get("is_error"))
        reason = str(data.get("terminal_reason") or "")
        if is_error or reason in ("error_limit", "max_turns"):
            return SandboxResult(
                result_code=1, stdout=result_text or out,
                error=result_text or "claude task failed")
        note = ""
        denials = data.get("permission_denials") or []
        if denials:
            note = (f"\n[note: {len(denials)} tool call(s) were denied "
                    f"by Claude's permission rules]")
        return SandboxResult(result_code=0, stdout=(result_text or out) + note)

    def _undo_payload(self, command: str,
                      cwd: Optional[str | Path] = None) -> dict:
        # Claude Code's edits are complex/unknown — no generic undo.
        # git reflog covers whatever it changed.
        return {"op": "none"}


#: Registry: action_type → executor class.
_EXECUTORS: dict[str, type[BaseExecutor]] = {
    "shell": ShellExecutor,
    "git": GitExecutor,
    "file": FileExecutor,
    "python": PythonExecutor,
    "testing": TestingExecutor,
    "ssh": SSHExecutor,
    "claude": ClaudeCodeExecutor,
}


def register_executor(action_type: str, executor_cls: type[BaseExecutor]) -> None:
    """Register a custom executor (skills/wave-10 layer can extend this)."""
    _EXECUTORS[action_type] = executor_cls


# ──────────────────────────────────────────────────────────────────────
# Dispatcher
# ──────────────────────────────────────────────────────────────────────


def execute(action_type: str, command: str, *,
            cwd: Optional[str | Path] = None,
            conn=None,
            confirm_fn: Optional[ConfirmFn] = None,
            force: bool = False,
            goal: str = "",
            sandbox: Optional[Sandbox] = None,
            audit: Optional[AuditLogger] = None,
            gate: Optional[PermissionGate] = None) -> ExecutionResult:
    """Run ``command`` as ``action_type`` through the full pipeline.

    The single entry point voice/CLI/web will call (via the ONE NLU
    point, ``nlu.resolve()``). Unknown action types fail closed with a
    structured result — never an exception.

    Usage:
        result = execute(
            "testing", "tests/", cwd=Path("."), conn=conn,
            confirm_fn=lambda d: input(f"{d}? [y/N] ").lower() == "y",
            goal="run the test suite")
        if result.ok:
            print(result.output)
        elif result.status == "denied":
            print("operator declined")
    """
    executor_cls = _EXECUTORS.get(action_type)
    if executor_cls is None:
        return ExecutionResult(
            action_type, status="failed",
            output=f"unknown action type: {action_type!r} "
                   f"(known: {sorted(_EXECUTORS)})")
    executor = executor_cls(sandbox=sandbox, gate=gate)
    return executor.execute(command, cwd=cwd, conn=conn,
                            confirm_fn=confirm_fn, force=force,
                            goal=goal, audit=audit)


__all__ = [
    "ExecutionResult",
    "BaseExecutor",
    "ShellExecutor",
    "GitExecutor",
    "FileExecutor",
    "PythonExecutor",
    "TestingExecutor",
    "SSHExecutor",
    "ClaudeCodeExecutor",
    "execute",
    "register_executor",
]
