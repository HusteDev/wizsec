# wizsec

A Python SDK for the [Wiz](https://www.wiz.io/) Cloud Security GraphQL API.

## Features

- **Sync and async** — unified HTTP transport via `httpx`
- **Automatic pagination** — cursor-based `$after` injection for Relay connection queries
- **Rate limiting** — shared per-environment local limits and server backoff handling
- **Schema validation** — optional client-side query validation against the cached Wiz schema
- **Batch queries** — run multiple queries concurrently with `AsyncWizBatchRequest`
- **Report generation** — create, poll, and stream/download Wiz reports
- **Multi-environment** — use enabled Wiz environments with isolated auth state
- **Serverless ready** — built-in Lambda/serverless support with inline execution

## Quick Example

```python
from wizsec import WizClient, Config

Config.load()
client = WizClient()

response = client.create_request(
    query="""
        query ListProjects($first: Int) {
            projects(first: $first) {
                nodes { id name }
                pageInfo { hasNextPage endCursor }
            }
        }
    """,
    vars={"first": 100},
)
result = response.submit()

if result.success():
    projects = result.data["projects"]["nodes"]
    print(f"Found {len(projects)} projects")
```

!!! note "Automatic pagination"
    The SDK detects `nodes`/`pageInfo` in your query and automatically injects
    `$after` if you didn't include it. All pages are fetched and merged into a
    single result. The default is controlled by `api.auto_paginate`.

## Installation

```bash
pip install wizsec
```
