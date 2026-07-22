from friday.runtime.executors import ClaudeCodeWorker
from dataclasses import dataclass
from typing import List

@dataclass
class MockTask:
    title: str = "Refactor calculator"
    description: str = "Refactor process_numbers in calculator.py for clarity without changing behavior. Ensure tests pass."
    acceptance_criteria: List[str] = None
    runtime_payload: str = ""

if __name__ == "__main__":
    task = MockTask(acceptance_criteria=["Do not break test_calculator.py"])
    worker = ClaudeCodeWorker(workspace=".")
    res = worker.execute(task)
    print("Success:", res.success)
    print("Exit code:", res.exit_code)
    print("Stdout:", res.stdout)
    print("Stderr:", res.stderr)
