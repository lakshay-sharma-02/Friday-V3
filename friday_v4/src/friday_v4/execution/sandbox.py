"""Sandbox — restricted execution environment for Wave 9 executors.

Every subprocess Friday launches passes through a :class:`Sandbox` that
enforces:

- **Path allowlists** — file operations may only touch paths under the
  configured roots (``resolve_path`` raises :class:`SandboxViolation`
  otherwise). This is the primary containment: Friday can only act inside
  its workspace.
- **Timeout** — every run is bounded; a hung subprocess is killed and
  reported as ``timed_out``, never left to hang the daemon.
- **Environment sanitization** — secret-looking env vars (tokens, keys,
  passwords) are stripped from the child environment so a script can't
  exfiltrate them, and callers can pin a minimal env via ``allowed_env``.
- **No stdin** — every child runs with ``stdin=/dev/null`` (Wave 18):
  a sandboxed process never reads the operator's terminal, so a
  delegated Claude Code task can't wait on input. Callers that
  genuinely need stdin pass it explicitly via ``run(..., stdin=...)``.

Design laws (V4): pure stdlib, never crash (all errors become structured
results), hermetic (tests use tmp_path roots).

Usage:
    sb = Sandbox(allowed_roots=[Path.cwd()], timeout_seconds=30)
    path = sb.resolve_path("src/main.py")          # raises on escape
    res = sb.run(["pytest", "-q"], cwd=root)       # SandboxResult
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("friday_v4.execution.sandbox")

#: Environment variable names considered secret (case-insensitive
#: substring match). Stripped from every child environment.
_SECRET_MARKERS = (
    "token", "secret", "password", "passwd", "api_key", "apikey",
    "access_key", "private_key", "client_secret", "auth",
)


class SandboxViolation(Exception):
    """A path or command was rejected by the sandbox (policy, not I/O)."""


@dataclass
class SandboxResult:
    """Structured outcome of one sandboxed run — never raises."""

    result_code: Optional[int] = None   # None when timed out
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    error: str = ""                     # subprocess launch failure detail

    @property
    def output(self) -> str:
        """Combined trimmed output for audit logs."""
        parts = [p.strip() for p in (self.stdout, self.stderr) if p.strip()]
        return "\n".join(parts)


class Sandbox:
    """Path-allowlisted, timeout-bounded subprocess runner."""

    def __init__(self,
                 allowed_roots: Optional[Iterable[Path]] = None,
                 timeout_seconds: float = 120.0,
                 allowed_env: Optional[dict[str, str]] = None) -> None:
        roots = list(allowed_roots) if allowed_roots else [Path.cwd()]
        self.allowed_roots = [r.resolve() for r in roots]
        self.timeout_seconds = timeout_seconds
        self._allowed_env = dict(allowed_env or {})
        if not self.allowed_roots:
            self.allowed_roots = [Path.cwd().resolve()]

    # ── Path policy ───────────────────────────────────────────────────

    def resolve_path(self, path: str | Path) -> Path:
        """Resolve ``path`` relative to the first root, enforcing the
        allowlist. Raises :class:`SandboxViolation` on escape.

        Relative paths are resolved against the first allowed root; the
        result is always absolute and must sit under one of the roots.
        """
        p = Path(path)
        if not p.is_absolute():
            p = self.allowed_roots[0] / p
        resolved = p.resolve()
        if not self._within_roots(resolved):
            raise SandboxViolation(
                f"path {resolved} is outside the sandbox roots "
                f"{[str(r) for r in self.allowed_roots]}")
        return resolved

    def is_allowed(self, path: str | Path) -> bool:
        """Whether ``path`` resolves inside the allowlist (no raise)."""
        try:
            self.resolve_path(path)
            return True
        except SandboxViolation:
            return False

    def _within_roots(self, resolved: Path) -> bool:
        return any(resolved == r or r in resolved.parents
                   for r in self.allowed_roots)

    # ── Env policy ────────────────────────────────────────────────────

    def sanitized_env(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        """A child environment: current env minus secrets, plus ``extra``.

        ``allowed_env`` (constructor) is merged last so callers can pin
        specific values (e.g. ``PYTHONPATH`` for test isolation).
        """
        env = {k: v for k, v in os.environ.items()
               if not _is_secret_var(k)}
        env.update(extra or {})
        env.update(self._allowed_env)
        return env

    # ── Execution ─────────────────────────────────────────────────────

    def run(self, args: list[str] | tuple[str, ...],
            cwd: Optional[str | Path] = None,
            env: Optional[dict[str, str]] = None,
            timeout: Optional[float] = None,
            stdin: Optional[object] = None) -> SandboxResult:
        """Run ``args`` inside the sandbox; returns a :class:`SandboxResult`.

        Never raises (``SandboxViolation`` is only raised by
        :meth:`resolve_path` / path policy). Subprocess launch failures
        and timeouts are reported in the result. ``stdin`` defaults to
        ``/dev/null`` (a sandboxed child never reads the operator's
        terminal — the Claude Code executor relies on this so a
        delegated task doesn't wait on stdin).
        """
        if not args:
            return SandboxResult(error="empty command")
        argv = [str(a) for a in args]

        run_cwd: Optional[str] = None
        if cwd is not None:
            try:
                run_cwd = str(self.resolve_path(cwd))  # must be inside roots
            except SandboxViolation as exc:
                return SandboxResult(error=str(exc))

        start = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=run_cwd,
                env=self.sanitized_env(env),
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else self.timeout_seconds,
                stdin=subprocess.DEVNULL if stdin is None else stdin,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or b""
            err = exc.stderr or b""
            return SandboxResult(
                result_code=None,
                stdout=out.decode(errors="replace") if isinstance(out, bytes) else out,
                stderr=err.decode(errors="replace") if isinstance(err, bytes) else err,
                duration_ms=int((time.monotonic() - start) * 1000),
                timed_out=True,
            )
        except OSError as exc:
            logger.debug(f"sandbox run failed to launch {argv[0]}: {exc}")
            return SandboxResult(
                error=f"failed to launch {argv[0]}: {exc}",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        return SandboxResult(
            result_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_ms=int((time.monotonic() - start) * 1000),
        )


def _is_secret_var(name: str) -> bool:
    low = name.lower()
    return any(marker in low for marker in _SECRET_MARKERS)


__all__ = ["Sandbox", "SandboxResult", "SandboxViolation"]
