"""
Progress tracking during pagination.

Demonstrates:
- on_page_event callback for real-time pagination progress
- Spinner/progress display while a sync query runs on the background thread
- Async progress with live updates
"""

import asyncio
import sys
import threading
import time
from wizsec import WizClient, Config

Config.load()
client = WizClient()


# ─── 1. Simple page event callback ─────────────────────────────────
def example_page_callback():
    """Shows the on_page_event callback firing for each page."""
    print("=== Page Event Callback ===")

    def on_page(event):
        page = event["page_info"]["page"]
        per_page = event["page_info"]["per_page"]
        count = len(event["page_data"].get("projects", {}).get("nodes", []))
        print(f"  Page {page}: received {count} items (requesting {per_page} per page)")

    result = client.create_request(
        query="""
            query ListProjects($first: Int) {
                projects(first: $first) {
                    nodes { id name }
                    pageInfo { hasNextPage endCursor }
                }
            }
        """,
        vars={"first": 100},
        on_page_event=on_page,
    ).submit()

    if result.success():
        total = len(result.data["projects"]["nodes"])
        print(f"  Done! Total: {total} projects\n")
    else:
        print(f"  Failed: {result.errors}\n")


# ─── 2. Spinner with progress counter ──────────────────────────────
def example_spinner_progress():
    """Shows a spinner with a running page count while the query paginates."""
    print("=== Spinner with Progress ===")

    progress = {"pages": 0, "items": 0, "done": False}

    def on_page(event):
        progress["pages"] = event["page_info"]["page"]
        key = next(iter(event["page_data"]), None)
        if key:
            progress["items"] += len(event["page_data"][key].get("nodes", []))

    # Submit in a thread so we can animate the spinner
    result_holder = {}

    def run_query():
        result_holder["result"] = client.create_request(
            query="""
                query ListUsers($first: Int) {
                    users(first: $first) {
                        nodes { id name email }
                        pageInfo { hasNextPage endCursor }
                    }
                }
            """,
            vars={"first": 25},
            on_page_event=on_page,
        ).submit()
        progress["done"] = True

    thread = threading.Thread(target=run_query)
    thread.start()

    # Animate while waiting
    spinner = "|/-\\"
    i = 0
    while not progress["done"]:
        char = spinner[i % len(spinner)]
        sys.stdout.write(
            f"\r  {char} Fetching users... "
            f"page {progress['pages']}, "
            f"{progress['items']} items so far"
        )
        sys.stdout.flush()
        i += 1
        time.sleep(0.15)

    thread.join()
    result = result_holder["result"]

    # Clear the spinner line
    sys.stdout.write("\r" + " " * 60 + "\r")

    if result.success():
        total = len(result.data["users"]["nodes"])
        print(f"  Done! Retrieved {total} users across {progress['pages']} pages\n")
    else:
        print(f"  Failed: {result.errors}\n")


# ─── 3. Async progress with live updates ───────────────────────────
async def example_async_progress():
    """Shows async pagination with progress updates."""
    print("=== Async Progress ===")

    pages_fetched = 0

    def on_page(event):
        nonlocal pages_fetched
        pages_fetched = event["page_info"]["page"]
        key = next(iter(event["page_data"]), None)
        if key:
            count = len(event["page_data"][key].get("nodes", []))
            print(f"  Page {pages_fetched}: +{count} items")

    async with client.async_session() as async_client:
        request = await async_client.create_async_request(
            query="""
                query ListProjects($first: Int) {
                    projects(first: $first) {
                        nodes { id name slug }
                        pageInfo { hasNextPage endCursor }
                    }
                }
            """,
            vars={"first": 25},
            on_page_event=on_page,
        )
        result = await request.submit()

    if result.success:
        total = len(result.data["projects"]["nodes"])
        print(f"  Done! {total} projects in {pages_fetched} pages\n")
    else:
        print(f"  Failed: {result.errors}\n")


# ─── Run all examples ──────────────────────────────────────────────
if __name__ == "__main__":
    example_page_callback()
    example_spinner_progress()
    asyncio.run(example_async_progress())
