# Schema Validation

The SDK can validate your GraphQL queries against the Wiz API schema before sending them, catching typos and invalid fields at development time instead of getting cryptic 400 errors from the API.

## Enabling Validation

Add to your `~/.wiz/wiz.config`:

```yaml
api:
  validate_queries: true
```

## How It Works

1. On first use, the SDK runs an introspection query to fetch the full Wiz schema
2. The schema is cached to `{app.wiz_dir}/schema_{environment}.json`
3. Every query is validated against the schema before being sent

If validation fails, a `WizSchemaValidationError` is raised with details:

```python
from wizsec import WizClient, Config, WizSchemaValidationError

Config.load()
client = WizClient()

try:
    response = client.create_request(
        query="""
            query {
                projectz {
                    nodes { id }
                    pageInfo { hasNextPage endCursor }
                }
            }
        """,
    )
except WizSchemaValidationError as e:
    print(e)
    # Query validation failed with 1 error(s):
    #   - Cannot query field 'projectz' on type 'Query'.
    for err in e.validation_errors:
        print(err)
```

## Refreshing the Schema

The cached schema may become outdated when Wiz updates their API. To refresh:

```python
from wizsec import SchemaValidator

# Clear cached schema for a specific environment
SchemaValidator.clear("gov")

# Clear all cached schemas
SchemaValidator.clear()
```

Delete the cache file from the configured `app.wiz_dir` to force a fresh introspection on next use:

```bash
rm ~/.wiz/schema_gov.json  # or {app.wiz_dir}/schema_gov.json if wiz_dir is customized
```

## Programmatic Validation

You can validate queries without creating a request:

```python
from wizsec import SchemaValidator

SchemaValidator.validate_query(
    "query { projects { nodes { id } pageInfo { hasNextPage endCursor } } }",
    environment="gov",
)
# Raises WizSchemaValidationError if invalid, returns None if valid
```
