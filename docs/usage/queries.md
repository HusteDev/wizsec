# Basic Queries

## Single Query

```python
from wizsec import WizClient, Config

Config.load()
client = WizClient()

response = client.create_request(
    query="""
        query ListUsers($first: Int) {
            users(first: $first) {
                nodes { id name email }
                pageInfo { hasNextPage endCursor }
            }
        }
    """,
    vars={"first": 100},
)
result = response.submit()

if result.success():
    users = result.data["users"]["nodes"]
    print(f"Retrieved {len(users)} users")
```

## Automatic Pagination

The SDK automatically handles cursor-based pagination for queries using the Relay connection pattern (`nodes` + `pageInfo`). You don't need to include `$after` in your query — the SDK detects the pattern and injects it.

All pages are fetched and merged into a single `nodes` list in the result.

To disable pagination for a specific query:

```python
response = client.create_request(
    query="...",
    paginate=False,
)
```

## Page Events

Monitor pagination progress with a callback:

```python
def on_page(event):
    page_info = event["page_info"]
    print(f"Page {page_info['page']}, {page_info['per_page']} per page")

response = client.create_request(
    query="...",
    vars={"first": 100},
    on_page_event=on_page,
)
result = response.submit()
```

## Stopping Early

Two optional predicates end pagination before the last page:

- `stop_when(node)` — called for each result node in order. The matching node is kept; the rest of its page is dropped and no further pages are fetched.
- `stop_on_page(page_data)` — called once per fetched page with the raw GraphQL data. The whole page is kept, then pagination stops.

```python
response = client.create_request(
    query="...",
    vars={"first": 500},
    stop_when=lambda node: node["name"].startswith("legacy-"),
)
result = response.submit()

result.success            # still True — stopping early is not an error
response.stopped_early    # True
response.stop_info        # {"reason": "node", "page": 3, "cursor": ..., "next_cursor": ...}
```

Given a page of `[a, b, MATCH, d, e]`, the aggregated node list ends at `MATCH`.

The same predicates work on the streaming iterators:

```python
for issue in client.iterate_nodes(
    query="...", vars={"first": 500}, stop_when=lambda n: n["id"] == target
):
    process(issue)   # the matching node is the last one yielded
```

and on batch requests, where each request stops its own pagination:

```python
batch = client.create_batch_request()
batch.add_request(query="...", stop_on_page=lambda page: budget_exceeded())
results = batch.submit()
```

`stop_info["next_cursor"]` is an exact resume point only when `reason == "page"`; after a `stop_when` truncation, resume from `stop_info["cursor"]` and de-duplicate by id. `totalCount` is left as the server-side total. A predicate that raises is reported as a `WizQueryError` rather than silently returning partial results, and setting either predicate disables query splitting for that request.

## Query Collections

Organize reusable queries in a module:

```python
# my_queries.py
LIST_PROJECTS = """
    query ListProjects($first: Int) {
        projects(first: $first) {
            nodes { id name }
            pageInfo { hasNextPage endCursor }
        }
    }
"""
```

```python
import my_queries

response = client.create_request(
    queryCollection=my_queries,
    query="LIST_PROJECTS",
    vars={"first": 100},
)
```

`queryCollection` accepts a module, a module name string (auto-imported), or any object whose attributes are the query strings. For one-off scripts where a dedicated `queries.py` module is overkill, a `types.SimpleNamespace` works just as well:

```python
from types import SimpleNamespace

queries = SimpleNamespace(
    LIST_PROJECTS="query ListProjects($first: Int) { projects(first: $first) { nodes { id } } }",
)

response = client.create_request(
    queryCollection=queries,
    query="LIST_PROJECTS",
    vars={"first": 100},
)
```

Dataclass instances and plain class instances work the same way — anything with attributes named after your queries.
