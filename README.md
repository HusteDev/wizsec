# wiz-sdk

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
- **Custom query libraries** — resolve query names from importable Python modules
- **PEP 561 typed** (`py.typed` marker included)

## Installation

```bash
pip install wiz-sdk
```

Or install from source:

```bash
git clone https://github.com/HusteDev/wiz-sdk.git
cd wiz-sdk
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

**Option 1 — Environment variables (simplest):**

```bash
export WIZ_CLIENT_ID="your-client-id"
export WIZ_CLIENT_SECRET="your-client-secret"
```

**Option 2 — Credentials file** at `~/.wiz/wiz.credentials`:

```ini
[default]
client_id = your-client-id
client_secret = your-client-secret
environment = app
```

**Option 3 — Pass directly:**

```python
from wiz_sdk import WizClient, Config

Config.load()
client = WizClient(client_id="...", client_secret="...")
```

### Your First Query

```python
from wiz_sdk import WizClient, Config

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

Pagination is handled automatically — results from all pages are merged into `result.data`.

### Query Collections

Resolve query names from a Python module instead of passing raw GraphQL strings:

```python
import my_queries  # module with GraphQL strings as attributes

response = client.create_request(
    queryCollection=my_queries,
    query="ListUsers",  # resolves to my_queries.ListUsers
    vars={"first": 50}
)
```

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
from wiz_sdk import WizClient, Config

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

```python
def on_page(event):
    print(f"Page {event['page_info']['page']} received")

response = client.create_request(
    query="...",
    vars={"first": 500},
    on_page_event=on_page
)
```

## Configuration

The SDK reads `~/.wiz/wiz.config` (YAML). Example:

```yaml
app:
  name: wiz-sdk
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

| Exception | When |
|---|---|
| `WizError` | Base class for all SDK errors |
| `WizAuthenticationError` | Auth flow fails |
| `WizAPIError` | API returns an error (includes `status_code`) |
| `WizCredentialsError` | Credentials missing or invalid |
| `WizConfigurationError` | Config file missing or malformed |
| `WizRateLimitError` | Rate limit exceeded (includes `retry_after`) |
| `WizQueryError` | Invalid GraphQL query (includes `query`, `errors`) |
| `WizReportError` | Report generation/download fails |
| `WizTimeoutError` | Operation timed out |
| `WizFileError` | File I/O error |
| `WizServerlessError` | Serverless-specific failure |

```python
from wiz_sdk import WizAuthenticationError, WizRateLimitError

try:
    result = response.submit()
except WizRateLimitError as e:
    print(f"Rate limited — retry after {e.retry_after}s")
except WizAuthenticationError as e:
    print(f"Auth failed: {e}")
```

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
```

## License

MIT
