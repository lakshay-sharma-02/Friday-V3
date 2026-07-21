"""Architecture extraction — deterministic, evidence-backed (Milestone 3).

Each test builds a synthetic repository on disk and asserts that analysis
produces the expected architecture label / components / entry points WITHOUT
relying on git.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from friday.architecture import analyze
from friday.discovery import Repo


def _mk(tmp_path: Path, files: dict[str, str]) -> Repo:
    for rel, txt in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt)
    return Repo(path=tmp_path)


# --- Project structure ------------------------------------------------------


def test_top_level_packages_and_tests(tmp_path):
    repo = _mk(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "src/mypkg/__init__.py": "",
        "src/mypkg/core.py": "def f(): pass\n",
        "tests/test_core.py": "def test_f(): assert True\n",
        "config.toml": "x = 1\n",
    })
    p = analyze(repo.path)
    assert "src/mypkg" in p.packages
    assert "src/mypkg" in p.libraries
    assert any(t.startswith("tests/") for t in p.tests)
    assert "config.toml" in p.config_files


def test_build_manifests_not_treated_as_config(tmp_path):
    repo = _mk(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "package.json": json.dumps({"dependencies": {"react": "18"}}),
    })
    p = analyze(repo.path)
    # pyproject.toml / package.json must NOT be flagged as config loading.
    assert "pyproject.toml" not in p.config_files
    assert "package.json" not in p.config_files


# --- Dependency graph -------------------------------------------------------


def test_dependency_graph_and_external_deps(tmp_path):
    repo = _mk(tmp_path, {
        "src/pkg/__init__.py": "",
        "src/pkg/a.py": "import os\nimport pkg.b\nimport requests\n",
        "src/pkg/b.py": "import pkg.a\n",
    })
    p = analyze(repo.path)
    assert "requests" in p.external_dependencies
    assert "os" not in p.external_dependencies  # stdlib excluded
    edges = {(s, d) for s, d in p.internal_imports}
    assert ("src/pkg/a.py", "pkg.b") in edges
    assert ("src/pkg/b.py", "pkg.a") in edges


def test_circular_dependency_detected(tmp_path):
    repo = _mk(tmp_path, {
        "src/pkg/__init__.py": "",
        "src/pkg/a.py": "import pkg.b\n",
        "src/pkg/b.py": "import pkg.a\n",
    })
    p = analyze(repo.path)
    assert p.circular, "expected a detected circular dependency"


def test_no_false_circular_when_acyclic(tmp_path):
    repo = _mk(tmp_path, {
        "src/pkg/__init__.py": "",
        "src/pkg/a.py": "import pkg.b\n",
        "src/pkg/b.py": "x = 1\n",
    })
    p = analyze(repo.path)
    assert p.circular == []


# --- Architecture patterns --------------------------------------------------


def test_detect_fastapi(tmp_path):
    repo = _mk(tmp_path, {
        "main.py": "from fastapi import FastAPI\nimport uvicorn\napp = FastAPI()\n@app.get('/')\ndef r(): return {}\n",
        "routers/users.py": "from fastapi import APIRouter\nr = APIRouter()\n",
    })
    p = analyze(repo.path)
    assert p.architecture == "FastAPI REST API"
    assert any("FastAPI()" in e for e in p.architecture_evidence)
    assert any(e.kind == "FastAPI app" for e in p.entry_points)


def test_detect_flask(tmp_path):
    repo = _mk(tmp_path, {
        "app.py": "from flask import Flask\napp = Flask(__name__)\n@app.route('/')\ndef i(): return 'x'\n",
    })
    p = analyze(repo.path)
    assert p.architecture == "Flask web app"
    assert any(e.kind == "Flask app" for e in p.entry_points)


def test_detect_next_app_router(tmp_path):
    repo = _mk(tmp_path, {
        "package.json": json.dumps({"dependencies": {"next": "14", "react": "18"}}),
        "app/page.tsx": "export default () => null",
        "app/layout.tsx": "export default () => null",
    })
    p = analyze(repo.path)
    assert p.architecture == "Next.js App Router"


def test_detect_next_pages_router(tmp_path):
    repo = _mk(tmp_path, {
        "package.json": json.dumps({"dependencies": {"next": "14", "react": "18"}}),
        "pages/index.tsx": "export default () => null",
    })
    p = analyze(repo.path)
    assert p.architecture == "Next.js Pages Router"


def test_detect_react_spa(tmp_path):
    repo = _mk(tmp_path, {
        "package.json": json.dumps({"dependencies": {"react": "18"}}),
        "vite.config.ts": "export default {}",
        "src/main.tsx": "console.log(1)",
        "public/index.html": "<html></html>",
    })
    p = analyze(repo.path)
    assert p.architecture == "React SPA"


def test_detect_cargo_workspace(tmp_path):
    repo = _mk(tmp_path, {
        "Cargo.toml": "[workspace]\nmembers = ['a', 'b']\n",
        "a/Cargo.toml": "[package]\nname = 'a'\n",
        "b/Cargo.toml": "[package]\nname = 'b'\n",
        "a/src/main.rs": "fn main() {}",
        "b/src/main.rs": "fn main() {}",
    })
    p = analyze(repo.path)
    assert p.architecture == "Cargo workspace"


def test_detect_cargo_binary(tmp_path):
    repo = _mk(tmp_path, {
        "Cargo.toml": "[package]\nname = 'x'\n[[bin]]\nname = 'x'\npath = 'src/main.rs'\n",
        "src/main.rs": "fn main() { println!(\"hi\"); }",
    })
    p = analyze(repo.path)
    assert p.architecture == "Cargo binary"
    assert any(e.kind == "Cargo binary" for e in p.entry_points)


def test_detect_django(tmp_path):
    repo = _mk(tmp_path, {
        "requirements.txt": "django==5.0\n",
        "settings.py": "DEBUG = True",
        "urls.py": "urlpatterns = []",
        "wsgi.py": "application = {}",
    })
    p = analyze(repo.path)
    assert p.architecture == "Django web app"


def test_detect_cli_tool(tmp_path):
    repo = _mk(tmp_path, {
        "pyproject.toml": "[project]\nname='cli'\n[project.scripts]\nf = 'cli:main'\n",
        "cli.py": "import click\n\ndef main(): pass\n",
    })
    p = analyze(repo.path)
    assert p.architecture == "CLI tool"
    assert any(e.kind == "CLI" for e in p.entry_points)


def test_detect_library(tmp_path):
    repo = _mk(tmp_path, {
        "pyproject.toml": "[project]\nname='lib'\nversion='1'\n",
        "mylib/__init__.py": "",
        "mylib/core.py": "def f(): pass\n",
    })
    p = analyze(repo.path)
    assert p.architecture == "Library"


# --- Components -------------------------------------------------------------


def test_component_discovery_auth_db_config(tmp_path):
    repo = _mk(tmp_path, {
        "main.py": "import fastapi\napp = fastapi.FastAPI()\n@app.get('/')\ndef r(): return {}\n",
        "auth.py": "import jwt\n",
        "db.py": "import sqlalchemy\n",
        "config.py": "import os\nfrom pydantic_settings import BaseSettings\nclass S(BaseSettings):\n    pass\nsettings = S()\n",
        "routers/users.py": "from fastapi import APIRouter\nr = APIRouter()\n",
    })
    p = analyze(repo.path)
    names = {c.name for c in p.components}
    assert "Authentication" in names
    assert "Database" in names          # sqlalchemy -> behavioral DB evidence
    # Configuration requires proof of config *loading*, not just a library import.
    assert "Configuration" in names
    assert any("loader" in c.evidence
               for c in p.components if c.name == "Configuration")
    # Routing now requires actual route modules, not just a FastAPI import.
    assert "Routing" in names
    # Components are Weak evidence (concepts, not implementations).
    assert all(c.strength == "Weak" for c in p.components)


def test_component_evidence_backed(tmp_path):
    repo = _mk(tmp_path, {
        "auth.py": "import jwt\n",
    })
    p = analyze(repo.path)
    auth = next((c for c in p.components if c.name == "Authentication"), None)
    assert auth is not None
    assert "jwt" in auth.evidence


def test_no_false_llm_component_on_main_file(tmp_path):
    # 'main.py' contains the substring 'ai' — must NOT trigger LLM interface.
    repo = _mk(tmp_path, {
        "main.py": "def main():\n    print('hello')\n",
    })
    p = analyze(repo.path)
    names = {c.name for c in p.components}
    assert "LLM interface" not in names


# --- Entry points -----------------------------------------------------------


def test_entry_point_main_and_guard(tmp_path):
    repo = _mk(tmp_path, {
        "cli.py": "def main():\n    pass\n\nif __name__ == '__main__':\n    main()\n",
    })
    p = analyze(repo.path)
    assert any(e.kind == "main()" for e in p.entry_points)


def test_entry_point_shebang_script(tmp_path):
    repo = _mk(tmp_path, {
        "bin/run": "#!/usr/bin/env bash\necho hi\n",
    })
    p = analyze(repo.path)
    # bin/ script is a runtime executable entry point.
    assert any(e.kind == "Executable script" for e in p.entry_points)


def test_scripts_dir_is_utility_not_app_entry(tmp_path):
    # scripts/fix-layouts.sh must be classified Utility script, NOT an app entry.
    repo = _mk(tmp_path, {
        "scripts/fix-layouts.sh": "#!/usr/bin/env bash\necho hi\n",
    })
    p = analyze(repo.path)
    assert any(e.kind == "Utility script" for e in p.entry_points)
    assert not any(e.kind == "Executable script" for e in p.entry_points)


# --- Summary ----------------------------------------------------------------


def test_architecture_summary_fields(tmp_path):
    repo = _mk(tmp_path, {
        "pyproject.toml": "[project]\nname='cli'\n[project.scripts]\nf='cli:main'\n",
        "cli.py": "import click\n\ndef main(): pass\n",
        "src/app/__init__.py": "",
        "src/app/svc.py": "def s(): pass\n",
        "tests/test_app.py": "def test_s(): assert True\n",
    })
    p = analyze(repo.path)
    assert p.complexity
    assert p.complexity.startswith(("Low", "Moderate", "High"))
    # src/ + tests/ separation layering detected.
    assert any("src/" in l for l in p.layering)


def test_analyze_does_not_modify_repo(tmp_path):
    files = {
        "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "pyproject.toml": "[project]\nname='x'\n",
    }
    repo = _mk(tmp_path, files)
    before = {rel: (tmp_path / rel).read_text() for rel in files}
    analyze(repo.path)
    after = {rel: (tmp_path / rel).read_text() for rel in files}
    assert before == after
