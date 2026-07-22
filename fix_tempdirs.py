import os
import re
from pathlib import Path

files = [
    "tests/test_contracts.py",
    "tests/test_runtime.py",
    "tests/test_runtime_stabilization.py",
    "tests/test_scheduler.py",
]

def fix_file(p):
    with open(p, "r") as f:
        content = f.read()

    # Add tmp_path to test definitions
    content = re.sub(r'def test_([a-zA-Z0-9_]+)\(\):', r'def test_\1(tmp_path):', content)

    # replace Path(tempfile.mkdtemp()) with tmp_path
    content = content.replace("Path(tempfile.mkdtemp())", "tmp_path")
    # replace tempfile.mkdtemp() with str(tmp_path)
    content = content.replace("tempfile.mkdtemp()", "str(tmp_path)")

    with open(p, "w") as f:
        f.write(content)
    print(f"Fixed {p}")

for f in files:
    fix_file(f)
