"""Project Session — persistent context for building and editing projects.

A ``ProjectSession`` tracks:
- The project root path
- Files created/modified during the session
- Conversation history (user requests → actions → results)
- Auto-detected project type (Python, React, CLI tool, etc.)
- A running suggestions board for proactive enhancements

Usage::

    session = ProjectSession.start("/home/user/projects/myapp")
    session.add_file("src/main.py", "print('hello')")
    session.add_exchange("create a CLI app", "created 3 files", "ok")
    session.suggest_enhancements()  # LLM-powered suggestions

Design is self-contained with no new DB tables.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .utils import (
    _strip_code_fences,
    FILE_TYPE_MAP as _FILE_TYPE_MAP,
    PROJECT_SIGNATURES as _PROJECT_SIGNATURES,
    SKIP_DIRS as _SKIP_DIRS,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SESSION_DIR = Path.home() / ".friday" / "sessions"
_SESSION_FILE = "active_session.json"

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class ProjectFile:
    """A file tracked in the project session."""
    path: str           # Relative to project root
    content: str        # Current content (after last write)
    created: bool = False   # True = created by Friday, False = pre-existing


@dataclass
class ProjectExchange:
    """One turn in the project conversation."""
    user_input: str
    action_taken: str       # e.g. "created", "modified", "suggested"
    result: str              # Short summary of what happened
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class EnhancementSuggestion:
    """A proactive enhancement suggestion for the project."""
    title: str
    description: str
    priority: str = "medium"   # high / medium / low
    implemented: bool = False


# ---------------------------------------------------------------------------
# Session Manager
# ---------------------------------------------------------------------------


class ProjectSession:
    """Manages a persistent project working session.

    Thread-safe for single-process use (CLI). Not designed for concurrent
    multi-process access — the session file is read/written atomically.
    """

    def __init__(
        self,
        root_path: str,
        project_type: str | None = None,
        session_id: str | None = None,
    ):
        self.root_path = Path(root_path).resolve()
        self.project_type = project_type or self._detect_project_type()
        self.files: dict[str, ProjectFile] = {}
        self.conversation: list[ProjectExchange] = []
        self.suggestions: list[EnhancementSuggestion] = []
        self.created_at = time.time()
        self._session_id = session_id or f"proj_{int(time.time())}"

        # If the project already exists, index its files.
        if self.root_path.exists():
            self._index_existing_files()

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def created_files(self) -> list[ProjectFile]:
        return [f for f in self.files.values() if f.created]

    # ------------------------------------------------------------------
    # File tracking
    # ------------------------------------------------------------------

    def add_file(self, relative_path: str, content: str, *, created: bool = True) -> None:
        """Track a file in the session.

        ``relative_path`` is relative to ``root_path``.
        """
        rel = relative_path.lstrip("/")
        full = self.root_path / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        self.files[rel] = ProjectFile(path=rel, content=content, created=created)

    def read_file(self, relative_path: str) -> str | None:
        """Read a file from disk (or from session cache if written this session)."""
        rel = relative_path.lstrip("/")
        # Return session-tracked content first (most up-to-date).
        if rel in self.files:
            return self.files[rel].content
        # Fall back to disk.
        full = self.root_path / rel
        if full.exists():
            content = full.read_text(encoding="utf-8")
            self.files[rel] = ProjectFile(path=rel, content=content, created=False)
            return content
        return None

    def modify_file(self, relative_path: str, content: str) -> bool:
        """Modify an existing file. Returns True if the file existed."""
        rel = relative_path.lstrip("/")
        full = self.root_path / rel
        existed = full.exists() or rel in self.files
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        self.files[rel] = ProjectFile(path=rel, content=content, created=not existed)
        return existed

    def get_file_tree(self, max_depth: int = 3) -> str:
        """Return a textual tree of all tracked files."""
        lines = ["Files:"]
        tracked = sorted(self.files.keys())
        for rel in tracked:
            pf = self.files[rel]
            marker = "+" if pf.created else " "
            lines.append(f"  {marker} {rel}")
        if not tracked:
            lines.append("  (no tracked files)")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Conversation tracking
    # ------------------------------------------------------------------

    def add_exchange(self, user_input: str, action_taken: str, result: str) -> None:
        """Record one turn in the project conversation."""
        self.conversation.append(
            ProjectExchange(
                user_input=user_input,
                action_taken=action_taken,
                result=result,
            )
        )

    def get_conversation_context(self, max_turns: int = 10) -> str:
        """Return recent conversation turns as a formatted context string."""
        recent = self.conversation[-max_turns:] if self.conversation else []
        if not recent:
            return "(no prior conversation)"
        lines = ["Recent project conversation:"]
        for ex in recent:
            lines.append(f"  User: {ex.user_input[:80]}")
            lines.append(f"  → {ex.action_taken}: {ex.result[:80]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Enhancement suggestions
    # ------------------------------------------------------------------

    def suggest_enhancements(self, llm_call) -> list[EnhancementSuggestion]:
        """LLM-powered enhancement suggestions based on project state.

        ``llm_call`` is a function ``(system_prompt, user_prompt) -> str | None``
        matching the signature of ``services.llm._call``.
        """
        system = (
            "You are a senior software engineer reviewing a project. "
            "Suggest specific, actionable enhancements that would make the "
            "project more complete, maintainable, or impressive. "
            "Output ONLY valid JSON: "
            '[{"title": "...", "description": "...", "priority": "high|medium|low"}]'
        )
        context = self._build_suggestion_context()
        user = f"Review this project and suggest enhancements:\n\n{context}"
        raw = llm_call(system, user)
        if not raw:
            return self._default_suggestions()

        suggestions = self._parse_suggestions(raw)
        if not suggestions:
            suggestions = self._default_suggestions()

        self.suggestions = suggestions
        return suggestions

    def _build_suggestion_context(self) -> str:
        """Build a prompt context describing the project state."""
        parts = [
            f"Project: {self.root_path}",
            f"Type: {self.project_type or 'unknown'}",
            f"Files ({len(self.files)}):",
        ]
        for rel, pf in sorted(self.files.items()):
            lines = pf.content.count("\n") + 1
            ext = Path(rel).suffix
            lang = _FILE_TYPE_MAP.get(ext, "text")
            parts.append(f"  - {rel}  ({lang}, {lines} lines)")
        parts.append(f"\nPrior actions ({len(self.conversation)}):")
        for ex in self.conversation[-5:]:
            parts.append(f"  - {ex.action_taken}: {ex.result[:100]}")
        return "\n".join(parts)

    def _parse_suggestions(self, raw: str) -> list[EnhancementSuggestion]:
        """Parse JSON suggestions from LLM output."""
        # Strip markdown fences.
        text = raw.strip()
        if "```" in text:
            m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
            if m:
                text = m.group(1).strip()
        try:
            items = json.loads(text)
            if not isinstance(items, list):
                return []
            return [
                EnhancementSuggestion(
                    title=item.get("title", "Unknown"),
                    description=item.get("description", ""),
                    priority=item.get("priority", "medium"),
                )
                for item in items
                if isinstance(item, dict) and item.get("title")
            ]
        except (json.JSONDecodeError, TypeError):
            return []

    def _default_suggestions(self) -> list[EnhancementSuggestion]:
        """Deterministic fallback suggestions based on project type."""
        base = [
            EnhancementSuggestion(
                title="Add a README.md",
                description="Document your project's purpose, setup, and usage.",
                priority="high",
            ),
            EnhancementSuggestion(
                title="Add unit tests",
                description="Cover core functionality with tests.",
                priority="high",
            ),
        ]
        if self.project_type == "python":
            base.append(EnhancementSuggestion(
                title="Add a virtual environment and requirements.txt",
                description="Pin dependencies for reproducible builds.",
                priority="medium",
            ))
        elif self.project_type in ("node", "react"):
            base.append(EnhancementSuggestion(
                title="Add ESLint and Prettier config",
                description="Enforce consistent code style.",
                priority="medium",
            ))
        return base

    # ------------------------------------------------------------------
    # Project type detection
    # ------------------------------------------------------------------

    def _detect_project_type(self) -> str | None:
        """Auto-detect project type from existing files."""
        from .utils import detect_project_type as _detect
        return _detect(self.root_path)

    def _index_existing_files(self) -> None:
        """Index pre-existing files in the project root (up to 3 levels deep)."""
        root = self.root_path
        for f in sorted(root.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(root)
            depth = len(rel.parts)
            if depth > 3:
                continue
            if any(p in _SKIP_DIRS for p in rel.parts):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            self.files[str(rel)] = ProjectFile(path=str(rel), content=content, created=False)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize session to a dict (for JSON persistence)."""
        return {
            "session_id": self._session_id,
            "root_path": str(self.root_path),
            "project_type": self.project_type,
            "created_at": self.created_at,
            "files": {rel: {"path": pf.path, "content": pf.content, "created": pf.created}
                      for rel, pf in self.files.items()},
            "conversation": [
                {"user_input": ex.user_input, "action_taken": ex.action_taken,
                 "result": ex.result, "timestamp": ex.timestamp}
                for ex in self.conversation
            ],
            "suggestions": [
                {"title": s.title, "description": s.description,
                 "priority": s.priority, "implemented": s.implemented}
                for s in self.suggestions
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> ProjectSession:
        """Restore session from a dict."""
        session = cls(
            root_path=data["root_path"],
            project_type=data.get("project_type"),
            session_id=data.get("session_id"),
        )
        session.created_at = data.get("created_at", time.time())
        for rel, fdata in data.get("files", {}).items():
            session.files[rel] = ProjectFile(**fdata)
        for edata in data.get("conversation", []):
            ex = ProjectExchange(**edata)
            session.conversation.append(ex)
        for sdata in data.get("suggestions", []):
            session.suggestions.append(EnhancementSuggestion(**sdata))
        return session

    # ------------------------------------------------------------------
    # Active session management (singleton-style)
    # ------------------------------------------------------------------

    @classmethod
    def start(cls, root_path: str, project_type: str | None = None) -> ProjectSession:
        """Start a new project session and save it as active."""
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        session = cls(root_path=root_path, project_type=project_type)
        session.save_active()
        return session

    @classmethod
    def active(cls) -> Optional[ProjectSession]:
        """Get the currently active session, or None."""
        path = _SESSION_DIR / _SESSION_FILE
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(data)
        except (json.JSONDecodeError, OSError, KeyError):
            return None

    def save_active(self) -> None:
        """Save this session as the active session."""
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        path = _SESSION_DIR / _SESSION_FILE
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def end_active(cls) -> bool:
        """End the active session. Returns True if there was one."""
        path = _SESSION_DIR / _SESSION_FILE
        if path.exists():
            path.unlink()
            return True
        return False

    @classmethod
    def has_active(cls) -> bool:
        """Check if an active session exists."""
        path = _SESSION_DIR / _SESSION_FILE
        return path.exists()


# ---------------------------------------------------------------------------
# Project Templates — predefined project structures
# ---------------------------------------------------------------------------


@dataclass
class TemplateFile:
    """A file definition within a project template."""
    path: str
    content: str = ""
    prompt: str = ""       # LLM prompt to generate content dynamically
    binary: bool = False    # True for binary files (images, etc.)


@dataclass
class ProjectTemplate:
    """A predefined project scaffold template."""
    name: str
    description: str
    project_type: str
    files: list[TemplateFile]
    default_output: str = "myapp"

    def describe(self) -> str:
        """Return a human-readable description of this template."""
        lines = [f"  📦 {self.name}: {self.description}"]
        for f in self.files:
            lines.append(f"    📄 {f.path}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Built-in templates
# ---------------------------------------------------------------------------

_PROJECT_TEMPLATES: dict[str, ProjectTemplate] = {
    "flask": ProjectTemplate(
        name="flask",
        description="Flask web app with routes, templates, and static files",
        project_type="flask",
        default_output="flask-app",
        files=[
            TemplateFile("app.py", prompt="""Create a Flask app.py with:
- Standard Flask setup
- At least 3 routes (home, about, contact)
- JSON API endpoint at /api/status
- Error handlers for 404 and 500
- Configuration from environment variables
- Blueprint structure hint"""),
            TemplateFile("requirements.txt",
                         content="flask>=3.0\npython-dotenv>=1.0\n"),
            TemplateFile("templates/base.html",
                         prompt="Create a base HTML template for a Flask app with: modern CSS, responsive navbar, footer, and content block"),
            TemplateFile("templates/index.html",
                         content="""{% extends "base.html" %}
{% block content %}
<div class="container">
    <h1>Welcome!</h1>
    <p>Your Flask app is running.</p>
</div>
{% endblock %}"""),
            TemplateFile("static/style.css",
                         content="""/* Modern clean styles */
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; }
.container { max-width: 960px; margin: 0 auto; padding: 20px; }
nav { background: #2563eb; color: white; padding: 1rem; }
nav a { color: white; text-decoration: none; margin-right: 1rem; }
.hero { text-align: center; padding: 4rem 0; }
.hero h1 { font-size: 2.5rem; margin-bottom: 1rem; }
footer { text-align: center; padding: 2rem; color: #666; }
"""),
            TemplateFile(".env.example",
                         content="FLASK_APP=app.py\nFLASK_ENV=development\nSECRET_KEY=change-me\nDATABASE_URL=sqlite:///app.db\n"),
            TemplateFile("README.md",
                         prompt="Create a README.md for a Flask web app describing setup, installation, and API endpoints"),
        ],
    ),
    "fastapi": ProjectTemplate(
        name="fastapi",
        description="FastAPI web service with routing, models, and OpenAPI docs",
        project_type="fastapi",
        default_output="fastapi-app",
        files=[
            TemplateFile("main.py", prompt="""Create a FastAPI main.py with:
- FastAPI app setup with metadata
- At least 3 endpoints (GET /, GET /items, POST /items)
- Pydantic models for request/response
- CORS middleware
- Error handling
- Health check endpoint"""),
            TemplateFile("models.py", prompt="Create Pydantic models for a FastAPI app: Item model with id, name, description, price, and tax fields"),
            TemplateFile("requirements.txt",
                         content="fastapi>=0.104.0\nuvicorn[standard]>=0.24.0\npydantic>=2.0\n"),
            TemplateFile(".env.example",
                         content="DATABASE_URL=sqlite:///./app.db\nDEBUG=true\nAPP_NAME=My FastAPI App\n"),
            TemplateFile("README.md",
                         prompt="Create a README.md for a FastAPI web service with setup instructions, API documentation link, and example requests"),
        ],
    ),
    "cli": ProjectTemplate(
        name="cli",
        description="Python CLI tool with argparse, logging, and tests",
        project_type="python",
        default_output="cli-tool",
        files=[
            TemplateFile("cli.py", prompt="""Create a Python CLI tool using argparse with:
- Main parser with subcommands
- At least 3 subcommands (hello, config, version)
- Colored output support (optional)
- Logging setup
- Error handling with meaningful messages
- Type hints throughout"""),
            TemplateFile("core.py", prompt="Create a Python module with core business logic functions for a CLI tool: functions with type hints, docstrings, error handling"),
            TemplateFile("tests/test_cli.py", prompt="Create pytest tests for a CLI tool: test each subcommand, test error cases, test help output"),
            TemplateFile("tests/__init__.py", content=""),
            TemplateFile("requirements.txt",
                         content="# Core dependencies (minimal)\n# Add your dependencies here\n\n# Dev dependencies\npytest>=7.0\n"),
            TemplateFile("setup.py", content="""from setuptools import setup, find_packages

setup(
    name="my-cli-tool",
    version="0.1.0",
    packages=find_packages(),
    py_modules=["cli", "core"],
    install_requires=[],
    entry_points={
        "console_scripts": [
            "mycli=cli:main",
        ],
    },
)
"""),
            TemplateFile("README.md", prompt="Create a README.md for a CLI tool with installation, usage examples, and subcommand documentation"),
        ],
    ),
    "python-package": ProjectTemplate(
        name="python-package",
        description="Python package with src layout, tests, and pyproject.toml",
        project_type="python",
        default_output="mypackage",
        files=[
            TemplateFile("pyproject.toml", content="""[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "mypackage"
version = "0.1.0"
description = "A Python package"
readme = "README.md"
requires-python = ">=3.9"

dependencies = []

[project.optional-dependencies]
dev = [
    "pytest>=7.0",
    "pytest-cov>=4.0",
]
"""),
            TemplateFile("src/mypackage/__init__.py", content='"""MyPackage - A Python package."""\n\n__version__ = "0.1.0"\n\n\ndef hello() -> str:\n    """Return a greeting."""\n    return "Hello from mypackage!"\n'),
            TemplateFile("src/mypackage/core.py", prompt="Create a Python module with core functionality: functions with type hints, docstrings, error handling, logging"),
            TemplateFile("tests/__init__.py", content=""),
            TemplateFile("tests/test_core.py", prompt="Create pytest tests for a Python package: test core functions, edge cases, type hints"),
            TemplateFile("README.md", prompt="Create a README.md for a Python package with installation, usage, API documentation, and development setup"),
        ],
    ),
    "react": ProjectTemplate(
        name="react",
        description="React SPA with components, state management, and API integration",
        project_type="react",
        default_output="react-app",
        files=[
            TemplateFile("package.json", content="""{
  "name": "react-app",
  "version": "0.1.0",
  "private": true,
  "dependencies": {
    "react": "^18.2.0",
    "react-dom": "^18.2.0",
    "react-scripts": "5.0.1"
  },
  "scripts": {
    "start": "react-scripts start",
    "build": "react-scripts build",
    "test": "react-scripts test",
    "eject": "react-scripts eject"
  }
}
"""),
            TemplateFile("public/index.html", content="""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>React App</title>
</head>
<body>
  <div id="root"></div>
</body>
</html>
"""),
            TemplateFile("src/index.js", content="""import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './App.css';

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
"""),
            TemplateFile("src/App.js", prompt="Create a React App.js component with: multiple child components, state management with hooks, API call examples, modern CSS styling"),
            TemplateFile("src/App.css", content=""".App { text-align: center; }
.App-header { background-color: #282c34; min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; font-size: calc(10px + 2vmin); color: white; }
"""),
            TemplateFile("README.md", prompt="Create a README.md for a React app with setup, available scripts, and project structure"),
        ],
    ),
}


def list_templates() -> list[ProjectTemplate]:
    """Return all available project templates."""
    return list(_PROJECT_TEMPLATES.values())


def get_template(name: str) -> ProjectTemplate | None:
    """Get a template by name (case-insensitive)."""
    lower = name.lower()
    for key, tmpl in _PROJECT_TEMPLATES.items():
        if key == lower or tmpl.name.lower() == lower:
            return tmpl
    return None


def scaffold_from_template(
    session: ProjectSession,
    template: ProjectTemplate,
    llm_call,
    project_name: str | None = None,
) -> int:
    """Scaffold a project from a template.

    Uses LLM to generate dynamic file content where prompts are specified.
    Static content (from ``TemplateFile.content``) is written directly.

    Returns the number of files created.
    """
    created = 0
    for tf in template.files:
        if tf.content:
            content = tf.content
        elif tf.prompt:
            system = (
                "You generate code. Output ONLY the file content. "
                "No explanations, no markdown fences, no extra text. "
                "Just the raw code."
            )
            user = tf.prompt
            if project_name:
                user += f"\n\nProject name: {project_name}"
            raw = llm_call(system, user)
            if raw:
                content = _strip_code_fences(raw) if "```" in raw else raw
            else:
                content = f"# {tf.path}\n"
        else:
            content = f"# {tf.path}\n"

        session.add_file(tf.path, content, created=True)
        created += 1

    return created


# ---------------------------------------------------------------------------
# Helper: render suggestions for CLI display
# ---------------------------------------------------------------------------


def format_suggestions(suggestions: list[EnhancementSuggestion]) -> str:
    """Format enhancement suggestions for display."""
    if not suggestions:
        return "  No suggestions available."
    lines = ["Proactive Enhancements:"]
    for i, s in enumerate(suggestions, 1):
        priority_mark = {"high": "🔥", "medium": "💡", "low": "📝"}.get(s.priority, "💡")
        lines.append(f"  {i}. {priority_mark} [{s.priority.upper()}] {s.title}")
        if s.description:
            lines.append(f"     {s.description}")
    return "\n".join(lines)
