"""
Reusable GraphQL query collection.

Define your team's standard queries as module-level constants.
The SDK resolves them by name when passed via queryCollection.
"""

# ─── User queries ───────────────────────────────────────────────────

ListUsers = """
    query ListUsers($first: Int) {
        users(first: $first) {
            nodes {
                id
                name
                email
                role
            }
            pageInfo { hasNextPage endCursor }
        }
    }
"""

GetUser = """
    query GetUser($id: ID!) {
        user(id: $id) {
            id
            name
            email
            role
            createdAt
        }
    }
"""

# ─── Project queries ────────────────────────────────────────────────

ListProjects = """
    query ListProjects($first: Int) {
        projects(first: $first) {
            nodes {
                id
                name
                slug
                businessImpact
            }
            pageInfo { hasNextPage endCursor }
        }
    }
"""

GetProject = """
    query GetProject($id: ID!) {
        project(id: $id) {
            id
            name
            slug
            businessImpact
            riskProfile { businessImpact }
        }
    }
"""

# ─── Issue queries ──────────────────────────────────────────────────

ListIssues = """
    query ListIssues($first: Int, $filterBy: IssueFilters) {
        issues(first: $first, filterBy: $filterBy) {
            nodes {
                id
                sourceRule { name }
                severity
                status
                createdAt
            }
            pageInfo { hasNextPage endCursor }
        }
    }
"""

# ─── Service account queries ────────────────────────────────────────

ListServiceAccounts = """
    query ListServiceAccounts($first: Int) {
        serviceAccounts(first: $first) {
            nodes {
                id
                name
                clientId
                createdAt
                lastRotatedAt
            }
            pageInfo { hasNextPage endCursor }
        }
    }
"""
