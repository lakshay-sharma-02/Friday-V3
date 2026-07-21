from friday.runtime.benchmark import BenchmarkRunner, BenchmarkTask


def test_benchmark_runs_capability_level():
    tasks = [BenchmarkTask(capability="Documentation",
                           payload="write a one-line doc",
                           expect_nonempty_stdout=True)]
    workers = [("worker:echo", lambda p: ("ok", 0))]
    runner = BenchmarkRunner(tasks, workers)
    report = runner.run()
    assert "Documentation" in report
    row = report["Documentation"][0]
    assert row.worker == "worker:echo"
    assert row.passed is True
    assert row.duration_ms >= 0


def test_benchmark_fails_on_nonzero_exit():
    tasks = [BenchmarkTask(capability="Testing", payload="x",
                           expect_nonempty_stdout=False)]
    workers = [("worker:fail", lambda p: ("", 1))]
    report = BenchmarkRunner(tasks, workers).run()
    assert report["Testing"][0].passed is False
