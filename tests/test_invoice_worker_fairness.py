from app.workers.invoice_worker import should_poll_benchmark


def test_benchmark_is_polled_when_normal_invoice_queue_is_empty():
    assert should_poll_benchmark(
        normal_status="empty",
        normal_jobs_since_benchmark=0,
        interval_jobs=5,
    ) is True


def test_normal_invoice_queue_retains_priority_before_fairness_interval():
    assert should_poll_benchmark(
        normal_status="completed",
        normal_jobs_since_benchmark=4,
        interval_jobs=5,
    ) is False


def test_busy_normal_queue_cannot_starve_benchmark_suite():
    assert should_poll_benchmark(
        normal_status="completed",
        normal_jobs_since_benchmark=5,
        interval_jobs=5,
    ) is True


def test_failed_normal_jobs_also_count_toward_fairness_interval():
    assert should_poll_benchmark(
        normal_status="failed",
        normal_jobs_since_benchmark=5,
        interval_jobs=5,
    ) is True
