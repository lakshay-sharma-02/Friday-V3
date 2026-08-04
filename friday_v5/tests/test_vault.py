"""Vault pure-function tests — no SDK, no model."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday_v5.vault import Vault  # noqa: E402


def make_vault(tmp_path) -> Vault:
    return Vault(tmp_path)


def test_log_appends_and_datestamps(tmp_path):
    v = make_vault(tmp_path)
    p1 = v.log("user", "hello friday")
    p2 = v.log("friday", "hi there")
    assert p1 == p2  # same day file
    text = p1.read_text()
    assert "user" in text and "hello friday" in text
    assert "hi there" in text


def test_note_roundtrip_and_slug(tmp_path):
    v = make_vault(tmp_path)
    p = v.note("My Idea!", "summary\n\nbody")
    assert p.name == "My-Idea.md"
    assert p.read_text().startswith("summary")
    assert v.note_path("[[My Idea!]]") == p


def test_list_wiki_newest_first(tmp_path):
    v = make_vault(tmp_path)
    v.note("a", "x")
    import time
    time.sleep(0.01)
    v.note("b", "y")
    names = [p.name for p in v.list_wiki()]
    assert names[0] == "b.md"


def test_query_finds_across_wiki_and_raw(tmp_path):
    v = make_vault(tmp_path)
    v.note("projects", "friday uses a vault not a database")
    v.log("user", "friday loves coffee")
    hits = v.query("coffee")
    assert any("coffee" in h for h in hits)
    hits = v.query("vault")
    assert any("vault" in h for h in hits)


def test_query_empty_terms_returns_nothing(tmp_path):
    v = make_vault(tmp_path)
    v.note("a", "hello world")
    assert v.query("") == []


def test_links_from():
    v = Vault.__new__(Vault)
    assert v.links_from("see [[me]] and [[people|y]]") == ["me", "people"]
