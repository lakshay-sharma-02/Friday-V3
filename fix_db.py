import re

p = "tests/test_runtime_stabilization.py"
with open(p, "r") as f:
    content = f.read()

content = content.replace("def _fresh_db() -> \"sqlite3.Connection\":", "def _fresh_db(tmp_path) -> \"sqlite3.Connection\":")
content = content.replace("_fresh_db()", "_fresh_db(tmp_path)")

with open(p, "w") as f:
    f.write(content)
print(f"Fixed {p}")

p = "tests/test_contracts.py"
with open(p, "r") as f:
    content = f.read()
# if tmp_path already in signature, skip
if "def _fresh_db() -> sqlite3.Connection:" in content:
    content = content.replace("def _fresh_db() -> sqlite3.Connection:", "def _fresh_db(tmp_path) -> sqlite3.Connection:")
    content = content.replace("_fresh_db()", "_fresh_db(tmp_path)")
    with open(p, "w") as f:
        f.write(content)
    print(f"Fixed {p}")

