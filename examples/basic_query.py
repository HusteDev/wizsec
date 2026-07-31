"""
Basic single query example.

Demonstrates:
- Initializing Config and WizClient
- Running a single GraphQL query with automatic pagination
- Checking success and reading results
"""

from wizsec import WizClient, Config

# Load configuration from ~/.wiz/wiz.config
Config.load()

# Create a client (defaults to environment from config, profile "default")
# Uses default environment from ~/.wiz/wiz.config
client = WizClient()

# Build and submit a paginated query.
# Pagination defaults to api.auto_paginate in ~/.wiz/wiz.config.
# Pass paginate=False to create_request() to fetch only the first page.
response = client.create_request(
    query="""
        query ListUsers($first: Int, $after: String) {
            users(first: $first, after: $after) {
                nodes {
                    id
                    name
                    email
                }
                pageInfo {
                    hasNextPage
                    endCursor
                }
            }
        }
    """,
    vars={"first": 100},
)
result = response.submit()

if result.success:
    users = result.data["users"]["nodes"]
    print(f"Retrieved {len(users)} users")
    for user in users[:5]:
        print(f"  {user['name']} — {user['email']}")
else:
    print("Query failed:")
    for error in result.errors:
        print(f"  {error['message']}")
