#!/bin/sh
# Celery worker for ONE processing queue. Run one (or more) per queue so each
# queue scales independently:
#
#   ./start-worker.sh extraction   # Azure Document Intelligence (rate-limited)
#   ./start-worker.sh vision       # Azure OpenAI (rate-limited)
#   ./start-worker.sh forensics    # local CPU, no rate limit — scale freely
#
# Pool sizes come from env (no code change to tune):
#   EXTRACTION_WORKER_CONCURRENCY  default 4  — enough to saturate the global
#                                  AZURE_DOCUMENT_INTELLIGENCE_MAX_CALLS_PER_SECOND cap
#   VISION_WORKER_CONCURRENCY      default 4  — enough to saturate AZURE_OPENAI_MAX_REQUESTS_PER_MINUTE
#   FORENSICS_WORKER_CONCURRENCY   default 0 = one process per CPU core
# CELERY_POOL overrides the pool type (default: threads for the two I/O-bound
# Azure queues, prefork for CPU-bound forensics; use "threads" on Windows).
set -e
QUEUE="${1:-${WORKER_QUEUE:-forensics}}"
case "$QUEUE" in
  extraction)
    CONCURRENCY="${EXTRACTION_WORKER_CONCURRENCY:-4}"
    POOL="${CELERY_POOL:-threads}" ;;
  vision)
    CONCURRENCY="${VISION_WORKER_CONCURRENCY:-4}"
    POOL="${CELERY_POOL:-threads}" ;;
  forensics)
    CONCURRENCY="${FORENSICS_WORKER_CONCURRENCY:-0}"
    if [ "$CONCURRENCY" = "0" ]; then
      # One process per core THIS container may use. `nproc` reports the
      # host's cores and ignores a container CPU limit (docker --cpus, k8s /
      # Container Apps CPU requests), which would oversubscribe the CPU when
      # several replicas share a host — so read the cgroup quota first.
      CONCURRENCY=""
      if [ -r /sys/fs/cgroup/cpu.max ]; then
        read -r QUOTA PERIOD < /sys/fs/cgroup/cpu.max
        if [ "$QUOTA" != "max" ] && [ -n "$PERIOD" ]; then
          CONCURRENCY=$(( (QUOTA + PERIOD - 1) / PERIOD ))
        fi
      fi
      [ -z "$CONCURRENCY" ] && CONCURRENCY="$(nproc 2>/dev/null || echo 2)"
      [ "$CONCURRENCY" -lt 1 ] && CONCURRENCY=1
    fi
    POOL="${CELERY_POOL:-prefork}"
    # Also drain Celery's old default queue, so messages enqueued before the
    # three-queue split are never stranded.
    EXTRA_QUEUES=",celery" ;;
  *)
    echo "usage: $0 extraction|vision|forensics" >&2
    exit 2 ;;
esac
exec celery -A app.tasks.celery_app worker \
  --queues="${QUEUE}_queue${EXTRA_QUEUES:-}" \
  --hostname="${QUEUE}@%h" \
  --concurrency="$CONCURRENCY" \
  --pool="$POOL" \
  --loglevel=info
