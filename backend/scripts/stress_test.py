"""Async load / stress tester for the ARIADNE stack."""
from __future__ import annotations

import asyncio
import os
import time

import httpx

API_KEY = os.environ.get("API_SECRET", "")


async def _worker(client, method, url, json, sem, latencies, statuses):
    async with sem:
        t = time.perf_counter()
        try:
            if method == "GET":
                r = await client.get(url)
            else:
                r = await client.post(url, json=json)
            statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
        except Exception as exc:  # noqa: BLE001
            statuses[type(exc).__name__] = statuses.get(type(exc).__name__, 0) + 1
        finally:
            latencies.append(time.perf_counter() - t)


def _pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, int(round(p / 100.0 * (len(sorted_vals) - 1))))
    return sorted_vals[i]


async def run(name, method, url, n, concurrency, json=None, headers=None):
    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    statuses: dict = {}
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=120, limits=limits, headers=headers or {}) as client:
        await asyncio.gather(*[
            _worker(client, method, url, json, sem, latencies, statuses)
            for _ in range(n)
        ])
    wall = time.perf_counter() - t0
    s = sorted(latencies)
    ok = statuses.get(200, 0)
    result = {
        "name": name, "n": n, "concurrency": concurrency, "wall_s": wall,
        "rps": n / wall if wall else 0.0, "ok": ok, "fail": n - ok,
        "statuses": statuses,
        "p50": _pct(s, 50) * 1000, "p90": _pct(s, 90) * 1000,
        "p95": _pct(s, 95) * 1000, "p99": _pct(s, 99) * 1000,
        "max": (s[-1] * 1000 if s else 0.0), "min": (s[0] * 1000 if s else 0.0),
        "latencies": s,
    }
    return result


def _print_result(r):
    print(f"\n=== {r['name']} ===")
    print(f"requests={r['n']}  concurrency={r['concurrency']}  wall={r['wall_s']:.2f}s")
    print(f"throughput={r['rps']:.1f} req/s   ok={r['ok']}  fail={r['fail']}  statuses={r['statuses']}")
    print(f"latency ms  min={r['min']:.0f}  p50={r['p50']:.0f}  p90={r['p90']:.0f}  "
          f"p95={r['p95']:.0f}  p99={r['p99']:.0f}  max={r['max']:.0f}")


def _ascii_hist(latencies_ms, buckets=10, width=40):
    if not latencies_ms:
        return
    lo, hi = latencies_ms[0], latencies_ms[-1]
    if hi <= lo:
        hi = lo + 1
    step = (hi - lo) / buckets
    counts = [0] * buckets
    for v in latencies_ms:
        idx = min(buckets - 1, int((v - lo) / step))
        counts[idx] += 1
    mx = max(counts) or 1
    print("\nlatency histogram (ms):")
    for i, c in enumerate(counts):
        a = lo + i * step
        b = a + step
        bar = "#" * int(width * c / mx)
        print(f"  {a:7.0f}-{b:7.0f} | {bar} {c}")


async def main():
    chat = "http://assistant:8001/api/v1/assistant/chat"
    demo = {"campus_id": "campus-demo", "building_id": "bldg-demo-1"}

    scenarios = [
        ("Gateway /health (middleware)", "GET", "http://middleware:8080/health", 2000, 100, None),
        ("Backend /health", "GET", "http://backend:8000/health", 2000, 100, None),
        ("Assistant deterministic (find-place)", "POST", chat, 300, 30,
         {**demo, "user_query": "where is the toilet"}),
        ("Assistant LLM path (RAG)", "POST", chat, 30, 4,
         {**demo, "user_query": "tell me about the demo building"}),
    ]
    results = []
    for name, method, url, n, c, body in scenarios:
        headers = {"X-Api-Key": API_KEY} if ":8080" in url and API_KEY else None
        r = await run(name, method, url, n, c, json=body, headers=headers)
        _print_result(r)
        results.append(r)

    # Histogram for the heaviest user-facing fast path.
    fp = next((x for x in results if "find-place" in x["name"]), None)
    if fp:
        _ascii_hist([v * 1000 for v in fp["latencies"]])

    print("\n=== throughput summary (req/s) ===")
    mx = max((r["rps"] for r in results), default=1) or 1
    for r in results:
        bar = "#" * int(40 * r["rps"] / mx)
        print(f"  {r['name'][:34]:34} | {bar} {r['rps']:.1f}")


if __name__ == "__main__":
    asyncio.run(main())
