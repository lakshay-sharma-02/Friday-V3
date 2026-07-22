#!/usr/bin/env python3
import sys
sys.path.insert(0, 'src')
from friday.db import connect

conn = connect()

# Check all initiatives by title
rows = conn.execute('SELECT id, title, initiative_type FROM initiatives').fetchall()
print('Unique initiatives:')
for r in rows:
    print(f'  {r[0]}: {r[1]} ({r[2]})')

# Check pending_initiatives
rows = conn.execute('SELECT id, title, initiative_type FROM pending_initiatives').fetchall()
print('\nPending initiatives:')
for r in rows:
    print(f'  {r[0]}: {r[1]} ({r[2]})')

conn.close()