"""Technology detection — pure, no git required."""

from __future__ import annotations

from pathlib import Path

import pytest

from friday.tech import Detection, detect
from friday.discovery import Repo


def _repo_with(tmp_path: Path, **files: str) -> Repo:
    """Create a temp repo dir with given relative filename -> content files.

    Filenames use underscores in the call but map to real dotted names so the
    detection logic (which looks for 'requirements.txt', 'package.json', etc.)
    works. e.g. requirements_txt -> requirements.txt, docker_compose_yml -> docker-compose.yml.
    """
    for raw, content in files.items():
        name = raw.replace("_", ".")
        if raw == "docker_compose_yml":
            name = "docker-compose.yml"
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return Repo(path=tmp_path)


def _techs(repo: Repo) -> set[str]:
    return {d.tech for d in detect(repo)}


def test_python_and_fastapi(tmp_path):
    techs = _techs(_repo_with(tmp_path, requirements_txt="fastapi\nuvicorn\n"))
    assert "Python" in techs
    assert "FastAPI" in techs


def test_rust_and_cargo(tmp_path):
    techs = _techs(_repo_with(tmp_path, Cargo_toml="[package]\nname='x'\n"))
    assert "Rust" in techs
    assert "Cargo" in techs


def test_next_and_react_and_typescript(tmp_path):
    techs = _techs(
        _repo_with(
            tmp_path,
            package_json='{"dependencies": {"next": "14", "react": "18", "typescript": "5"}}',
        )
    )
    assert {"Next.js", "React", "TypeScript", "Node.js"} <= techs


def test_go(tmp_path):
    assert "Go" in _techs(_repo_with(tmp_path, go_mod="module example.com/x\n"))


def test_docker_with_postgres_and_redis(tmp_path):
    techs = _techs(
        _repo_with(
            tmp_path,
            docker_compose_yml="services:\n  db:\n    image: postgres\n  cache:\n    image: redis\n",
        )
    )
    assert "Docker" in techs
    assert "Postgres" in techs
    assert "Redis" in techs


def test_supabase(tmp_path):
    techs = _techs(_repo_with(tmp_path, package_json='{"dependencies": {"@supabase/supabase-js": "2"}}'))
    assert "Supabase" in techs


def test_sqlite_from_file(tmp_path):
    (tmp_path / "app.db").write_text("")
    assert "SQLite" in _techs(Repo(path=tmp_path))


def test_evidence_recorded(tmp_path):
    dets = detect(_repo_with(tmp_path, requirements_txt="torch\n"))
    torch = next((d for d in dets if d.tech == "PyTorch"), None)
    assert torch is not None
    assert "torch" in torch.evidence


def test_no_false_detection(tmp_path):
    # Empty repo -> no technologies.
    assert _techs(Repo(path=tmp_path)) == set()
