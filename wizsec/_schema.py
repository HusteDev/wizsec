##
##
##    SSSSS    CCCCC   H     H   EEEEE   M     M     A
##   S        C        H     H   E       MM   MM    A   A
##    SSSSS   C        HHHHHHH   EEEEE   M M M M   AAAAAAA
##        S   C        H     H   E       M  M  M   A     A
##    SSSSS    CCCCC   H     H   EEEEE   M     M   A     A
##
##
##

# _schema.py
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from graphql import (
    parse as gql_parse,
    validate,
    build_client_schema,
    GraphQLSchema,
    IntrospectionQuery,
)

from .config import Config, DEFAULT_WIZ_DIR
from .exceptions import WizSchemaValidationError

logger = logging.getLogger("wizsec._schema")

# Standard introspection query used to fetch the schema from the API
INTROSPECTION_QUERY = """
query IntrospectionQuery {
  __schema {
    queryType { name }
    mutationType { name }
    subscriptionType { name }
    types {
      ...FullType
    }
  }
}

fragment FullType on __Type {
  kind
  name
  description
  fields(includeDeprecated: true) {
    name
    description
    args { ...InputValue }
    type { ...TypeRef }
    isDeprecated
    deprecationReason
  }
  inputFields { ...InputValue }
  interfaces { ...TypeRef }
  enumValues(includeDeprecated: true) {
    name
    description
    isDeprecated
    deprecationReason
  }
  possibleTypes { ...TypeRef }
}

fragment InputValue on __InputValue {
  name
  description
  type { ...TypeRef }
  defaultValue
}

fragment TypeRef on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType {
        kind
        name
        ofType {
          kind
          name
        }
      }
    }
  }
}
"""


class SchemaValidator:
    """Validates GraphQL queries against a cached Wiz API schema.

    Schema is cached per-environment in ~/.wiz/schema_{env}.json.
    If no cached schema exists and a WizClient is available, the schema
    is automatically fetched via introspection and cached to disk.
    """

    _schemas: dict[str, GraphQLSchema] = {}
    _lock = threading.Lock()
    _fetching: set[str] = set()  # prevents recursive introspection

    @classmethod
    def get_schema(
        cls, environment: str, client: Any = None
    ) -> Optional[GraphQLSchema]:
        """Load or fetch the GraphQL schema for a given environment."""
        if environment in cls._schemas:
            return cls._schemas[environment]

        with cls._lock:
            # Double-check after acquiring lock
            if environment in cls._schemas:
                return cls._schemas[environment]

            # Try loading from disk cache
            schema = cls._load_from_cache(environment)
            if schema:
                cls._schemas[environment] = schema
                return schema

            # Avoid recursive validation while an introspection request is
            # being constructed for this environment.
            if not client or environment in cls._fetching:
                return None

        # Fetch outside _lock. Introspection performs network I/O and should not
        # block other schema cache operations for the duration of the request.
        schema = cls._fetch_and_cache(environment, client)
        if schema:
            with cls._lock:
                cls._schemas[environment] = schema
            return schema

        return None

    @classmethod
    def validate_query(
        cls, query_str: str, environment: str, client: Any = None
    ) -> None:
        """Validate a query against the schema. Raises WizSchemaValidationError on failure."""
        schema = cls.get_schema(environment, client)
        if schema is None:
            logger.debug(
                "No schema available for '%s' — skipping validation", environment
            )
            return

        try:
            document = gql_parse(query_str)
        except Exception:
            return  # syntax errors are caught elsewhere

        errors = validate(schema, document)
        if errors:
            messages = [str(e) for e in errors]
            raise WizSchemaValidationError(
                f"Query validation failed with {len(errors)} error(s):\n"
                + "\n".join(f"  - {m}" for m in messages),
                query=query_str,
                validation_errors=messages,
            )

    @classmethod
    def clear(cls, environment: Optional[str] = None) -> None:
        """Clear cached schema(s). If environment is None, clear all."""
        with cls._lock:
            if environment:
                cls._schemas.pop(environment, None)
            else:
                cls._schemas.clear()

    @classmethod
    def _schema_cache_path(cls, environment: str) -> Path:
        """Return the filesystem path for the cached schema JSON."""
        return DEFAULT_WIZ_DIR / f"schema_{environment}.json"

    @classmethod
    def _load_from_cache(cls, environment: str) -> Optional[GraphQLSchema]:
        """Load and build a GraphQL schema from the disk cache, or return None."""
        cache_path = cls._schema_cache_path(environment)
        if not cache_path.exists():
            return None

        try:
            with open(cache_path, "r") as f:
                data = json.load(f)
            # Handle both {"__schema": {...}} and direct schema dict
            introspection = {"__schema": data} if "__schema" not in data else data
            schema = build_client_schema(cast(IntrospectionQuery, introspection))
            logger.info(
                "Loaded cached schema for '%s' from %s", environment, cache_path
            )
            return schema
        except Exception as e:
            logger.warning("Failed to load cached schema for '%s': %s", environment, e)
            return None

    @classmethod
    def _fetch_and_cache(cls, environment: str, client: Any) -> Optional[GraphQLSchema]:
        """Fetch schema via introspection and cache to disk."""
        with cls._lock:
            if environment in cls._fetching:
                return None
            cls._fetching.add(environment)
        try:
            logger.info("Fetching schema for '%s' via introspection...", environment)
            client._check_token()
            response = client._post(
                url=client._api_endpoint(),
                headers=client._get_headers(),
                json={"query": INTROSPECTION_QUERY, "variables": {}},
            )

            if response.status_code != 200:
                logger.warning(
                    "Introspection query failed for '%s' with status %s: %s",
                    environment,
                    response.status_code,
                    response.text,
                )
                return None

            response_data = response.json()
            errors = response_data.get("errors", [])
            if errors:
                logger.warning(
                    "Introspection query failed for '%s': %s",
                    environment,
                    errors,
                )
                return None

            schema_data = (response_data.get("data") or {}).get("__schema")
            if not schema_data:
                logger.warning(
                    "Introspection response missing __schema for '%s'", environment
                )
                return None

            # Cache to disk
            cache_path = cls._schema_cache_path(environment)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w") as f:
                json.dump(schema_data, f, indent=2)
            logger.info("Cached schema for '%s' to %s", environment, cache_path)

            return build_client_schema({"__schema": schema_data})

        except Exception as e:
            logger.warning("Failed to fetch schema for '%s': %s", environment, e)
            return None
        finally:
            with cls._lock:
                cls._fetching.discard(environment)
