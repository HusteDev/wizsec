# wizsec

[![CI](https://github.com/HusteDev/wizsec/actions/workflows/ci.yml/badge.svg)](https://github.com/HusteDev/wizsec/actions/workflows/ci.yml)
[![CodeQL](https://github.com/HusteDev/wizsec/actions/workflows/codeql.yml/badge.svg)](https://github.com/HusteDev/wizsec/actions/workflows/codeql.yml)
[![PyPI version](https://img.shields.io/pypi/v/wizsec)](https://pypi.org/project/wizsec/)
[![Python versions](https://img.shields.io/pypi/pyversions/wizsec)](https://pypi.org/project/wizsec/)
[![License](https://img.shields.io/pypi/l/wizsec)](https://github.com/HusteDev/wizsec/blob/main/LICENSE)
[![codecov](https://codecov.io/gh/HusteDev/wizsec/graph/badge.svg)](https://codecov.io/gh/HusteDev/wizsec)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

A Python SDK for the [Wiz](https://www.wiz.io/) Cloud Security GraphQL API. Provides sync and async clients with automatic pagination, rate limiting, batch operations, and report generation.

## Features

- **Unified HTTP transport** via [httpx](https://www.python-httpx.org/) (sync and async)
- **Automatic cursor-based pagination** with result merging
- **Per-environment rate limiting** using `pyrate-limiter` (respects Wiz's global rate limits)
- **Batch requests** — submit multiple queries concurrently (sync threads or async tasks)
- **Report generation** — create, poll, stream, and download Wiz reports (JSON and CSV)
- **Multiple auth flows** — client credentials and device code (OAuth)
- **Flexible credential storage** — environment variables, credential files, or interactive prompt
- **Multi-environment / multi-profile** — connect to `app`, `gov`, or custom Wiz tenants with separate credential profiles
- **Serverless support** — optimized for AWS Lambda and similar environments
- **YAML configuration** via `~/.wiz/wiz.config`
- **Client-side schema validation** — catch query typos before they hit the API
- **Custom query libraries** — resolve query names from importable Python modules
- **PEP 561 typed** (`py.typed` marker included)

## Installation

Install from source:

```bash
git clone https://github.com/HusteDev/wizsec.git
cd wizsec
pip install .
```

For development:

```bash
pip install -e ".[dev]"
```

## Requirements

- Python >= 3.9
- `httpx`, `pyrate-limiter`, `python-dotenv`, `PyYAML`, `graphql-core`

## Quick Start

### Authentication Setup

The SDK supports two OAuth grant types:

| Grant Type | Use Case | Requires |
|---|---|---|
| `client_credentials` | Service accounts, automation, CI/CD | Client ID + Secret |
| `device_code` | Interactive / user-based sessions | Browser + WizCode license |

#### Client Credentials (default)

Provide your client ID and secret via environment variables, a credentials file, or constructor arguments.

**Environment variables (simplest):**

```bash
export WIZ_CLIENT_ID="your-client-id"
export WIZ_CLIENT_SECRET="your-client-secret"
```

**Credentials file** at `~/.wiz/wiz.credentials`:

```ini
[default]
client_id = your-client-id
client_secret = your-client-secret
environment = app
```

**Pass directly:**

```python
from wizsec import WizClient, Config

Config.load()
client = WizClient(client_id="...", client_secret="...")
```

#### Device Code (Interactive)

Device code auth opens a browser for the user to authorize the session — no client secret needed. This is ideal for CLI tools, notebooks, or any context where a human is present.

Set the grant type in `~/.wiz/wiz.config`:

```yaml
auth:
  grant_type: device_code
  device:
    quiet: true       # auto-authorize without extra prompts (default: true)
    poll_time: 5       # seconds between auth status checks (default: 5)
```

Then use the client normally — the browser will open automatically:

```python
from wizsec import WizClient, Config

Config.load()
client = WizClient(environment="app")  # opens browser for authorization
result = client.create_request(query="...", vars={}).submit()
```

The SDK polls the auth endpoint until the user completes authorization or the request times out.

### Your First Query

```python
from wizsec import WizClient, Config

Config.load()
client = WizClient(environment="app")

response = client.create_request(
    query='{ users(first: 10) { nodes { name email } pageInfo { hasNextPage endCursor } } }',
    vars={}
)
result = response.submit()

if result.success:
    print(result.data)
else:
    print(result.errors)
```

## Usage

### Single Queries

```python
response = client.create_request(query="...", vars={"first": 100})
result = response.submit()
```

Pagination is handled automatically — the SDK detects queries using the [Relay connection pattern](https://relay.dev/graphql/connections.htm) (`nodes` + `pageInfo { hasNextPage endCursor }`) and injects the `$after` cursor variable for you. Results from all pages are merged into `result.data`.

You don't need to declare `$after` in your query — the SDK adds it when:
- The operation is a **query** (not a mutation)
- The query selects both `nodes` and `pageInfo` subfields
- `$after` isn't already declared

If you set `paginate=False`, no injection occurs and only the first page is returned.

### Query Collections

Organize reusable GraphQL queries in a Python module and reference them by name. This keeps queries out of your application logic and makes them shareable across scripts.

**`queries.py`** — define queries as module-level constants:

```python
ListUsers = """
    query ListUsers($first: Int) {
        users(first: $first) {
            nodes { id name email role }
            pageInfo { hasNextPage endCursor }
        }
    }
"""

GetProject = """
    query GetProject($id: ID!) {
        project(id: $id) { id name slug riskProfile { businessImpact } }
    }
"""
```

**`main.py`** — resolve by name or pass the string directly:

```python
import queries

# Resolve by attribute name from the collection
response = client.create_request(
    queryCollection=queries,
    query="ListUsers",  # resolves to queries.ListUsers
    vars={"first": 50}
)

# Or pass the query string directly (no collection needed)
response = client.create_request(
    query=queries.GetProject,
    vars={"id": "some-project-id"},
    paginate=False,
)
```

See [`examples/query_collection/`](examples/query_collection/) for a complete working example.

### Batch Requests (Sync)

```python
batch = client.create_batch_request()
batch.add_request(query="...", vars={"type": "VM"})
batch.add_request(query="...", vars={"type": "CONTAINER"})

batch.set_progress_callback(lambda done, total: print(f"{done}/{total}"))
results = batch.submit(max_concurrent=5)

print(f"{results.success_count()}/{results.total_count()} succeeded")

for request_id, response in results:
    if response.success:
        print(response.data)
```

### Async Requests

```python
import asyncio
from wizsec import WizClient, Config

async def main():
    Config.load()
    client = WizClient(environment="app")

    async with client.async_session() as async_client:
        response = await async_client.create_async_request(
            query="...", vars={"first": 100}
        )
        result = await response.submit()
        print(result.data)

asyncio.run(main())
```

### Async Batch Requests

```python
async with client.async_session() as async_client:
    batch = await async_client.create_async_batch_request()
    batch.add_request(query="...", vars={"type": "VM"})
    batch.add_request(query="...", vars={"type": "CONTAINER"})

    results = await batch.submit(max_concurrent=50)
    print(results.success_rate())
```

### Sync vs Async: When to Use Each

The SDK provides both synchronous and asynchronous interfaces. Choose based on your use case:

| Approach  | Best For                                                                 |
| --------- | ------------------------------------------------------------------------ |
| **Sync**  | Simple scripts, single queries, CLI tools, quick prototypes              |
| **Async** | Multiple independent queries, high-throughput applications, web services |

**Performance comparison** (3 queries fetching Projects, Users, and Service Accounts):

```
SYNC (sequential)    2.57s   — queries run one after another
ASYNC (concurrent)   0.60s   — queries run in parallel
```

Async achieves ~4x speedup here because all three API calls happen concurrently instead of waiting for each to complete.

**Use sync when:**

- Running a single query or a few dependent queries
- Writing simple scripts or one-off tools
- Code simplicity matters more than throughput

**Use async when:**

- Fetching data from multiple independent queries
- Building web applications or services that need to handle concurrent requests
- Performance is critical and queries don't depend on each other
- Working with `asyncio`-based frameworks (FastAPI, aiohttp, etc.)

See [`examples/sync_vs_async.py`](examples/sync_vs_async.py) for a runnable comparison.

### Report Generation

```python
response = client.create_request(
    query="mutation { createReport(...) { report { id } } }",
    report_request={"name": "my-report", "stream": True}
)
result = response.submit()

# Report data is automatically polled, downloaded, and attached:
report_rows = result.data.get("report_data", [])
```

### Progress Tracking

Monitor pagination progress with a callback that fires after each page is fetched. The `on_page_event` callback receives a dict with:

| Key | Type | Description |
|-----|------|-------------|
| `page_data` | `dict` | Raw GraphQL data from the current page |
| `page_info` | `dict` | `{"page": int, "per_page": int}` — current page number and page size |
| `errors` | `list` | Any errors accumulated so far |

**Simple progress logging:**

```python
def on_page(event):
    page = event["page_info"]["page"]
    per_page = event["page_info"]["per_page"]
    key = next(iter(event["page_data"]), None)
    count = len(event["page_data"][key]["nodes"]) if key else 0
    print(f"Page {page}: received {count}/{per_page} items")

response = client.create_request(
    query="...",
    vars={"first": 500},
    on_page_event=on_page
)
result = response.submit()
```

**Spinner with live counter** (runs the query on the background thread while animating in the main thread):

```python
import sys, time, threading

progress = {"pages": 0, "items": 0, "done": False}

def on_page(event):
    progress["pages"] = event["page_info"]["page"]
    key = next(iter(event["page_data"]), None)
    if key:
        progress["items"] += len(event["page_data"][key].get("nodes", []))

result_holder = {}

def run_query():
    result_holder["result"] = client.create_request(
        query="...", vars={"first": 100}, on_page_event=on_page
    ).submit()
    progress["done"] = True

thread = threading.Thread(target=run_query)
thread.start()

spinner = "|/-\\"
i = 0
while not progress["done"]:
    sys.stdout.write(f"\r  {spinner[i % 4]} page {progress['pages']}, {progress['items']} items")
    sys.stdout.flush()
    i += 1
    time.sleep(0.15)
thread.join()
```

Progress tracking also works with async requests and report streaming. For reports, the callback receives `{"name", "total_size", "downloaded", "status"}` instead of page data.

See [`examples/progress_tracking.py`](examples/progress_tracking.py) for complete sync, spinner, and async examples.

### Schema Validation

Validate GraphQL queries against the Wiz schema before they hit the API. Catches typos and invalid fields early with helpful suggestions:

```python
from wizsec import WizClient, Config, SchemaValidator, WizSchemaValidationError

Config.load()
Config._CONFIG.setdefault("api", {})["validate_queries"] = True  # or set in wiz.config

client = WizClient()

# Typos are caught before the request is sent
try:
    client.create_request(query="query { projectz { nodes { id } } }")
except WizSchemaValidationError as e:
    print(e.validation_errors[0])
    # "Cannot query field 'projectz' on type 'Query'. Did you mean 'project', 'projects', or 'projectTags'?"
```

Validate queries programmatically without creating a request:

```python
try:
    SchemaValidator.validate_query("query { fakeEndpoint { data } }", "app")
except WizSchemaValidationError as e:
    print(e.validation_errors[0])
    # "Cannot query field 'fakeEndpoint' on type 'Query'. Did you mean 'apiEndpoint'?"
```

The schema is cached locally at `~/.wiz/schema_<env>.json` and reloaded automatically.

See [`examples/schema_validation.py`](examples/schema_validation.py) for more examples.

## Rate Limiting

The SDK automatically enforces Wiz's API rate limits so you don't have to think about throttling. Rate limiters are shared across all `WizClient` instances on the same environment — even if you create multiple clients, they coordinate through a single limiter.

Limits are applied per request type (query vs. mutation) and account type (user vs. service account), matching [Wiz's published rate limits](https://docs.wiz.io/wiz-docs/docs/rate-limiting).

If a rate limit is hit, the SDK waits and retries automatically. You only need to handle `WizRateLimitError` if retries are exhausted.

## Configuration

The SDK reads `~/.wiz/wiz.config` (YAML). Example:

```yaml
app:
  name: wizsec
  release: "1.0.0"

auth:
  grant_type: client_credentials
  credential_file: ~/.wiz/wiz.credentials
  storage_method: file

api:
  timeout: 60
  max_retries: 3
  retry_time: 2

logging:
  level: INFO
```

Config can also be set via `Config.load(overrides=["api.timeout=120"])`.

## Multi-Environment & Multi-Profile

```python
# Different Wiz tenants
app_client = WizClient(environment="app")
gov_client = WizClient(environment="gov")

# Different credential profiles on the same tenant
admin = WizClient(environment="app", profile="admin")
readonly = WizClient(environment="app", profile="readonly")
```

Clients sharing the same environment automatically share a single request queue and rate limiter.

## Serverless / Lambda

Set `WIZ_SERVERLESS=1` or deploy to an environment with `AWS_LAMBDA_FUNCTION_NAME` set. The SDK adapts automatically:

- Disables background worker threads (executes inline)
- Reads config from `/var/task/.wiz/`
- Call `client.cleanup_for_lambda()` at the end of each invocation

```python
def handler(event, context):
    Config.load()
    client = WizClient(environment="app", serverless=True)
    try:
        result = client.create_request(query="...", vars={}).submit()
        return result.data
    finally:
        client.cleanup_for_lambda()
```

## Error Handling

The SDK provides a structured exception hierarchy:

| Exception                    | When                                                         |
| ---------------------------- | ------------------------------------------------------------ |
| `WizError`                   | Base class for all SDK errors                                |
| `WizAuthenticationError`     | Auth flow fails                                              |
| `WizAPIError`                | API returns an error (includes `status_code`)                |
| `WizCredentialsError`        | Credentials missing or invalid                               |
| `WizConfigurationError`      | Config file missing or malformed                             |
| `WizRateLimitError`          | Rate limit exceeded (includes `retry_after`)                 |
| `WizQueryError`              | Invalid GraphQL query (includes `query`, `errors`)           |
| `WizSchemaValidationError`   | Query fails schema validation (includes `validation_errors`) |
| `WizReportError`             | Report generation/download fails                             |
| `WizTimeoutError`            | Operation timed out                                          |
| `WizFileError`               | File I/O error                                               |
| `WizServerlessError`         | Serverless-specific failure                                  |

```python
from wizsec import WizAuthenticationError, WizRateLimitError

try:
    result = response.submit()
except WizRateLimitError as e:
    print(f"Rate limited — retry after {e.retry_after}s")
except WizAuthenticationError as e:
    print(f"Auth failed: {e}")
```

## Development

```bash
pip install -e ".[dev]"     # install with dev + docs dependencies
python -m pytest tests/ -q  # run tests
```

### Documentation

API docs are built with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/):

```bash
pip install -e ".[docs]"
mkdocs serve                # live preview at http://127.0.0.1:8000
mkdocs build                # static site in site/
```

## License

MIT
