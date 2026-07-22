import pytest
import os
import subprocess
from friday.runtime.executors import (
    Executor, ExecutionResult, CLIExecutor, Invocation, execute_with_fallback
)

class HangingWorker(CLIExecutor):
    worker_id = "worker:claude"
    def build_invocation(self, task) -> Invocation:
        return Invocation(
            argv=["sleep", "5"],
            timeout=1  # artificially low timeout to force TimeoutExpired
        )

class MockDeepseek(Executor):
    worker_id = "worker:deepseek"
    def execute(self, task) -> ExecutionResult:
        return ExecutionResult(success=True, stdout="deepseek success", stderr="", exit_code=0, duration_ms=10, artifacts=[], error="", worker_id=self.worker_id)

class MockTask:
    def __init__(self):
        self.title = "output the current working directory and list files"
        self.task_type = "implementation"
        self.goal = self.title
        self.payload = ""
        self.evidence = []
        self.dependencies = []
        self.id = "t1"
        self.status = "pending"

def test_execute_with_fallback_catches_timeout():
    task = MockTask()
    
    # We resolve the chain: claude -> deepseek -> shell.
    # claude will hang and timeout. deepseek will succeed.
    def mock_resolver(wid):
        if wid == "worker:claude":
            return HangingWorker()
        elif wid == "worker:deepseek":
            return MockDeepseek()
        return None

    res = execute_with_fallback(task, "worker:claude", worker_resolver=mock_resolver)
    
    assert res.success is True, f"Fallback failed to succeed: {res.error}"
    assert res.stdout == "deepseek success", "Fallback did not reach deepseek"
    assert res.worker_id == "worker:deepseek", "Fallback did not record correct worker_id"

