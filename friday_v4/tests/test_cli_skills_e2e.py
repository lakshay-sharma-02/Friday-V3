"""End-to-end integration test: `friday4 skills` through the real CLI parser.

Exercises the full shadow-first lifecycle the way an operator would, via
``cli_skills.main()`` (the real argparse parser + command dispatch):

    watch → (work) → watch-stop → shadow ×N → promote → dispatch

- Every command is invoked through the actual parser (``main([...])``),
  not by calling the ``cmd_*`` functions directly.
- The DB is hermetic (``--db <tmp_path>`` on every command).
- Between CLI calls, the test records audited actions directly — in the
  real product that is the execution layer's job; the skills CLI only
  reads the audit log, exactly as production does.

Safety laws verified end-to-end:
- The formed skill starts in shadow with confidence 0 — nothing executes.
- Shadow matches accumulate through real CLI ``shadow`` sweeps, and the
  sweep that crosses the threshold auto-verifies (Wave 14 close-out).
- Promotion requires the operator's explicit ``promote`` step.
- Dispatch is read-only (a suggestion, never an execution).
"""

from __future__ import annotations

from friday_v4 import db
from friday_v4.cli_skills import main as skills_cli


def _record_demo_work(conn, repo: str = "friday_v4") -> None:
    """The operator's demonstrated workflow: pytest then git status."""
    cwd = f"/home/me/{repo}"
    db.record_action(conn, "testing", goal="run tests", command="pytest -q",
                     cwd=cwd, status="succeeded")
    db.record_action(conn, "git", goal="check state", command="git status",
                     cwd=cwd, status="succeeded")


def _record_matching_action(conn) -> None:
    """The operator does the skill's first step again (real work)."""
    db.record_action(conn, "testing", goal="run tests", command="pytest -q",
                     cwd="/home/me/friday_v4", status="succeeded")


class TestSkillsCliEndToEnd:
    def test_watch_learn_promote_dispatch_full_lifecycle(self, tmp_path,
                                                        capsys):
        db_path = tmp_path / "v4.db"

        # 1. watch — tag a window on the audit trail ("watch me do this").
        rc = skills_cli(["watch", "deploy-routine", "--db", str(db_path)])
        assert rc == 0
        conn = db.connect(db_path)
        try:
            assert db.active_watch(conn) is not None
        finally:
            conn.close()

        # 2. The operator works; every action is audited as usual.
        conn = db.connect(db_path)
        try:
            _record_demo_work(conn)
        finally:
            conn.close()

        # 3. watch-stop — form the shadow skill ("learn this").
        rc = skills_cli(["watch-stop", "deploy-routine", "--db", str(db_path)])
        assert rc == 0
        conn = db.connect(db_path)
        try:
            assert db.active_watch(conn) is None
            skill = db.get_skill(conn, "deploy-routine")
            assert skill is not None
            assert skill["verification_state"] == "shadow"
            assert skill["confidence"] == 0.0
            assert skill["shadow_matches"] == 0
            # The watch links the formed skill.
            watch = db.list_watches(conn, status="formed")[0]
            assert watch["skill_id"] == skill["id"]
        finally:
            conn.close()

        # 4. shadow ×N — each sweep records a match for the real workflow;
        #    the sweep crossing the threshold auto-verifies (Wave 14).
        for i in range(3):
            conn = db.connect(db_path)
            try:
                _record_matching_action(conn)
            finally:
                conn.close()
            rc = skills_cli(["shadow", "--db", str(db_path)])
            assert rc == 0
            conn = db.connect(db_path)
            try:
                state = db.get_skill(conn, "deploy-routine")["verification_state"]
                expected = "verified" if i >= 2 else "shadow"
                assert state == expected, f"sweep {i+1} state {state}"
            finally:
                conn.close()

        # 5. promote — the operator-approval step (verified → promoted).
        rc = skills_cli(["promote", "deploy-routine", "--db", str(db_path)])
        assert rc == 0
        conn = db.connect(db_path)
        try:
            assert db.get_skill(conn, "deploy-routine")["verification_state"] == "promoted"
        finally:
            conn.close()

        # 6. dispatch — a matching context suggests the next step, read-only.
        conn = db.connect(db_path)
        try:
            _record_matching_action(conn)
        finally:
            conn.close()
        rc = skills_cli(["dispatch", "--db", str(db_path)])
        assert rc == 0
        out = capsys.readouterr().out
        # The suggestion names the skill and its next step, and is
        # pending approval — the CLI's own output, nothing executed.
        assert "deploy-routine" in out
        assert "git status" in out
        assert "pending approval" in out

    def test_shadow_never_promotes_without_approval(self, tmp_path,
                                                    capsys):
        """Even at the verification threshold, shadow skills are not
        promoted by sweeps — approval is a separate explicit step."""
        db_path = tmp_path / "v4.db"
        skills_cli(["watch", "lint-routine", "--db", str(db_path)])
        conn = db.connect(db_path)
        try:
            _record_demo_work(conn)
        finally:
            conn.close()
        skills_cli(["watch-stop", "lint-routine", "--db", str(db_path)])
        for _ in range(3):
            conn = db.connect(db_path)
            try:
                _record_matching_action(conn)
            finally:
                conn.close()
            skills_cli(["shadow", "--db", str(db_path)])
        conn = db.connect(db_path)
        try:
            skill = db.get_skill(conn, "lint-routine")
            assert skill["verification_state"] == "verified"  # not promoted
        finally:
            conn.close()
        # Dispatch ignores non-promoted skills.
        rc = skills_cli(["dispatch", "--db", str(db_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No promoted skill matches" in out
