#!/bin/sh
# Celery beat — the scheduler for periodic jobs (nightly usage-counter
# reconciliation, per-minute queue metrics log line). Run exactly ONE.
set -e
exec celery -A app.tasks.celery_app beat --loglevel=info --schedule="${CELERY_BEAT_SCHEDULE_FILE:-/tmp/celerybeat-schedule}"
