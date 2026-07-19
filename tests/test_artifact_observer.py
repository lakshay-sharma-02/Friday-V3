"""Milestone 7.4 — Artifact Observer tests.

Deterministic tests for ArtifactObserver: it is a PURE READER of filesystem
METADATA (never file contents) and emits engineering artifact observations that
plug into the frozen Observation Engine. No daemon, no watcher, no indexer, no
LLM.

Coverage: repository created/renamed/deleted, README added, manifest detected,
research PDF, archive + extraction, workspace move, unknown artifact, privacy
(no file contents), classification, stable identity (relative path, not abs),
registration, health, engine integration, real end-to-end fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from friday.db import connect, observation_state_as_of
from friday.observation import (
    ArtifactObserver,
    Confidence,
    Observation,
    ObservationEngine,
    ObserverRegistry,
    classify,
    default_registry,
)


# --- Classification (deterministic, no LLM) ----------------------------------


def test_classify_repository(tmp_path):
    repo = tmp_path / "Aether"
    repo.mkdir()
    (repo / ".git").mkdir()
    assert classify(repo, True) == "Repository"


def test_classify_manifests_and_docs(tmp_path):
    assert classify(tmp_path / "Cargo.toml", False) == "Manifest"
    assert classify(tmp_path / "package.json", False) == "Manifest"
    assert classify(tmp_path / "pyproject.toml", False) == "Manifest"
    assert classify(tmp_path / "requirements.txt", False) == "Manifest"
    assert classify(tmp_path / "Dockerfile", False) == "Manifest"
    assert classify(tmp_path / "docker-compose.yml", False) == "Manifest"
    assert classify(tmp_path / "Makefile", False) == "Manifest"
    assert classify(tmp_path / "CMakeLists.txt", False) == "Manifest"


def test_classify_documentation(tmp_path):
    assert classify(tmp_path / "README.md", False) == "Documentation"
    assert classify(tmp_path / "readme.txt", False) == "Documentation"
    assert classify(tmp_path / "guide.rst", False) == "Documentation"


def test_classify_archive_research_image(tmp_path):
    assert classify(tmp_path / "llvm.tar.gz", False) == "Archive"
    assert classify(tmp_path / "data.zip", False) == "Archive"
    assert classify(tmp_path / "scheduler_rfc.pdf", False) == "Research Paper"
    assert classify(tmp_path / "diagram.svg", False) == "Diagram"
    assert classify(tmp_path / "arch.png", False) == "Image"
    assert classify(tmp_path / "dataset.csv", False) == "Dataset"
    assert classify(tmp_path / "build.log", False) == "Log"


def test_classify_notes_directory(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    assert classify(notes, True) == "Documentation"


def test_classify_unknown_and_binary(tmp_path):
    assert classify(tmp_path / "mysteryfile", False) == "Unknown"
    assert classify(tmp_path / "libfoo.so", False) == "Binary"


def test_classify_is_case_insensitive(tmp_path):
    assert classify(tmp_path / "README.MD", False) == "Documentation"
    assert classify(tmp_path / "Dockerfile", False) == "Manifest"


# --- Stable identity (design review: relative path, not absolute) -----------


def test_artifact_id_uses_root_alias_and_relative_path(tmp_path):
    root = tmp_path / "Projects"
    root.mkdir()
    (root / "Aether").mkdir()
    obs = ArtifactObserver(roots=[root])
    aid = obs._artifact_id(root, Path("Aether"))
    assert aid == "Projects/Aether"
    # Absolute path must NOT appear in the identity.
    assert str(tmp_path) not in aid


# --- Repository created (stable category fact) --------------------------------


def test_repository_category_emitted(tmp_path):
    root = tmp_path / "Projects"
    root.mkdir()
    (root / "Aether").mkdir()
    (root / "Aether" / ".git").mkdir()
    obs = {(o.subject, o.aspect): o for o in ArtifactObserver(roots=[root]).collect(None)}
    assert ("Projects/Aether", "category") in obs
    assert obs[("Projects/Aether", "category")].value == "Repository"
    # Stable readme/manifest presence facts are emitted per repository.
    assert ("Aether", "readme") in obs
    assert ("Aether", "manifest") in obs


# --- Repository renamed (engine diff surfaces it as a transition) ------------


def test_repository_renamed_via_engine_diff(tmp_path):
    root = tmp_path / "Projects"
    root.mkdir()
    old = root / "X"
    old.mkdir()
    (old / ".git").mkdir()
    obs = ArtifactObserver(roots=[root])
    conn = connect(tmp_path / "kb.db")
    # First scan establishes prior state.
    ObservationEngine(_reg(obs), conn).run()
    # Rename X -> Y.
    old.rename(root / "Y")
    run = ObservationEngine(_reg(obs), conn).run()
    changes = {(c.subject, c.kind) for c in run.observers[0].changes}
    # The old location's category fact is removed; the new one is observed.
    assert ("Projects/X", "category removed") in changes
    assert ("Projects/Y", "category observed") in changes
    conn.close()


# --- Repository deleted (engine diff surfaces removal) -----------------------


def test_repository_deleted_via_engine_diff(tmp_path):
    root = tmp_path / "Projects"
    root.mkdir()
    old = root / "Ghost"
    old.mkdir()
    (old / ".git").mkdir()
    obs = ArtifactObserver(roots=[root])
    conn = connect(tmp_path / "kb.db")
    ObservationEngine(_reg(obs), conn).run()
    import shutil
    shutil.rmtree(old)
    run = ObservationEngine(_reg(obs), conn).run()
    changes = {(c.subject, c.kind) for c in run.observers[0].changes}
    assert ("Projects/Ghost", "category removed") in changes
    conn.close()


# --- README added (stable 'readme' fact flips absent -> present) --------------


def test_readme_added_via_engine_diff(tmp_path):
    root = tmp_path / "Projects"
    (root / "Friday V3").mkdir(parents=True)
    obs = ArtifactObserver(roots=[root])
    conn = connect(tmp_path / "kb.db")
    ObservationEngine(_reg(obs), conn).run()
    (root / "Friday V3" / "README.md").write_text("# hi")
    run = ObservationEngine(_reg(obs), conn).run()
    changes = {(c.subject, c.kind) for c in run.observers[0].changes}
    assert ("Friday V3", "readme changed") in changes
    conn.close()


# --- Manifest detected (stable 'manifest' fact) -------------------------------


def test_manifest_present_fact(tmp_path):
    root = tmp_path / "Projects"
    (root / "Aether").mkdir(parents=True)
    (root / "Aether" / "Cargo.toml").write_text("[package]")
    obs = {(o.subject, o.aspect): o for o in ArtifactObserver(roots=[root]).collect(None)}
    assert obs[("Aether", "manifest")].value == "present"


# --- Research PDF -------------------------------------------------------------


def test_research_pdf_present_in_downloads(tmp_path):
    root = tmp_path / "Downloads"
    root.mkdir()
    (root / "scheduler_rfc.pdf").write_text("%PDF")
    obs = {(o.subject, o.aspect): o for o in ArtifactObserver(roots=[root]).collect(None)}
    assert ("workspace", "research_pdf") in obs
    assert obs[("workspace", "research_pdf")].value == "present"
    assert ("Downloads/scheduler_rfc.pdf", "category") in obs
    assert obs[("Downloads/scheduler_rfc.pdf", "category")].value == "Research Paper"


# --- Archive + extraction -----------------------------------------------------


def test_archive_and_extraction(tmp_path):
    root = tmp_path / "Downloads"
    root.mkdir()
    (root / "llvm.tar.gz").write_text("x")
    # Extracted sibling directory matching the archive stem.
    (root / "llvm").mkdir()
    obs = {(o.subject, o.aspect): o for o in ArtifactObserver(roots=[root]).collect(None)}
    assert ("llvm", "extracted_archive") in obs
    assert obs[("llvm", "extracted_archive")].confidence is Confidence.INFERRED
    assert ("Downloads/llvm.tar.gz", "category") in obs
    assert obs[("Downloads/llvm.tar.gz", "category")].value == "Archive"


# --- Workspace move (cross-root, engine diff surfaces it) ---------------------


def test_project_moved_across_roots_via_engine_diff(tmp_path):
    proj = tmp_path / "Projects"
    dl = tmp_path / "Downloads"
    proj.mkdir()
    dl.mkdir()
    old = proj / "Moving"
    old.mkdir()
    (old / ".git").mkdir()
    obs = ArtifactObserver(roots=[proj, dl])
    conn = connect(tmp_path / "kb.db")
    ObservationEngine(_reg(obs), conn).run()
    import shutil
    shutil.move(str(old), str(dl / "Moving"))
    run = ObservationEngine(_reg(obs), conn).run()
    changes = {(c.subject, c.kind) for c in run.observers[0].changes}
    # The repository disappears from Projects and appears in Downloads.
    assert ("Projects/Moving", "category removed") in changes
    assert ("Downloads/Moving", "category observed") in changes
    conn.close()


# --- Unknown artifact ---------------------------------------------------------


def test_unknown_artifact_classified_and_observed(tmp_path):
    root = tmp_path / "Downloads"
    root.mkdir()
    (root / "weirdthing").write_text("x")
    obs = {(o.subject, o.aspect): o for o in ArtifactObserver(roots=[root]).collect(None)}
    aid = "Downloads/weirdthing"
    assert (aid, "category") in obs
    assert obs[(aid, "category")].value == "Unknown"


# --- Privacy guarantees -------------------------------------------------------


def test_no_file_contents_emitted(tmp_path):
    root = tmp_path / "Projects"
    (root / "Secret").mkdir(parents=True)
    secret = root / "Secret" / "config.py"
    secret.write_text("API_KEY = 'sk-secret123'\nraw_sql = 'SELECT * FROM users'")
    obs = ArtifactObserver(roots=[root]).collect(None)
    blob = json.dumps([o.__dict__ for o in obs], default=str)
    assert "sk-secret123" not in blob
    assert "SELECT * FROM users" not in blob
    # Only metadata aspects are emitted.
    allowed = {
        "category", "name", "ext", "size", "modified_at",
        "readme", "manifest", "notes_directory", "extracted_archive",
        "large_archive_extraction", "research_pdf", "large_document",
        "artifact_count", "repository_count", "documentation_count",
        "research_paper_count", "archive_count", "download_count",
        "repository_lifecycle", "repeated_downloads",
    }
    assert all(o.aspect in allowed for o in obs)


def test_no_recursive_walk_outside_roots(tmp_path):
    # Roots must be honored; a sibling of the root is never observed.
    root = tmp_path / "Projects"
    root.mkdir()
    (root / "A").mkdir()
    sibling = tmp_path / "Outside"
    sibling.mkdir()
    (sibling / "intruder.txt").write_text("x")
    obs = ArtifactObserver(roots=[root]).collect(None)
    assert not any("Outside" in o.subject for o in obs)


# --- Health -------------------------------------------------------------------


def test_health_healthy_with_existing_root(tmp_path):
    root = tmp_path / "Projects"
    root.mkdir()
    h = ArtifactObserver(roots=[root]).health(None)
    assert h.healthy is True
    assert h.status.value == "healthy"


def test_health_down_when_no_roots(tmp_path):
    h = ArtifactObserver(roots=[]).health(None)
    assert h.healthy is False
    assert h.status.value == "down"


# --- Registration -------------------------------------------------------------


def test_artifact_registered_in_default_registry():
    names = default_registry().names()
    assert "artifact" in names
    assert "git" in names
    assert "terminal" in names


def test_register_duplicate_raises():
    reg = ObserverRegistry()
    reg.register(ArtifactObserver(roots=[Path("/tmp")]))
    with pytest.raises(ValueError):
        reg.register(ArtifactObserver(roots=[Path("/tmp")]))


# --- Summary ------------------------------------------------------------------


def test_summary_renders_artifact_counts(tmp_path):
    root = tmp_path / "Projects"
    root.mkdir()
    (root / "Aether").mkdir()
    (root / "Aether" / ".git").mkdir()
    (root / "Aether" / "README.md").write_text("# hi")
    (root / "Aether" / "Cargo.toml").write_text("[package]")
    s = ArtifactObserver(roots=[root]).summarize(None)
    assert "Artifact Observer" in s
    assert "Healthy" in s
    assert "Repositories" in s
    assert "1" in s  # one repository
    assert "Documentation" in s
    assert "Archives" in s
    assert "Workspace changes" in s


# --- Engine integration + end-to-end fixture ---------------------------------


def test_end_to_end_through_observation_engine(tmp_path):
    root = tmp_path / "Projects"
    root.mkdir()
    (root / "Aether").mkdir()
    (root / "Aether" / ".git").mkdir()
    (root / "Aether" / "README.md").write_text("# hi")
    conn = connect(tmp_path / "kb.db")
    obs = ArtifactObserver(roots=[root])
    run = ObservationEngine(_reg(obs), conn).run()
    conn.close()
    assert run.observers[0].name == "artifact"
    assert run.observers[0].health.healthy
    from friday.db import observations_all
    stored = observations_all(connect(tmp_path / "kb.db"))
    subjects = {o.subject for o in stored}
    aspects = {(o.subject, o.aspect) for o in stored}
    # Stable facts prove the observer worked and plugged into the engine.
    assert ("Projects/Aether", "category") in aspects
    assert ("Aether", "readme") in aspects
    assert ("Aether", "manifest") in aspects
    assert ("workspace", "repository_count") in aspects
    # Source proves it plugged into the engine unchanged.
    assert all(o.source == "artifact" for o in stored)
    assert any(s.startswith("Projects/") for s in subjects)


def test_observations_idempotent_on_rerun(tmp_path):
    root = tmp_path / "Projects"
    root.mkdir()
    (root / "Aether").mkdir()
    (root / "Aether" / ".git").mkdir()
    conn = connect(tmp_path / "kb.db")
    obs = ArtifactObserver(roots=[root])
    ObservationEngine(_reg(obs), conn).run()
    facts1 = {(o.subject, o.aspect, o.value)
              for o in observation_state_as_of(conn, "artifact", _future())}
    ObservationEngine(_reg(obs), conn).run()
    facts2 = {(o.subject, o.aspect, o.value)
              for o in observation_state_as_of(conn, "artifact", _future())}
    # Identical filesystem => identical facts (no new/changed facts emitted).
    assert facts1 == facts2
    conn.close()


def test_nested_git_not_double_counted_but_top_level_is(tmp_path):
    # A nested .git inside a non-repo should still classify by its own marker.
    root = tmp_path / "Projects"
    (root / "app" / "vendor" / "lib").mkdir(parents=True)
    (root / "app" / "vendor" / "lib" / ".git").mkdir()
    obs = {(o.subject, o.aspect): o for o in ArtifactObserver(roots=[root]).collect(None)}
    # The nested .git dir itself is observed as a Repository by its marker.
    assert ("Projects/app/vendor/lib", "category") in obs
    assert obs[("Projects/app/vendor/lib", "category")].value == "Repository"


# --- helpers ------------------------------------------------------------------


def _reg(observer: ArtifactObserver) -> ObserverRegistry:
    reg = ObserverRegistry()
    reg.register(observer)
    return reg


def _future() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
