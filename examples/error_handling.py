"""
Error handling example.

Demonstrates:
- Catching specific SDK exceptions
- Inspecting error details (status codes, original errors)
- Graceful degradation patterns
"""

from wiz_sdk import (
    WizClient,
    Config,
    WizAuthenticationError,
    WizCredentialsError,
    WizRateLimitError,
    WizQueryError,
    WizTimeoutError,
    WizAPIError,
)

# --- Startup errors ---
# Catch config and credential problems early.

try:
    Config.load()
    client = WizClient()
except WizCredentialsError as e:
    print(f"Credentials not found: {e}")
    print("Set WIZ_CLIENT_ID and WIZ_CLIENT_SECRET, or configure ~/.wiz/wiz.credentials")
    exit(1)
except WizAuthenticationError as e:
    print(f"Authentication failed: {e}")
    if e.original_error:
        print(f"  Caused by: {e.original_error}")
    exit(1)


# --- Query errors ---
# The result object captures errors without raising, so check result.success.

response = client.create_request(
    query="{ users(first: 10) { nodes { name } } }",
    vars={},
)
result = response.submit()

if not result.success:
    for error in result.errors:
        print(f"GraphQL error: {error.get('message')}")


# --- Exception-based error handling ---
# Some errors (auth, rate limits) are raised as exceptions.

try:
    response = client.create_request(
        query="{ projects { nodes { id } } }",
        vars={},
    )
    result = response.submit()

except WizRateLimitError as e:
    print(f"Rate limited — retry after {e.retry_after}s")

except WizTimeoutError as e:
    print(f"Request timed out: {e}")

except WizAPIError as e:
    print(f"API error: {e}")
    if e.status_code:
        print(f"  HTTP status: {e.status_code}")
    if e.original_error:
        print(f"  Caused by: {e.original_error}")
