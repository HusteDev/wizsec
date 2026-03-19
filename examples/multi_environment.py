"""
Multi-environment and multi-profile example.

Demonstrates:
- Connecting to different Wiz tenants (app, gov)
- Using multiple credential profiles on the same tenant
- How shared-environment rate limiting works automatically
"""

from wizsec import WizClient, Config

Config.load()

# --- Different environments ---
# Each environment gets its own request queue and rate limiter.

app_client = WizClient(environment="app")
gov_client = WizClient(environment="gov")

# These two clients operate completely independently.
app_result = app_client.create_request(
    query="{ users(first: 5) { nodes { name } } }",
    vars={},
).submit()

gov_result = gov_client.create_request(
    query="{ users(first: 5) { nodes { name } } }",
    vars={},
).submit()


# --- Multiple profiles on the same environment ---
# Both clients share the same queue and rate limiter (since they
# target the same environment), but authenticate with separate
# credentials.

admin_client = WizClient(environment="app", profile="admin")
reader_client = WizClient(environment="app", profile="readonly")

# Admin performs a mutation
admin_result = admin_client.create_request(
    query="""
        mutation UpdateProject($id: ID!, $name: String!) {
            updateProject(input: { id: $id, patch: { name: $name } }) {
                project { id name }
            }
        }
    """,
    vars={"id": "proj-123", "name": "Renamed Project"},
    paginate=False,
).submit()

# Reader queries the same data
reader_result = reader_client.create_request(
    query="""
        query GetProject($id: ID!) {
            project(id: $id) { id name }
        }
    """,
    vars={"id": "proj-123"},
    paginate=False,
).submit()

if admin_result.success:
    print(f"Admin updated: {admin_result.data}")
if reader_result.success:
    print(f"Reader sees:   {reader_result.data}")
