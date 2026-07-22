#!/usr/bin/env python3
import sys
sys.path.insert(0, 'src')
from friday.db import connect

conn = connect()
rows = conn.execute('SELECT id, title, statement FROM initiatives ORDER BY title').fetchall()
conn.close()

for r in rows:
    print(f'{r["id"]}: {r["statement"][:120]}')