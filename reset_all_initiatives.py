#!/usr/bin/env python3
import sys
sys.path.insert(0, 'src')
from friday.db import connect

conn = connect()
# Delete all initiatives and their related data
conn.execute('DELETE FROM initiative_relationships')
conn.execute('DELETE FROM initiative_evolution')
conn.execute('DELETE FROM initiative_history')
conn.execute('DELETE FROM initiatives')
conn.execute('DELETE FROM pending_initiatives')
conn.commit()
conn.close()
print('All initiatives and pending initiatives deleted')