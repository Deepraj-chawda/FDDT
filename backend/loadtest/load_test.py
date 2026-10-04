"""
Queue fairness / rate-limit load test (drives the real API over HTTP).

Prerequisites: Postgres + Redis up, migrations applied, `python seed.py` run,
and the stubbed API + one stubbed worker per queue running:

    python -m loadtest.run_api 8100
    python -m loadtest.run_worker extraction 16
    python -m loadtest.run_worker vision 16
    python -m loadtest.run_worker forensics 4

then:

    python -m loadtest.load_test [--api http://127.0.0.1:8100]

Scenario 1 — 27 concurrent single-document uploads from 9 users in 3
companies, all released at once by a barrier.
Scenario 2 — Company A uploads a 100-document case (10 parallel uploads at a
time, like a browser), then Company B immediately uploads one document.

Prints the processing order, per-queue wait times, the observed Azure call
rates, simulated-Azure 429 counts, and a cross-check of the queue monitor
endpoint against Redis. Writes the raw numbers to loadtest_results.json.
"""
from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import redis

from app.core.config import settings

SAMPLES = sorted((Path(__file__).resolve().parents[2] / "sample-documents").glob("*.pdf"))
PIPELINE_TASKS = (
    "process_document", "run_metadata_forensics", "run_tampering_checks", "run_duplicate_check",
    "run_visual_inconsistency_review", "run_signature_detection",
)
QUEUE_OF = {
    "process_document": "extraction_queue",
    "run_visual_inconsistency_review": "vision_queue",
    "run_signature_detection": "vision_queue",
    "run_document_checks": "vision_queue",
    "run_metadata_forensics": "forensics_queue",
    "run_tampering_checks": "forensics_queue",
    "run_duplicate_check": "forensics_queue",
}


class Api:
    def __init__(self, base: str):
        self.base = base
        self.http = httpx.Client(base_url=base, timeout=120)

    def login(self, email, password="LoadTest-pass1!"):
        res = self.http.post("/auth/login", json={"email": email, "password": password})
        res.raise_for_status()
        return {"Authorization": f"Bearer {res.json()['access_token']}"}


def setup_tenants(api: Api, admin_headers, tag: str, companies=("A", "B", "C"), users_per_company=3):
    out = {}
    for key in companies:
        res = api.http.post("/platform/companies", headers=admin_headers, json={"name": f"Loadtest {key} {tag}"})
        res.raise_for_status()
        company_id = res.json()["id"]
        headers = []
        for i in range(users_per_company):
            email = f"lt.{key.lower()}{i}.{tag}@example.com"
            api.http.post("/settings/users", headers=admin_headers, json={
                "email": email, "password": "LoadTest-pass1!", "role": "user", "company_id": company_id,
            }).raise_for_status()
            headers.append(api.login(email))
        out[key] = {"id": company_id, "users": headers}
    return out


def new_case(http, headers):
    res = http.post("/cases", headers=headers, json={"case_type": "vendor_invoice"})
    res.raise_for_status()
    return res.json()["id"]


def upload(http, headers, case_id, n):
    sample = SAMPLES[n % len(SAMPLES)]
    res = http.post(
        f"/cases/{case_id}/documents", headers=headers,
        files={"file": (f"{n:03d}-{sample.name}", sample.read_bytes(), "application/pdf")},
    )
    res.raise_for_status()
    return res.json()["id"], time.time()


def queue_len(r, q):
    """All fair-share priority lists of one queue."""
    from app.services.queue_monitor import priority_lists

    return sum(r.llen(name) for name in priority_lists(q))


def wait_until_idle(r, timeout=1800, settle=6):
    start, idle_since = time.time(), None
    while time.time() - start < timeout:
        busy = sum(queue_len(r, q) for q in ("extraction_queue", "vision_queue", "forensics_queue")) + r.hlen("unacked")
        if busy == 0:
            idle_since = idle_since or time.time()
            if time.time() - idle_since >= settle:
                return time.time() - start
        else:
            idle_since = None
        time.sleep(1)
    raise TimeoutError("queues did not drain")


def task_starts(r):
    out = []
    for raw in r.lrange("loadtest:starts", 0, -1):
        stamp, name, ident = raw.decode().split("|", 2)
        out.append((float(stamp), name, ident))
    return sorted(out)


def max_in_window(stamps, window):
    stamps = sorted(stamps)
    best, j = 0, 0
    for i in range(len(stamps)):
        while stamps[i] - stamps[j] >= window:
            j += 1
        best = max(best, i - j + 1)
    return best


def kendall_tau(a, b):
    """Rank agreement between two orderings of the same items (1 = identical)."""
    pos = {x: i for i, x in enumerate(b)}
    seq = [pos[x] for x in a if x in pos]
    n = len(seq)
    concordant = sum(1 for i in range(n) for j in range(i + 1, n) if seq[i] < seq[j])
    pairs = n * (n - 1) / 2
    return round((2 * concordant - pairs) / pairs, 3) if pairs else 1.0


def scenario_1(api, tenants, r):
    print("\n=== Scenario 1: 27 concurrent single-document uploads, 9 users, 3 companies ===")
    jobs = []
    for key in ("A", "B", "C"):
        for headers in tenants[key]["users"]:
            for _ in range(3):
                jobs.append((key, headers, new_case(api.http, headers)))
    barrier = threading.Barrier(len(jobs))
    results = []
    lock = threading.Lock()

    def go(i, job):
        key, headers, case_id = job
        with httpx.Client(base_url=api.base, timeout=120) as http:
            barrier.wait()
            doc_id, done = upload(http, headers, case_id, i)
        with lock:
            results.append({"company": key, "document_id": doc_id, "submitted_at": done})

    t0 = time.time()
    with ThreadPoolExecutor(len(jobs)) as pool:
        list(pool.map(lambda p: go(*p), enumerate(jobs)))
    drained = wait_until_idle(r)
    starts = task_starts(r)
    by_doc = {res["document_id"]: res for res in results}
    submit_order = [d["document_id"] for d in sorted(results, key=lambda d: d["submitted_at"])]

    report = {"documents": len(results), "upload_burst_seconds": round(max(d["submitted_at"] for d in results) - t0, 2),
              "drain_seconds": round(drained, 1), "queues": {}}
    for task in ("process_document", "run_visual_inconsistency_review", "run_tampering_checks"):
        order = [ident for _, name, ident in starts if name == task and ident in by_doc]
        start_of = {ident: s for s, name, ident in starts if name == task and ident in by_doc}
        waits = [start_of[d] - by_doc[d]["submitted_at"] for d in order]
        report["queues"][QUEUE_OF[task]] = {
            "task": task,
            "start_order_companies": "".join(by_doc[d]["company"] for d in order),
            "kendall_tau_vs_submission": kendall_tau(order, submit_order),
            "wait_seconds_min_avg_max": [round(min(waits), 2), round(statistics.fmean(waits), 2), round(max(waits), 2)],
        }
    for q, info in report["queues"].items():
        print(f"  {q:16s} {info['task']:32s} start order by company: {info['start_order_companies']}")
        print(f"  {'':16s} Kendall tau vs submission order: {info['kendall_tau_vs_submission']}   "
              f"wait s (min/avg/max): {info['wait_seconds_min_avg_max']}")
    print(f"  upload burst {report['upload_burst_seconds']} s; all pipelines drained after {report['drain_seconds']} s")
    return report


def scenario_2(api, tenants, r, admin_headers, bulk=100):
    print(f"\n=== Scenario 2: Company A uploads {bulk} documents, then Company B uploads 1 ===")
    a_headers, b_headers = tenants["A"]["users"][0], tenants["B"]["users"][0]
    a_case, b_case = new_case(api.http, a_headers), new_case(api.http, b_headers)
    monitor_samples, stop = [], threading.Event()

    def monitor():
        with httpx.Client(base_url=api.base, timeout=30) as http:
            while not stop.is_set():
                t = time.time()
                snap = http.get("/platform/queues?window_minutes=5", headers=admin_headers).json()
                direct = {q: queue_len(r, q) for q in ("extraction_queue", "vision_queue", "forensics_queue")}
                monitor_samples.append({
                    "t": t,
                    "endpoint_waiting": {q["queue"]: q["waiting"] for q in snap.get("queues", [])},
                    "endpoint_running": {q["queue"]: q["running"] for q in snap.get("queues", [])},
                    "endpoint_oldest": {q["queue"]: q["oldest_waiting_seconds"] for q in snap.get("queues", [])},
                    "redis_llen": direct,
                })
                time.sleep(2)

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    t0 = time.time()
    a_docs = []
    with ThreadPoolExecutor(10) as pool:  # ~a browser's parallel uploads
        def up(n):
            with httpx.Client(base_url=api.base, timeout=120) as http:
                return upload(http, a_headers, a_case, n)
        a_docs = list(pool.map(up, range(bulk)))
    a_done_uploading = time.time()
    b_doc, b_submitted = upload(api.http, b_headers, b_case, 7)
    print(f"  A's {bulk} uploads took {a_done_uploading - t0:.1f} s; B submitted {b_submitted - a_done_uploading:.2f} s later")
    drained = wait_until_idle(r)
    stop.set()
    mon.join(timeout=5)

    starts = task_starts(r)
    a_ids = {d for d, _ in a_docs}
    report = {"drain_seconds": round(drained, 1), "b_document": b_doc, "per_queue": {}, "monitor_samples": monitor_samples}
    for task in PIPELINE_TASKS:
        order = [ident for _, name, ident in starts if name == task and (ident in a_ids or ident == b_doc)]
        start_of = {ident: s for s, name, ident in starts if name == task}
        if b_doc not in order:
            continue
        b_pos = order.index(b_doc)
        a_waits = [start_of[d] - t for d, t in a_docs if d in start_of]
        report["per_queue"][task] = {
            "queue": QUEUE_OF[task],
            "a_documents_started_before_b": b_pos,
            "b_wait_seconds": round(start_of[b_doc] - b_submitted, 1),
            "a_wait_seconds_median": round(statistics.median(a_waits), 1),
            "a_wait_seconds_max": round(max(a_waits), 1),
        }
    # When each queue last started a scenario-2 task (= when it drained).
    last_start = {}
    for stamp, name, ident in starts:
        if ident in a_ids or ident == b_doc:
            q = QUEUE_OF.get(name)
            if q:
                last_start[q] = max(last_start.get(q, 0), stamp - t0)
    report["queue_drained_after_seconds"] = {q: round(v, 1) for q, v in sorted(last_start.items(), key=lambda kv: kv[1])}
    print(f"  each queue's last task started after (s): {report['queue_drained_after_seconds']}")
    for task, info in report["per_queue"].items():
        print(f"  {info['queue']:16s} {task:32s} B started after {info['a_documents_started_before_b']:3d}/{bulk} of A's; "
              f"B waited {info['b_wait_seconds']:6.1f}s (A median {info['a_wait_seconds_median']}s, max {info['a_wait_seconds_max']}s)")
    print(f"  all pipelines drained after {report['drain_seconds']} s")
    return report


def rate_report(r):
    print("\n=== Azure call rates (as seen by the simulated Azure services) ===")
    def stamps(service):
        return [float(x) for x in r.lrange(f"loadtest:calls:{service}", 0, -1)]
    di, oa = stamps("document_intelligence"), stamps("openai_requests")
    out = {
        "document_intelligence": {
            "calls": len(di), "max_calls_in_any_1s": max_in_window(di, 1.0),
            "app_cap_per_s": settings.azure_document_intelligence_max_calls_per_second,
            "simulated_azure_quota_per_s": float(__import__("os").environ.get("LOADTEST_AZURE_DI_TPS", "15")),
            "azure_429s": int(r.get("loadtest:azure:document_intelligence:429") or 0),
        },
        "azure_openai": {
            "calls": len(oa), "max_calls_in_any_60s": max_in_window(oa, 60.0),
            "app_cap_per_min": settings.azure_openai_max_requests_per_minute,
            "app_token_cap_per_min": settings.azure_openai_max_tokens_per_minute,
            "azure_429s": int(r.get("loadtest:azure:openai_requests:429") or 0)
            + int(r.get("loadtest:azure:openai_tokens:429") or 0),
        },
    }
    for key in ("azure_document_intelligence", "azure_openai", "azure_openai_tokens"):
        raw = r.hgetall(f"fddt:ratelimit:{key}:stats")
        out.setdefault("limiter", {})[key] = {k.decode(): float(v) for k, v in raw.items()}
    d, o = out["document_intelligence"], out["azure_openai"]
    print(f"  Document Intelligence: {d['calls']} calls, peak {d['max_calls_in_any_1s']} in any 1 s "
          f"(app cap {d['app_cap_per_s']:g}/s, simulated Azure quota {d['simulated_azure_quota_per_s']:g}/s), 429s: {d['azure_429s']}")
    print(f"  Azure OpenAI:          {o['calls']} calls, peak {o['max_calls_in_any_60s']} in any 60 s "
          f"(app cap {o['app_cap_per_min']:g}/min), 429s: {o['azure_429s']}")
    for key, stats in out["limiter"].items():
        print(f"  limiter {key:28s} calls={int(stats.get('calls', 0))} throttled={int(stats.get('throttled_calls', 0))} "
              f"throttled_seconds={stats.get('throttled_seconds', 0):.1f} http_429={int(stats.get('http_429', 0))}")
    return out


def monitor_report(samples):
    print("\n=== Queue monitor endpoint vs. Redis (sampled every 2 s during scenario 2) ===")
    peak = {q: max((s["endpoint_waiting"].get(q, 0) for s in samples), default=0)
            for q in ("extraction_queue", "vision_queue", "forensics_queue")}
    diffs = [abs(s["endpoint_waiting"].get(q, 0) - s["redis_llen"][q]) for s in samples for q in s["redis_llen"]]
    oldest = {q: max((s["endpoint_oldest"].get(q) or 0 for s in samples), default=0) for q in peak}
    print(f"  samples: {len(samples)}; peak waiting per queue (endpoint): {peak}")
    print(f"  peak 'oldest waiting' age per queue: {oldest}")
    print(f"  |endpoint waiting - Redis LLEN| over all samples: max {max(diffs, default=0)}, "
          f"mean {statistics.fmean(diffs) if diffs else 0:.2f} (sampled moments apart while workers drain)")
    return {"peak_waiting": peak, "peak_oldest_seconds": oldest, "max_abs_diff": max(diffs, default=0)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8100")
    parser.add_argument("--admin-email", default="admin@example.com")
    parser.add_argument("--admin-password", default="ChangeMe123!")
    parser.add_argument("--bulk", type=int, default=100, help="documents in scenario 2's batch")
    parser.add_argument("--only-scenario-2", action="store_true")
    parser.add_argument("--out", default="loadtest_results.json")
    args = parser.parse_args()

    r = redis.Redis.from_url(settings.celery_broker_url)
    for key in r.scan_iter("loadtest:*"):
        r.delete(key)
    for key in list(r.scan_iter("fddt:fairshare:*")) + list(r.scan_iter("fddt:queue:*")):
        r.delete(key)
    for key in r.scan_iter("fddt:ratelimit:*"):
        r.delete(key)

    api = Api(args.api)
    admin = api.login(args.admin_email, args.admin_password)
    tag = uuid.uuid4().hex[:6]
    tenants = setup_tenants(api, admin, tag)

    results = {} if args.only_scenario_2 else {"scenario_1": scenario_1(api, tenants, r)}
    r.delete("loadtest:starts")
    results["scenario_2"] = scenario_2(api, tenants, r, admin, bulk=args.bulk)
    results["rates"] = rate_report(r)
    results["monitor"] = monitor_report(results["scenario_2"].pop("monitor_samples"))
    usage = api.http.get("/platform/usage?period=all_time", headers=admin).json()
    results["usage"] = [row for row in usage["companies"] if row["company_name"].endswith(tag)]
    print("\n=== Billing counters for the load-test companies ===")
    for row in results["usage"]:
        print(f"  {row['company_name']}: cases={row['cases_created']} documents={row['documents_uploaded']} "
              f"bytes={row['storage_bytes']}")
    Path(args.out).write_text(json.dumps(results, indent=2, default=str))
    print(f"\nRaw results: {args.out}")


if __name__ == "__main__":
    main()
