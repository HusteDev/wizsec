# Async & Batch Queries

## Async Context Manager

Use `async_session()` to run queries asynchronously:

```python
import asyncio
from wizsec import WizClient, Config

Config.load()
client = WizClient()

async def main():
    async with client.async_session() as async_client:
        response = async_client.create_async_request(
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
        result = await response.execute()

        if result.success():
            print(f"Found {len(result.data['projects']['nodes'])} projects")

asyncio.run(main())
```

## Batch Queries

Run multiple queries concurrently:

```python
async def batch_example():
    async with client.async_session() as async_client:
        queries = [
            {
                "query": """
                    query ListProjects($first: Int) {
                        projects(first: $first) {
                            nodes { id name }
                            pageInfo { hasNextPage endCursor }
                        }
                    }
                """,
                "vars": {"first": 50},
            },
            {
                "query": """
                    query ListUsers($first: Int) {
                        users(first: $first) {
                            nodes { id name email }
                            pageInfo { hasNextPage endCursor }
                        }
                    }
                """,
                "vars": {"first": 50},
            },
        ]

        batch = async_client.create_batch_request(queries)
        results = await batch.execute()

        for i, result in enumerate(results):
            if result.success():
                print(f"Query {i}: success")
            else:
                print(f"Query {i}: {result.errors}")

asyncio.run(batch_example())
```

## Shared AsyncClient

Reuse an `httpx.AsyncClient` across multiple sessions:

```python
import httpx

async def shared_client_example():
    async with httpx.AsyncClient() as http_client:
        async with client.async_session(client=http_client) as async_client:
            # Uses the shared http_client
            ...
```
