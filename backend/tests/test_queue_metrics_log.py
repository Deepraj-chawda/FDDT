"""The every-minute `log_queue_metrics` beat task: one structured JSON line per
queue, plus a WARNING `queue_alert` line when the oldest waiting task is older
than QUEUE_ALERT_OLDEST_WAITING_SECONDS."""
import json
import logging

import pytest

from app.core.config import settings
from app.tasks import fairshare, usage_tasks


def _queue(name: str, oldest: float | None) -> dict:
    return {
        "queue": name,
        "waiting": 3,
        "running": 1,
        "oldest_waiting_seconds": oldest,
        "avg_wait_seconds": 1.5,
        "p95_wait_seconds": 2.0,
        "started_in_window": 7,
    }


@pytest.fixture
def snapshot(monkeypatch):
    holder: dict = {}
    monkeypatch.setattr("app.services.queue_monitor.snapshot", lambda window_seconds: holder["snap"])
    monkeypatch.setattr(fairshare, "reset_if_idle", lambda queue, idle: None)
    return holder


def _payloads(caplog, prefix: str) -> list[dict]:
    return [
        json.loads(r.getMessage()[len(prefix) + 1:])
        for r in caplog.records
        if r.name == "fddt.usage" and r.getMessage().startswith(prefix + " ")
    ]


def test_one_json_line_per_queue_and_an_alert_only_past_the_threshold(snapshot, monkeypatch, caplog):
    monkeypatch.setattr(settings, "queue_alert_oldest_waiting_seconds", 300.0)
    snapshot["snap"] = {
        "available": True,
        "window_seconds": 300,
        "queues": [_queue("extraction_queue", 12.0), _queue("vision_queue", 301.5), _queue("forensics_queue", None)],
    }
    with caplog.at_level(logging.INFO, logger="fddt.usage"):
        usage_tasks.log_queue_metrics()

    metrics = _payloads(caplog, "queue_metrics")
    assert [m["queue"] for m in metrics] == ["extraction_queue", "vision_queue", "forensics_queue"]
    assert metrics[0] == {
        "queue": "extraction_queue", "waiting": 3, "running": 1, "oldest_waiting_seconds": 12.0,
        "avg_wait_seconds": 1.5, "p95_wait_seconds": 2.0, "started_in_window": 7, "window_seconds": 300,
    }

    alerts = _payloads(caplog, "queue_alert")
    assert [a["queue"] for a in alerts] == ["vision_queue"]
    assert alerts[0]["alert"] == "oldest_waiting_exceeded" and alerts[0]["threshold_seconds"] == 300.0
    assert all(r.levelno == logging.WARNING for r in caplog.records if r.getMessage().startswith("queue_alert"))


def test_threshold_zero_disables_the_alert(snapshot, monkeypatch, caplog):
    monkeypatch.setattr(settings, "queue_alert_oldest_waiting_seconds", 0.0)
    snapshot["snap"] = {"available": True, "window_seconds": 300, "queues": [_queue("vision_queue", 9999.0)]}
    with caplog.at_level(logging.INFO, logger="fddt.usage"):
        usage_tasks.log_queue_metrics()
    assert len(_payloads(caplog, "queue_metrics")) == 1
    assert _payloads(caplog, "queue_alert") == []


def test_fair_share_counters_reset_only_on_a_fully_idle_queue(monkeypatch):
    # The job runs on housekeeping_queue now, so forensics is idle at running == 0.
    resets = []
    monkeypatch.setattr(fairshare, "reset_if_idle", lambda queue, idle: resets.append((queue, idle)))
    idle = {**_queue("forensics_queue", None), "waiting": 0, "running": 0}
    running = {**_queue("vision_queue", None), "waiting": 0, "running": 1}
    monkeypatch.setattr(
        "app.services.queue_monitor.snapshot",
        lambda window_seconds: {"available": True, "window_seconds": 300, "queues": [idle, running]},
    )
    usage_tasks.log_queue_metrics()
    assert resets == [("forensics_queue", True), ("vision_queue", False)]
