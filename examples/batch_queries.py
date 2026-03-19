"""
Batch query example.

Demonstrates:
- Submitting multiple queries concurrently via WizBatchRequest
- Progress tracking with a callback
- Inspecting individual results from WizBatchResponse
"""

from wiz_sdk import WizClient, Config

Config.load()
client = WizClient()

# Create a batch and add several queries
batch = client.create_batch_request()

# Define queries to run concurrently
QUERIES = [
    {
        "label": "Projects",
        "query": """
            query ListProjects($first: Int) {
                projects(first: $first) {
                    nodes { id name }
                    pageInfo { hasNextPage endCursor }
                }
            }
        """,
        "vars": {"first": 50},
        "key": "projects",
    },
    {
        "label": "Users",
        "query": """
            query ListUsers($first: Int) {
                users(first: $first) {
                    nodes { id name email }
                    pageInfo { hasNextPage endCursor }
                }
            }
        """,
        "vars": {"first": 50},
        "key": "users",
    },
    {
        "label": "Service Accounts",
        "query": """
            query ListServiceAccounts($first: Int) {
                serviceAccounts(first: $first) {
                    nodes { id name }
                    pageInfo { hasNextPage endCursor }
                }
            }
        """,
        "vars": {"first": 50},
        "key": "serviceAccounts",
    },
    {
        "label": "Connectors",
        "query": """
            query ListConnectors($first: Int) {
                connectors(first: $first) {
                    nodes { id name type { name } }
                    pageInfo { hasNextPage endCursor }
                }
            }
        """,
        "vars": {"first": 50},
        "key": "connectors",
    },
]

for q in QUERIES:
    batch.add_request(query=q["query"], vars=q["vars"])

# Optional: track progress
batch.set_progress_callback(lambda done, total: print(f"Progress: {done}/{total}"))

# Submit all at once (max 5 concurrent through the queue)
results = batch.submit(max_concurrent=5)

print(f"\n{results.success_count()}/{results.total_count()} succeeded")
print(f"Success rate: {results.success_rate():.0f}%")

# Iterate results by request ID
for request_id, response in results:
    q = QUERIES[request_id]
    if response.success:
        nodes = response.data.get(q["key"], {}).get("nodes", [])
        print(f"  [{q['label']}] {len(nodes)} results")
    else:
        print(f"  [{q['label']}] FAILED: {response.errors}")
