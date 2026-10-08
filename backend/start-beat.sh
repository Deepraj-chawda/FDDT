#!/bin/sh
# Celery beat — the scheduler for periodic jobs — together with the one worker
# slot that runs them (housekeeping_queue: per-minute queue metrics, nightly
# usage-counter reconciliation, stuck-document recovery every 5 minutes).
# Keeping these jobs here means they never wake the processing workers, so
# the forensics workers can scale to zero when there is no work.
# Run exactly ONE of these: two would schedule every job twice.
set -e
exec celery -A app.tasks.celery_app worker --beat \
  --queues=housekeeping_queue \
  --hostname="housekeeping@%h" \
  --pool=solo --concurrency=1 \
  --schedule="${CELERY_BEAT_SCHEDULE_FILE:-/tmp/celerybeat-schedule}" \
  --loglevel=info
