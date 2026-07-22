import subprocess
import os
import sys
import time

out_file = "FRIDAY_E2E_TEST.md"

env = os.environ.copy()
env["PYTHONPATH"] = "src"
env["FRIDAY_LLM_MODEL"] = "gemini/gemini-2.5-flash"

def run_cmd(cmd_list, label=None, expected_fail=False):
    cmd_str = "friday " + " ".join([f'"{c}"' if " " in c or ":" in c else c for c in cmd_list])
    if label:
        cmd_str = label
    
    print(f"Running {cmd_str}")
    full_cmd = ["python", "-m", "friday.cli"] + cmd_list
    
    try:
        res = subprocess.run(full_cmd, capture_output=True, text=True, env=env, timeout=300)
        output = res.stdout
        if res.stderr:
            output += "\n--- STDERR ---\n" + res.stderr
        
        if res.returncode == 0:
            status = "worked as expected"
        else:
            status = "expected failure" if expected_fail else f"broken (exit code {res.returncode})"
            
        with open(out_file, "a") as f:
            f.write(f"## `{cmd_str}`\n\n")
            f.write("```\n" + output + "\n```\n")
            f.write(f"**Note:** {status}\n\n")
            
        return output
    except Exception as e:
        with open(out_file, "a") as f:
            f.write(f"## `{cmd_str}`\n\n")
            f.write(f"**Error:** {e}\n\n")
            f.write("**Note:** broken (exception)\n\n")
        return ""

# Initialize file
with open(out_file, "w") as f:
    f.write("# Friday-V3 — End-to-End Full System Test\n\n")

# Reset initiatives for a clean slate
print("Resetting initiatives...")
subprocess.run(["python", "reset_all_initiatives.py"], env=env)

# 1. Foundation
with open(out_file, "a") as f: f.write("# 1. Foundation\n\n")
run_cmd(["ingest"], expected_fail=True)  # Missing path
run_cmd(["observe", "non_existent_repo"], expected_fail=True) # Bad repo
run_cmd(["ingest", "."])
run_cmd(["observe"])
run_cmd(["knowledge", "build"])
run_cmd(["understanding", "build"])
run_cmd(["initiatives", "build"])
run_cmd(["insights", "build"])

# 1.5 Run watch loop to populate pending_initiatives
with open(out_file, "a") as f: f.write("# 1.5 Background Loops\n\n")
run_cmd(["watch", "--run-once"])

# 2. Read/query surface
with open(out_file, "a") as f: f.write("# 2. Read/query surface\n\n")
run_cmd(["summary"])
run_cmd(["ask"], expected_fail=True) # Missing question
run_cmd(["ask", "What are the common technologies used across the workspace?"])
run_cmd(["identity"])
run_cmd(["portfolio"])
run_cmd(["strategy"])
run_cmd(["audit"])
run_cmd(["observers"])
run_cmd(["observer"], expected_fail=True) # Missing observer
run_cmd(["observer", "git"])
run_cmd(["observer", "fake_observer"], expected_fail=True)

# 3. Review and approval
with open(out_file, "a") as f: f.write("# 3. Review and approval\n\n")
run_cmd(["review", "pending"])
run_cmd(["review", "pending", "fake_id_123"], expected_fail=True)

# Issue 17 compliance: hardcoded IDs only — no dynamic "grab the first match".
# These are deterministic IDs produced by the initiative pipeline. If they
# don't exist the test fails loudly rather than silently approving wrong data.
INITIATIVE_ID = "platform:Engineering Platform"
GRAPH_SHORT_ID = "platform_Engineering_Platform"

# Verify the initiative actually exists before touching anything.
out_detail = run_cmd(["review", "pending", INITIATIVE_ID])
if "error:" in out_detail.lower() or "not found" in out_detail.lower():
    msg = f"FATAL: Expected initiative '{INITIATIVE_ID}' not found in pending queue."
    print(msg)
    with open(out_file, "a") as f: f.write(f"**{msg}**\n\n")
    sys.exit(1)

run_cmd(["review", "pending", "approve", INITIATIVE_ID])
run_cmd(["graph", "generate", INITIATIVE_ID])

# Graph proposal review — same hardcoded-ID discipline.
out_graphs = run_cmd(["graph", "review"])

# Verify the graph proposal exists.
out_proposal = run_cmd(["graph", "review", GRAPH_SHORT_ID])
if "error:" in out_proposal.lower() or "not found" in out_proposal.lower():
    msg = f"FATAL: Expected graph proposal '{GRAPH_SHORT_ID}' not found."
    print(msg)
    with open(out_file, "a") as f: f.write(f"**{msg}**\n\n")
    sys.exit(1)

run_cmd(["graph", "review", "approve", GRAPH_SHORT_ID])

# 4. Resolution/scheduling
with open(out_file, "a") as f: f.write("# 4. Resolution/scheduling\n\n")
run_cmd(["resolve"], expected_fail=True)
# Use the same hardcoded initiative ID as section 3.
run_cmd(["resolve", INITIATIVE_ID])
run_cmd(["resolver"])
run_cmd(["schedule"], expected_fail=True)
run_cmd(["schedule", INITIATIVE_ID])
run_cmd(["scheduler"])
run_cmd(["workers"])
run_cmd(["worker", "list"])
run_cmd(["capability", "list"])

# 5. Ambient loop
with open(out_file, "a") as f: f.write("# 5. Ambient loop\n\n")
run_cmd(["watch", "--status"])

# 6. Final
with open(out_file, "a") as f: f.write("# 6. Final\n\n")
run_cmd(["doctor"])

print("Done. Generated FRIDAY_E2E_TEST.md")
