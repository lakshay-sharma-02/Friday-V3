"""Friday knowledge-base storage layer.

Re-exports from core (schema, classes, migrations, utilities) and
queries (all CRUD functions) so existing ``from friday.db import X``
imports continue to work without changes.
"""

from .core import *
from .queries import *

# Explicit exports for private names used by tests/migrations
from .core import _migrate

