"""
Sync vs Async comparison.

Demonstrates:
- Running the same queries synchronously (one at a time) vs asynchronously (concurrent)
- Timing both approaches to show the performance difference
- Async is faster when running multiple independent queries
"""

import asyncio
import time
from wiz_sdk import WizClient, Config

Config.load()
client = WizClient()

QUERIES = [
    {
        "name": "Projects",
        "query": """
            query ListProjects($first: Int) {
                projects(first: $first) {
                    nodes { id name }
                    pageInfo { hasNextPage endCursor }
                }
            }
        """,
        "vars": {"first": 10},
        "key": "projects",
    },
    {
        "name": "Users",
        "query": """
            query ListUsers($first: Int) {
                users(first: $first) {
                    nodes { id name email }
                    pageInfo { hasNextPage endCursor }
                }
            }
        """,
        "vars": {"first": 10},
        "key": "users",
    },
    {
        "name": "Service Accounts",
        "query": """
            query ListServiceAccounts($first: Int) {
                serviceAccounts(first: $first) {
                    nodes { id name }
                    pageInfo { hasNextPage endCursor }
                }
            }
        """,
        "vars": {"first": 10},
        "key": "serviceAccounts",
    },
]


# ─── Sync: one query at a time ─────────────────────────────────────
def run_sync():
    print("=== SYNC (sequential) ===")
    start = time.perf_counter()

    for q in QUERIES:
        t0 = time.perf_counter()
        result = client.create_request(
            query=q["query"],
            vars=q["vars"],
            paginate=False,
        ).submit()

        elapsed = time.perf_counter() - t0
        if result.success:
            count = len(result.data[q["key"]]["nodes"])
            print(f"  {q['name']:20s} {count:>4} results  ({elapsed:.2f}s)")
        else:
            print(f"  {q['name']:20s} FAILED  ({elapsed:.2f}s)")

    total = time.perf_counter() - start
    print(f"  {'TOTAL':20s}              ({total:.2f}s)\n")


# ─── Async: all queries at once ─────────────────────────────────────
async def run_async():
    print("=== ASYNC (concurrent) ===")
    start = time.perf_counter()

    async with client.async_session() as async_client:
        tasks = []
        for q in QUERIES:
            request = await async_client.create_async_request(
                query=q["query"],
                vars=q["vars"],
                paginate=False,
            )
            tasks.append((q, request))

        # Fire all queries concurrently
        results = await asyncio.gather(*(req.submit() for _, req in tasks))

    for (q, _), result in zip(tasks, results):
        if result.success:
            count = len(result.data[q["key"]]["nodes"])
            print(f"  {q['name']:20s} {count:>4} results")
        else:
            print(f"  {q['name']:20s} FAILED")

    total = time.perf_counter() - start
    print(f"  {'TOTAL':20s}              ({total:.2f}s)\n")


# ─── Run both ───────────────────────────────────────────────────────
if __name__ == "__main__":
    run_sync()
    asyncio.run(run_async())
