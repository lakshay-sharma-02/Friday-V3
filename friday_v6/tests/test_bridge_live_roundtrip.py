"""Live bridge round-trip — sends a CLAUDE: prompt and asserts the reply
lands on the ambient bus. Uses a temp DB (never the real ~/.friday).
Skipped when the SDK/CLI can't run in this environment.
"""

import os
import time

import pytest

os.environ["FRIDAY_V4_DB"] = "/tmp/f6_bridge_roundtrip.db"
import friday_v6.db as db  # noqa: E402


@pytest.mark.skipif(
    os.environ.get("FRIDAY_BRIDGE_LIVE") != "1",
    reason="set FRIDAY_BRIDGE_LIVE=1 to run the live SDK round-trip",
)
def test_bridge_publishes_reply_to_ambient_bus():
    from friday_v6.agent import get_bridge

    try:
        os.unlink("/tmp/f6_bridge_roundtrip.db")
    except OSError:
        pass
    b = get_bridge()
    assert b.available(), "SDK must be installed"
    r = b.send("CLAUDE: reply with the single word ready")
    assert r["ok"]
    deadline = time.time() + 120
    got = None
    while time.time() < deadline:
        time.sleep(5)
        conn = db.connect()
        try:
            for e in db.recent_ambient_events(conn, limit=20):
                if e.get("topic") == "agent":
                    got = str(e.get("payload", ""))
                    break
        finally:
            conn.close()
        if got:
            break
    b.end()
    assert got, "no agent event reached the ambient bus"
    assert "ready" in got.lower(), f"unexpected reply: {got!r}"
