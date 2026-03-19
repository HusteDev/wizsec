"""
Async query examples.

Demonstrates:
- Using async_session() for single async requests
- Using AsyncWizBatchRequest for high-concurrency async batches
"""

import asyncio
from wiz_sdk import WizClient, Config


async def single_async_query():
    """Run a single query asynchronously."""
    Config.load()
    client = WizClient()

    async with client.async_session() as async_client:
        response = await async_client.create_async_request(
            query="""
                query ListProjects($first: Int, $after: String) {
                    projects(first: $first, after: $after) {
                        nodes { id name }
                        pageInfo { hasNextPage endCursor }
                    }
                }
            """,
            vars={"first": 50},
        )
        result = await response.submit()

        if result.success:
            projects = result.data["projects"]["nodes"]
            print(f"Found {len(projects)} projects")
        else:
            print(f"Failed: {result.errors}")


async def async_batch():
    """Run many queries concurrently with async batching."""
    Config.load()
    client = WizClient()

    async with client.async_session() as async_client:
        batch = await async_client.create_async_batch_request()

        # Add a bunch of queries — async batches can handle high concurrency
        severities = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL"]
        for severity in severities:
            batch.add_request(
                query="""
                    query IssuesBySeverity($first: Int, $after: String, $severity: [IssueSeverity!]) {
                        issues(first: $first, after: $after, filterBy: { severity: $severity }) {
                            nodes { id sourceRule { name } }
                            pageInfo { hasNextPage endCursor }
                        }
                    }
                """,
                vars={"first": 25, "severity": [severity]},
            )

        # Submit with up to 50 concurrent requests
        results = await batch.submit(max_concurrent=50)

        for i, (request_id, response) in enumerate(results):
            label = severities[i]
            if response.success:
                count = len(response.data.get("issues", {}).get("nodes", []))
                print(f"  {label}: {count} issues")
            else:
                print(f"  {label}: FAILED")


if __name__ == "__main__":
    print("=== Single Async Query ===")
    asyncio.run(single_async_query())

    print("\n=== Async Batch ===")
    asyncio.run(async_batch())
