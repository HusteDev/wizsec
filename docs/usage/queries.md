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
