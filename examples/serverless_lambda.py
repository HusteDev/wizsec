"""
AWS Lambda / serverless example.

Demonstrates:
- Serverless mode (no background threads, inline execution)
- Passing credentials directly (common in Lambda via env vars or Secrets Manager)
- Cleanup after each invocation
"""

import os
from wizsec import WizClient, Config


def handler(event, context):
    """AWS Lambda entry point."""

    # Load config from /var/task/.wiz/wiz.config (packaged with deployment)
    Config.load()

    # Create a serverless client — skips background worker threads
    client = WizClient(
        environment="app",
        client_id=os.environ["WIZ_CLIENT_ID"],
        client_secret=os.environ["WIZ_CLIENT_SECRET"],
        serverless=True,
    )

    try:
        # Pagination still works in serverless mode — it just runs inline
        # instead of through the background queue.
        result = client.create_request(
            query="""
                query CriticalIssues($first: Int, $after: String) {
                    issues(
                        first: $first
                        after: $after
                        filterBy: { severity: [CRITICAL] status: [OPEN] }
                    ) {
                        nodes { id sourceRule { name } severity }
                        pageInfo { hasNextPage endCursor }
                    }
                }
            """,
            vars={"first": 500},
        ).submit()

        if result.success:
            issues = result.data["issues"]["nodes"]
            return {
                "statusCode": 200,
                "body": {
                    "critical_issue_count": len(issues),
                    "issues": issues[:10],  # return first 10 as sample
                },
            }
        else:
            return {
                "statusCode": 500,
                "body": {"errors": result.errors},
            }

    finally:
        # Release per-environment and per-profile state so the next
        # Lambda invocation starts clean.
        client.cleanup_for_lambda()
