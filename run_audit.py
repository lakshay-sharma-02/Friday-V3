import subprocess
import os

commands_to_test = [
    ["review", "pending", "dismiss", "feature:Frontend Experience"],
    ["review", "pending", "approve", "infrastructure:Authentication Infrastructure"],
    ["graph", "generate", "run pwd and ls -la"],
    ["graph", "review"],
]

audit_file = "FRIDAY_COMMAND_AUDIT.md"

env = os.environ.copy()
env["PYTHONPATH"] = "src"
env["FRIDAY_LLM_MODEL"] = "gemini/gemini-2.5-flash"

for cmd in commands_to_test:
    full_cmd = ["python", "-m", "friday.cli"] + cmd
    # properly quote the arguments for the command string representation
    cmd_str = "friday " + " ".join([f'"{c}"' if " " in c or ":" in c else c for c in cmd])
    print(f"Running {cmd_str}")
    
    try:
        res = subprocess.run(full_cmd, capture_output=True, text=True, env=env, timeout=120)
        output = res.stdout
        if res.stderr:
            output += "\n--- STDERR ---\n" + res.stderr
            
        status = "worked as expected"
        if res.returncode != 0:
            status = f"broken (exit code {res.returncode})"
        
        with open(audit_file, "a") as f:
            f.write(f"## `{cmd_str}`\n\n")
            f.write("```\n")
            f.write(output)
            f.write("\n```\n")
            f.write(f"**Note:** {status}\n\n")
            
    except Exception as e:
        with open(audit_file, "a") as f:
            f.write(f"## `{cmd_str}`\n\n")
            f.write(f"**Error:** {e}\n\n")
            f.write("**Note:** broken (exception)\n\n")

# Manually add the untested ones
untested = [
    ("friday watch --install", "Untested: Skipped because the watch timer is currently active for real, per user rules."),
    ("friday watch --uninstall", "Untested: Skipped because the watch timer is currently active for real, per user rules."),
    ("friday execute \"run pwd and ls -la\"", "Untested: Skipped because it modifies external state (executes terminal commands)."),
    ("friday runtime \"run pwd and ls -la\"", "Untested: Skipped because it modifies external state."),
]

with open(audit_file, "a") as f:
    for cmd_str, reason in untested:
        f.write(f"## `{cmd_str}`\n\n")
        f.write("```\n(skipped)\n```\n")
        f.write(f"**Note:** {reason}\n\n")

print("Done.")
