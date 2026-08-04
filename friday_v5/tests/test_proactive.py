"""Proactive tests — pure, tmp_path, no SDK."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday_v5.proactive import Proactive  # noqa: E402
from friday_v5.vault import Vault  # noqa: E402


def _seed_notice(vault, text="standup at 9am", nid=None):
    vault.notices.mkdir(parents=True, exist_ok=True)
    ts = int(time.time()) if nid is None else nid
    p = vault.notices / f"{ts}-hello.md"
    p.write_text(f"# Notice\n\n- **at**: 2023-11-14T22:13:20\n- **id**: {ts}\n\n{text}\n",
                 encoding="utf-8")
    return p


def test_vault_notices_dir_exists(tmp_path):
    v = Vault(tmp_path)
    assert v.notices.is_dir()


def test_latest_notices_sorted(tmp_path):
    v = Vault(tmp_path)
    _seed_notice(v, "first", nid=1700000000)
    _seed_notice(v, "second", nid=1700000001)
    items = v.latest_notices(5)
    assert len(items) == 2
    assert items[0]["text"] == "second"  # newest first


def test_proactive_detects_new_notice(tmp_path):
    v = Vault(tmp_path)
    p = Proactive(vault_root=tmp_path, interval=0.05)
    got = []
    p.on_notice = got.append
    _seed_notice(v, nid=1700000000)
    seen = p.check()
    assert len(seen) == 1
    assert seen[0]["text"] == "standup at 9am"
    # second check: no new notices
    assert p.check() == []
