import pytest
from unittest.mock import patch
from friday.db import connect
from friday.planning import TaskGraphEngine
from friday.resolver import CapabilityResolver
from friday.worker.engine import ensure_runtime_bootstrapped
import friday.resolver.engine
from friday.runtime.discovery import DiscoveryResult

@pytest.fixture(autouse=True)
def reset_lazy_discovery():
    friday.resolver.engine._LAZY_DISCOVERY_RAN = False
    yield
    friday.resolver.engine._LAZY_DISCOVERY_RAN = False

def _fresh_db(tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    ensure_runtime_bootstrapped(conn)
    return conn

@patch("friday.runtime.discovery.discover")
def test_lazy_discovery_triggered_for_judgment(mock_discover, tmp_path):
    mock_discover.return_value = DiscoveryResult(
        unavailable=["worker:claude"], available=["worker:shell", "worker:python"]
    )
    
    conn = _fresh_db(tmp_path)
    g = TaskGraphEngine(conn).generate("Investigate the architecture and write a report")
    
    resolver = CapabilityResolver(conn)
    res = resolver.resolve_graph(g.id)
    
    assert mock_discover.call_count == 1
    
    assignments = {a.task_id.split("#")[-1]: a.worker_id for a in res.assignments}
    assert any("claude" not in wid for wid in assignments.values()), "Did not fall back from claude"
    
    # Second resolution uses cache
    resolver.resolve_graph(g.id)
    assert mock_discover.call_count == 1

@patch("friday.runtime.discovery.discover")
def test_lazy_discovery_skipped_for_mechanical(mock_discover, tmp_path):
    conn = _fresh_db(tmp_path)
    g = TaskGraphEngine(conn).generate("run command 'pwd'")
    
    resolver = CapabilityResolver(conn)
    res = resolver.resolve_graph(g.id)
    
    assert mock_discover.call_count == 0
    
    assignments = {a.task_id.split("#")[-1]: a.worker_id for a in res.assignments}
    assert any("claude" not in wid for wid in assignments.values())
