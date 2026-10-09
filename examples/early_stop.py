"""
Stopping a paginated query early.

Demonstrates:
- stop_when: stop at the first result node matching a predicate
- stop_on_page: stop after a whole page, based on the page as a whole
- stop_when with iterate_nodes (streaming, never holds more than a page)
- stop_when on an async request
- Reading stop_info and resuming from where the query stopped
"""

import asyncio
from datetime import datetime, timezone

from wizsec import WizClient, Config

Config.load()
client = WizClient()

ISSUES = """
    query Issues($first: Int, $after: String) {
        issues(first: $first, after: $after) {
            nodes { id createdAt entitySnapshot { name } }
            totalCount
            pageInfo { hasNextPage endCursor }
        }
    }
"""


# ─── 1. stop_when: stop at a date cutoff ───────────────────────────
def example_stop_at_date_cutoff():
    """Issues come back newest first; stop once they get older than the cutoff."""
    print("=== stop_when: date cutoff ===")

    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def older_than_cutoff(node):
        return datetime.fromisoformat(node["createdAt"]) < cutoff

    response = client.create_request(
        query=ISSUES,
        vars={"first": 500},
        stop_when=older_than_cutoff,
    )
    result = response.submit()

    if result.success():
        nodes = result.data["issues"]["nodes"]
        # The matching node is kept, so the last one is the first issue
        # older than the cutoff.
        print(
            f"  Fetched {len(nodes)} issues (server total: "
            f"{result.data['issues']['totalCount']})"
        )
        print(f"  Stopped early: {response.stopped_early}")
        print(f"  Stop info: {response.stop_info}\n")
    else:
        print(f"  Failed: {result.errors}\n")


# ─── 2. stop_when: stop at a name match ────────────────────────────
def example_stop_at_name_match():
    """Stop as soon as an issue on a legacy resource shows up."""
    print("=== stop_when: name match ===")

    response = client.create_request(
        query=ISSUES,
        vars={"first": 500},
        stop_when=lambda n: n["entitySnapshot"]["name"].startswith("legacy-"),
    )
    result = response.submit()

    if result.success():
        nodes = result.data["issues"]["nodes"]
        print(f"  Fetched {len(nodes)} issues")
        if response.stopped_early:
            print(f"  Last issue: {nodes[-1]['entitySnapshot']['name']}\n")
        else:
            print("  Ran to the last page — no legacy resource found\n")
    else:
        print(f"  Failed: {result.errors}\n")


# ─── 3. stop_on_page: stop on a whole-page condition ───────────────
def example_stop_on_page():
    """Cap the work at roughly 2000 issues, keeping whole pages."""
    print("=== stop_on_page: budget cap ===")

    budget = {"fetched": 0}

    def budget_spent(page_data):
        budget["fetched"] += len(page_data["issues"]["nodes"])
        return budget["fetched"] >= 2000

    response = client.create_request(
        query=ISSUES,
        vars={"first": 500},
        stop_on_page=budget_spent,
    )
    result = response.submit()

    if result.success():
        print(f"  Fetched {len(result.data['issues']['nodes'])} issues")
        print(f"  Stop info: {response.stop_info}\n")
    else:
        print(f"  Failed: {result.errors}\n")


# ─── 4. stop_when while streaming ──────────────────────────────────
def example_stop_while_streaming():
    """iterate_nodes never holds more than one page in memory."""
    print("=== stop_when with iterate_nodes ===")

    seen = 0
    for issue in client.iterate_nodes(
        query=ISSUES,
        vars={"first": 500},
        stop_when=lambda n: n["entitySnapshot"]["name"].startswith("legacy-"),
    ):
        seen += 1
    print(f"  Yielded {seen} issues (the match is the last one)\n")


# ─── 5. Async stop_when ────────────────────────────────────────────
async def example_async_stop():
    """Predicates work the same on the async path."""
    print("=== stop_when on an async request ===")

    async with client.async_session() as async_client:
        response = await async_client.create_async_request(
            query=ISSUES,
            vars={"first": 500},
            stop_when=lambda n: n["entitySnapshot"]["name"].startswith("legacy-"),
        )
        result = await response.submit()

    if result.success:
        print(f"  Fetched {len(result.data['issues']['nodes'])} issues")
        print(f"  Stop info: {result.stop_info}\n")
    else:
        print(f"  Failed: {result.errors}\n")


# ─── 6. Resuming from where a query stopped ────────────────────────
def example_resume():
    """Pick the query back up after an early stop.

    stop_info["next_cursor"] is an exact resume point only when the stop
    came from stop_on_page (reason == "page"), because the whole page was
    kept. After a stop_when truncation, resume from stop_info["cursor"] —
    that refetches the stopping page, so de-duplicate by id.
    """
    print("=== Resuming after an early stop ===")

    first = client.create_request(
        query=ISSUES,
        vars={"first": 500},
        stop_when=lambda n: n["entitySnapshot"]["name"].startswith("legacy-"),
    )
    result = first.submit()

    if not result.success() or not first.stopped_early:
        print("  Nothing to resume\n")
        return

    info = first.stop_info
    seen = {n["id"] for n in result.data["issues"]["nodes"]}
    resume_cursor = info["next_cursor"] if info["reason"] == "page" else info["cursor"]

    rest = client.create_request(
        query=ISSUES,
        vars={"first": 500, "after": resume_cursor},
    )
    rest_result = rest.submit()

    if rest_result.success():
        new = [n for n in rest_result.data["issues"]["nodes"] if n["id"] not in seen]
        print(f"  Resumed from {resume_cursor}: {len(new)} additional issues\n")
    else:
        print(f"  Failed: {rest_result.errors}\n")


# ─── Run all examples ──────────────────────────────────────────────
if __name__ == "__main__":
    example_stop_at_date_cutoff()
    example_stop_at_name_match()
    example_stop_on_page()
    example_stop_while_streaming()
    asyncio.run(example_async_stop())
    example_resume()
