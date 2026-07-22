#!/usr/bin/env python3
import sys
sys.path.insert(0, 'src')
from friday.db import connect

conn = connect()

# Check all tables
print("=== Pending Initiatives ===")
rows = conn.execute('SELECT id, title FROM pending_initiatives').fetchall()
print(f"Count: {len(rows)}")
for r in rows:
    print(f"  {r[0]}: {r[1]}")

print("\n=== Initiatives ===")
rows = conn.execute('SELECT id, title, status, confidence FROM initiatives').fetchall()
print(f"Count: {len(rows)}")
for r in rows:
    print(f"  {r[0]}: {r[1]} ({r[2]}, {r[3]})")

conn.close()