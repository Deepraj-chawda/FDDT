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
#   FORENSICS_WORKER_CONCURRENCY   default 0 = automatic: one process per CPU this
#                                  container may use, but no more than its memory
#                                  limit allows (FORENSICS_MEMORY_PER_PROCESS_MB,
#                                  default 768, per process)
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
      # several replicas share a host — so read the cgroup quota first:
      # cgroup v2 (cpu.max) or cgroup v1 (cpu.cfs_quota_us / cpu.cfs_period_us).
      # Azure Container Apps uses v1: with only the v2 file checked, a 0.5-CPU
      # 1 GiB worker fell back to nproc = 4 and its processes were OOM-killed.
      CONCURRENCY=""
      QUOTA=""; PERIOD=""
      if [ -r ${CGROUP_ROOT:-/sys/fs/cgroup}/cpu.max ]; then
        read -r QUOTA PERIOD < ${CGROUP_ROOT:-/sys/fs/cgroup}/cpu.max
      elif [ -r ${CGROUP_ROOT:-/sys/fs/cgroup}/cpu/cpu.cfs_quota_us ] && [ -r ${CGROUP_ROOT:-/sys/fs/cgroup}/cpu/cpu.cfs_period_us ]; then
        QUOTA=$(cat ${CGROUP_ROOT:-/sys/fs/cgroup}/cpu/cpu.cfs_quota_us)
        PERIOD=$(cat ${CGROUP_ROOT:-/sys/fs/cgroup}/cpu/cpu.cfs_period_us)
        [ "$QUOTA" = "-1" ] && QUOTA="max"   # v1: -1 = no limit
      fi
      if [ -n "$QUOTA" ] && [ "$QUOTA" != "max" ] && [ -n "$PERIOD" ] && [ "$PERIOD" -gt 0 ]; then
        CONCURRENCY=$(( (QUOTA + PERIOD - 1) / PERIOD ))
      fi
      [ -z "$CONCURRENCY" ] && CONCURRENCY="$(nproc 2>/dev/null || echo 2)"

      # Never more processes than the memory limit holds: each one loads the
      # interpreter, OpenCV and PyMuPDF and renders pages.
      MEM_LIMIT=""
      if [ -r ${CGROUP_ROOT:-/sys/fs/cgroup}/memory.max ]; then
        MEM_LIMIT=$(cat ${CGROUP_ROOT:-/sys/fs/cgroup}/memory.max)
      elif [ -r ${CGROUP_ROOT:-/sys/fs/cgroup}/memory/memory.limit_in_bytes ]; then
        MEM_LIMIT=$(cat ${CGROUP_ROOT:-/sys/fs/cgroup}/memory/memory.limit_in_bytes)
      fi
      PER_PROCESS_MB="${FORENSICS_MEMORY_PER_PROCESS_MB:-768}"
      # "max" or v1's ~2^63 "no limit" value: no memory cap. (Compared by
      # length: the shell's integers can't hold 2^63 reliably.)
      if [ -n "$MEM_LIMIT" ] && [ "$MEM_LIMIT" != "max" ] && [ "${#MEM_LIMIT}" -le 15 ]; then
        BY_MEMORY=$(( MEM_LIMIT / 1048576 / PER_PROCESS_MB ))
        [ "$BY_MEMORY" -lt "$CONCURRENCY" ] && CONCURRENCY="$BY_MEMORY"
      fi
      [ "$CONCURRENCY" -lt 1 ] && CONCURRENCY=1
      echo "start-worker.sh: forensics concurrency=$CONCURRENCY (cpu quota=${QUOTA:-?}/${PERIOD:-?}, memory limit=${MEM_LIMIT:-?}, ${PER_PROCESS_MB} MB per process)"
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
