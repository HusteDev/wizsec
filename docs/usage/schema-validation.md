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

The cached schema goes stale when Wiz updates their API. Note that step 1 above applies **only when the cache file is absent** — nothing re-introspects as the file ages, so a stale cache stays stale until you refresh it.

Use the CLI:

```bash
wizsec schema refresh                     # re-introspect and rewrite the cache
wizsec schema refresh --environment gov   # a specific environment
wizsec schema path                        # where the cache lives
wizsec schema clear                       # delete cached schemas
```

`wizsec schema refresh` authenticates the same way `wizsec creds test` does, so it also accepts `--profile`.

You do not have to track the age yourself. `wizsec doctor` reports it:

```
[WARN] schema cache schema_gov.json: 47 day(s) old (run 'wizsec schema refresh')
```

and the SDK logs a warning when it loads a cache older than 30 days:

```
Cached schema for 'gov' is 47 days old — run 'wizsec schema refresh' to update it
```

### Serverless deployments

A serverless bundle is read-only and cannot introspect at runtime, so generate the schema ahead of time and ship it with your code:

```bash
wizsec schema refresh --environment app --output ./bundle/schema_app.json
```

With `--output` the schema is written only to that path, leaving your own `~/.wiz` cache untouched.

### Clearing in-process state

`SchemaValidator.clear()` drops the **in-memory** schema only — it does not delete the cache file, so the next lookup reloads the same file from disk. Use it to pick up a schema file that changed underneath a long-running process, not to force a re-introspection:

```python
from wizsec import SchemaValidator

SchemaValidator.clear("gov")   # forget the in-memory schema for one environment
SchemaValidator.clear()        # forget all of them
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
