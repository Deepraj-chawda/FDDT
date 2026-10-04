"""Run a real Celery worker for one queue with the external services stubbed
(see loadtest/stubs.py). Also records every task start (task, document/case
id, time) in Redis so load_test.py can reconstruct the processing order.

    python -m loadtest.run_worker extraction|vision|forensics <concurrency|auto>

LOADTEST_POOL selects the pool (default threads; the Linux load test runs
forensics with prefork). "auto" concurrency = CPUs of this container's cgroup
limit (like start-worker.sh), else os.cpu_count().
"""
import os
import sys
import time

from loadtest import stubs

stubs.install()

from celery.signals import task_prerun  # noqa: E402

from app.tasks.celery_app import celery_app  # noqa: E402


@task_prerun.connect
def _log_start(task=None, args=None, **_kwargs):
    try:
        r = stubs._redis()
        r.rpush("loadtest:starts", f"{time.time():.3f}|{task.name}|{(args or [''])[0]}")
    except Exception:  # noqa: BLE001
        pass


def _auto_concurrency() -> int:
    try:
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()
        if quota != "max":
            return max(1, -(-int(quota) // int(period)))
    except (OSError, ValueError):
        pass
    return os.cpu_count() or 2


if __name__ == "__main__":
    queue = sys.argv[1]
    concurrency = sys.argv[2] if len(sys.argv) > 2 else "4"
    if concurrency == "auto":
        concurrency = str(_auto_concurrency())
    celery_app.worker_main(
        [
            "worker",
            f"--queues={queue}_queue",
            f"--hostname={queue}-loadtest@%h",
            f"--concurrency={concurrency}",
            f"--pool={os.environ.get('LOADTEST_POOL', 'threads')}",
            "--loglevel=WARNING",
            "--without-gossip",
            "--without-mingle",
        ]
    )
