from friday.db import connect
from friday.planning import TaskGraphEngine
from friday.worker.engine import ensure_runtime_bootstrapped
conn = connect(":memory:")
ensure_runtime_bootstrapped(conn)
g = TaskGraphEngine(conn).generate("run command 'pwd'")
for t in g.tasks:
    print(t.task_type, t.symbolic)
