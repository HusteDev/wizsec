"""
Query collection example.

Demonstrates:
- Organizing reusable GraphQL queries in a separate module (queries.py)
- Resolving query names from the collection by string reference
- Mixing paginated and non-paginated queries from the same collection
"""

from wizsec import WizClient, Config

# Import the query collection module
from . import queries

Config.load()
client = WizClient()


# ─── Resolve a query by name ───────────────────────────────────────
# Pass the module as queryCollection and reference queries by attribute name.
# The SDK looks up queries.ListUsers automatically.

print("=== List Users (by name from collection) ===")
response = client.create_request(
    queryCollection=queries,
    query="ListUsers",
    vars={"first": 10},
)
result = response.submit()

if result.success:
    for user in result.data["users"]["nodes"]:
        print(f"  {user['name']} — {user['email']} ({user['role']})")


# ─── Non-paginated lookup ──────────────────────────────────────────
# Single-record lookups don't have nodes/pageInfo, so pagination is disabled.

print("\n=== Get Project (single record) ===")
response = client.create_request(
    queryCollection=queries,
    query="GetProject",
    vars={"id": "some-project-id"},
    paginate=False,
)
result = response.submit()

if result.success:
    project = result.data["project"]
    print(f"  {project['name']} — impact: {project['riskProfile']['businessImpact']}")


# ─── Paginated issues with filters ─────────────────────────────────

print("\n=== List Critical Issues ===")
response = client.create_request(
    queryCollection=queries,
    query="ListIssues",
    vars={
        "first": 50,
        "filterBy": {"severity": ["CRITICAL"]},
    },
)
result = response.submit()

if result.success:
    issues = result.data["issues"]["nodes"]
    print(f"  Found {len(issues)} critical issues")
    for issue in issues[:5]:
        print(
            f"  [{issue['severity']}] {issue['sourceRule']['name']} — {issue['status']}"
        )


# ─── You can also pass the query string directly ───────────────────
# The queryCollection is optional — you can always inline a query.

print("\n=== Direct query (no collection) ===")
response = client.create_request(
    query=queries.ListServiceAccounts,  # pass the string directly
    vars={"first": 5},
)
result = response.submit()

if result.success:
    for sa in result.data["serviceAccounts"]["nodes"]:
        print(f"  {sa['name']} (client: {sa['clientId']})")
