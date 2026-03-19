"""
Schema validation example.

Demonstrates:
- Enabling query validation against the cached Wiz schema
- Catching validation errors before queries hit the API
- Programmatic validation without creating a request
- Refreshing the cached schema
"""

from wizsec import WizClient, Config, SchemaValidator, WizSchemaValidationError

Config.load()
client = WizClient()


# ─── 1. Enable validation via config ───────────────────────────────
# You can also set this in ~/.wiz/wiz.config under api.validate_queries
Config._CONFIG["api"] = Config._CONFIG.get("api", {})
Config._CONFIG["api"]["validate_queries"] = True


# ─── 2. Valid query — passes validation silently ────────────────────
print("=== Valid Query ===")
try:
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
    print("  Query accepted by validator")
    result = response.submit()
    if result.success:
        print(f"  Retrieved {len(result.data['projects']['nodes'])} projects")
except WizSchemaValidationError as e:
    print(f"  Unexpected validation error: {e}")


# ─── 3. Invalid field — caught before hitting the API ───────────────
print("\n=== Invalid Field (typo) ===")
try:
    response = client.create_request(
        query="""
            query {
                projectz {
                    nodes { id name }
                    pageInfo { hasNextPage endCursor }
                }
            }
        """,
    )
    print("  ERROR: Should have been caught by validator!")
except WizSchemaValidationError as e:
    print(f"  Caught {len(e.validation_errors)} validation error(s):")
    for err in e.validation_errors:
        print(f"    - {err}")


# ─── 4. Invalid subfield ───────────────────────────────────────────
print("\n=== Invalid Subfield ===")
try:
    response = client.create_request(
        query="""
            query ListProjects($first: Int) {
                projects(first: $first) {
                    nodes { id name nonExistentField }
                    pageInfo { hasNextPage endCursor }
                }
            }
        """,
        vars={"first": 10},
    )
    print("  ERROR: Should have been caught!")
except WizSchemaValidationError as e:
    print(f"  Caught: {e.validation_errors[0]}")


# ─── 5. Programmatic validation (no request needed) ────────────────
print("\n=== Programmatic Validation ===")
queries_to_check = [
    ("Valid",   "query { projects { nodes { id } pageInfo { hasNextPage endCursor } } }"),
    ("Invalid", "query { fakeEndpoint { data } }"),
    ("Valid",   "query { users { nodes { id name email } pageInfo { hasNextPage endCursor } } }"),
]

for label, query in queries_to_check:
    try:
        SchemaValidator.validate_query(query, client.environment)
        print(f"  [{label}] PASS: {query[:50]}...")
    except WizSchemaValidationError as e:
        print(f"  [{label}] FAIL: {e.validation_errors[0]}")


# ─── 6. Refreshing the schema cache ────────────────────────────────
print("\n=== Schema Cache Management ===")

# Clear the in-memory cache (disk cache remains)
SchemaValidator.clear(client.environment)
print(f"  Cleared in-memory cache for '{client.environment}'")

# Next validation call reloads from disk (or re-fetches if disk cache was deleted)
try:
    SchemaValidator.validate_query(
        "query { projects { nodes { id } pageInfo { hasNextPage endCursor } } }",
        client.environment,
    )
    print("  Schema reloaded successfully")
except WizSchemaValidationError:
    print("  Schema reloaded (validation ran)")
