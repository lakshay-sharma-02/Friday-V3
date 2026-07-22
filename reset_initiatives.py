#!/usr/bin/env python3
import sys
sys.path.insert(0, 'src')
from friday.db import connect

conn = connect()
# Delete all initiatives and their history
conn.execute('DELETE FROM initiative_relationships')
conn.execute('DELETE FROM initiative_evolution')
conn.execute('DELETE FROM initiative_history')
conn.execute('DELETE FROM initiatives')
conn.commit()
conn.close()
print('Initiatives deleted')