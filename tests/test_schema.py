"""Tests for _schema.py — SchemaValidator."""

import json
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from graphql import build_client_schema, introspection_from_schema, build_schema

from wizsec._schema import SchemaValidator, INTROSPECTION_QUERY
from wizsec.config import Config
from wizsec.exceptions import WizSchemaValidationError

# ── Helpers ──────────────────────────────────────────────────────────


def _make_test_schema():
    """Build a minimal GraphQL schema for testing."""
    schema = build_schema("""
        type Query {
            projects(first: Int, after: String): ProjectConnection!
            user(id: ID!): User
        }
        type ProjectConnection {
            nodes: [Project!]!
            pageInfo: PageInfo!
        }
        type Project {
            id: ID!
            name: String!
        }
        type User {
            id: ID!
            name: String!
            email: String
        }
        type PageInfo {
            hasNextPage: Boolean!
            endCursor: String
        }
    """)
    return schema


def _introspection_json():
    """Return introspection JSON for the test schema (just the __schema part)."""
    schema = _make_test_schema()
    result = introspection_from_schema(schema)
    return result["__schema"]


@pytest.fixture(autouse=True)
def _clean_schema_cache():
    """Clear SchemaValidator caches between tests."""
    SchemaValidator.clear()
    yield
    SchemaValidator.clear()


# ── Tests ────────────────────────────────────────────────────────────


class TestValidateQuery:
    def test_valid_query_passes(self):
        """A valid query should not raise."""
        SchemaValidator._schemas["test"] = _make_test_schema()
        SchemaValidator.validate_query(
            "query { projects(first: 10) { nodes { id name } pageInfo { hasNextPage } } }",
            "test",
        )

    def test_invalid_field_raises(self):
        """An invalid field should raise WizSchemaValidationError."""
        SchemaValidator._schemas["test"] = _make_test_schema()
        with pytest.raises(WizSchemaValidationError) as exc_info:
            SchemaValidator.validate_query("query { fakeField { id } }", "test")
        assert "fakeField" in str(exc_info.value)
        assert len(exc_info.value.validation_errors) >= 1

    def test_invalid_field_on_type_raises(self):
        SchemaValidator._schemas["test"] = _make_test_schema()
        with pytest.raises(WizSchemaValidationError) as exc_info:
            SchemaValidator.validate_query(
                "query { projects(first: 10) { nodes { id bogusField } pageInfo { hasNextPage } } }",
                "test",
            )
        assert "bogusField" in str(exc_info.value)

    def test_no_schema_available_skips_validation(self):
        """When no schema is cached, validation is silently skipped."""
        # Should not raise
        SchemaValidator.validate_query("query { anything { id } }", "nonexistent")

    def test_unparseable_query_skips_validation(self):
        """Syntax errors are handled elsewhere; validator returns silently."""
        SchemaValidator._schemas["test"] = _make_test_schema()
        SchemaValidator.validate_query("this is not graphql", "test")

    def test_error_includes_query(self):
        SchemaValidator._schemas["test"] = _make_test_schema()
        query = "query { missing { id } }"
        with pytest.raises(WizSchemaValidationError) as exc_info:
            SchemaValidator.validate_query(query, "test")
        assert exc_info.value.query == query


class TestGetSchema:
    def test_returns_cached_schema(self):
        schema = _make_test_schema()
        SchemaValidator._schemas["cached"] = schema
        assert SchemaValidator.get_schema("cached") is schema

    def test_returns_none_when_no_schema(self):
        assert SchemaValidator.get_schema("missing") is None

    def test_loads_from_disk_cache(self, tmp_path):
        schema_data = _introspection_json()
        cache_file = tmp_path / "schema_disktest.json"
        cache_file.write_text(json.dumps(schema_data))

        with patch.object(
            SchemaValidator, "_schema_cache_path", return_value=cache_file
        ):
            schema = SchemaValidator.get_schema("disktest")
        assert schema is not None
        assert "disktest" in SchemaValidator._schemas

    def test_corrupted_cache_returns_none(self, tmp_path):
        cache_file = tmp_path / "schema_bad.json"
        cache_file.write_text("not json")

        with patch.object(
            SchemaValidator, "_schema_cache_path", return_value=cache_file
        ):
            schema = SchemaValidator.get_schema("bad")
        assert schema is None

    def test_schema_cache_path_uses_configured_wiz_dir(self, mock_config, tmp_path):
        Config.set("app", "wiz_dir", value=str(tmp_path))
        assert SchemaValidator._schema_cache_path("gov") == tmp_path / "schema_gov.json"


class TestClear:
    def test_clear_specific(self):
        SchemaValidator._schemas["a"] = _make_test_schema()
        SchemaValidator._schemas["b"] = _make_test_schema()
        SchemaValidator.clear("a")
        assert "a" not in SchemaValidator._schemas
        assert "b" in SchemaValidator._schemas

    def test_clear_all(self):
        SchemaValidator._schemas["a"] = _make_test_schema()
        SchemaValidator._schemas["b"] = _make_test_schema()
        SchemaValidator.clear()
        assert len(SchemaValidator._schemas) == 0


class TestFetchAndCache:
    def test_successful_fetch(self, tmp_path):
        schema_data = _introspection_json()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": {"__schema": schema_data}}

        mock_client = MagicMock()
        mock_client._post.return_value = mock_response
        mock_client._api_endpoint.return_value = "https://example.test/graphql"
        mock_client._get_headers.return_value = {"Authorization": "Bearer token"}

        cache_file = tmp_path / "schema_fetch.json"
        with patch.object(
            SchemaValidator, "_schema_cache_path", return_value=cache_file
        ):
            schema = SchemaValidator._fetch_and_cache("fetch", mock_client)

        assert schema is not None
        assert cache_file.exists()
        assert "fetch" not in SchemaValidator._fetching
        mock_client._check_token.assert_called_once()
        mock_client._post.assert_called_once()

    def test_fetch_bypasses_validated_request_construction(self, tmp_path):
        """Schema bootstrap must not create a normal WizRequest while validation is enabled."""
        schema_data = _introspection_json()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": {"__schema": schema_data}}

        mock_client = MagicMock()
        mock_client._post.return_value = mock_response
        mock_client._api_endpoint.return_value = "https://example.test/graphql"
        mock_client._get_headers.return_value = {}
        mock_client.create_request.side_effect = AssertionError(
            "schema introspection must bypass create_request"
        )

        cache_file = tmp_path / "schema_fetch.json"
        with patch.object(
            SchemaValidator, "_schema_cache_path", return_value=cache_file
        ):
            schema = SchemaValidator.get_schema("fetch", client=mock_client)

        assert schema is not None
        mock_client.create_request.assert_not_called()

    def test_failed_fetch_returns_none(self):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "unauthorized"

        mock_client = MagicMock()
        mock_client._post.return_value = mock_response
        mock_client._api_endpoint.return_value = "https://example.test/graphql"
        mock_client._get_headers.return_value = {}

        schema = SchemaValidator._fetch_and_cache("fail", mock_client)
        assert schema is None

    def test_fetch_with_graphql_errors_returns_none(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"errors": [{"message": "unauthorized"}]}

        mock_client = MagicMock()
        mock_client._post.return_value = mock_response
        mock_client._api_endpoint.return_value = "https://example.test/graphql"
        mock_client._get_headers.return_value = {}

        schema = SchemaValidator._fetch_and_cache("fail", mock_client)
        assert schema is None

    def test_recursion_guard(self):
        """Should not fetch if already fetching for this environment."""
        mock_client = MagicMock()
        SchemaValidator._fetching.add("recursive")
        result = SchemaValidator.get_schema("recursive", client=mock_client)
        assert result is None
        mock_client._post.assert_not_called()
        SchemaValidator._fetching.discard("recursive")


class TestIntrospectionQuery:
    def test_query_is_parseable(self):
        from graphql import parse

        doc = parse(INTROSPECTION_QUERY)
        assert len(doc.definitions) > 0


class TestGetSchemaReentrancyAndServerless:
    def test_reentrant_validation_during_fetch_does_not_deadlock(self, tmp_path):
        """The introspection request's own query validation re-enters
        get_schema on the same thread; this must not deadlock."""
        client = MagicMock()
        client.environment = "reentrant-env"

        def create_request(query=None, paginate=None, **kw):
            # Mirrors the real query setter: validating the introspection
            # query itself calls back into get_schema on this thread.
            SchemaValidator.validate_query(query, "reentrant-env", client=client)
            result = MagicMock()
            result.success.return_value = False
            result.errors = []
            response = MagicMock()
            response.submit.return_value = result
            return response

        client.create_request = create_request

        outcome = {}

        def run():
            with patch.object(Config, "wiz_dir", return_value=tmp_path):
                outcome["schema"] = SchemaValidator.get_schema("reentrant-env", client)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(5)
        assert not t.is_alive(), "get_schema deadlocked on re-entrant validation"
        assert outcome["schema"] is None

    def test_serverless_never_introspects(self, tmp_path):
        client = MagicMock()
        with (
            patch.object(Config, "wiz_dir", return_value=tmp_path),
            patch.object(Config, "serverless", return_value=True),
        ):
            assert SchemaValidator.get_schema("srvless-env", client) is None
        client.create_request.assert_not_called()

    def test_serverless_still_loads_bundled_cache(self, tmp_path):
        """A pre-bundled schema_<env>.json must keep working in serverless."""
        cache_file = tmp_path / "schema_bundled-env.json"
        cache_file.write_text(json.dumps(_introspection_json()))
        client = MagicMock()
        with (
            patch.object(Config, "wiz_dir", return_value=tmp_path),
            patch.object(Config, "serverless", return_value=True),
        ):
            schema = SchemaValidator.get_schema("bundled-env", client)
        assert schema is not None
        client.create_request.assert_not_called()

    def test_failed_fetch_is_not_retried_per_request(self, tmp_path):
        """A failed introspection is remembered for the session instead of
        being re-attempted on every request."""
        response = MagicMock()
        response.status_code = 403
        response.text = "forbidden"
        client = MagicMock()
        client._post.return_value = response
        client._api_endpoint.return_value = "https://example.test/graphql"
        client._get_headers.return_value = {}

        with patch.object(Config, "wiz_dir", return_value=tmp_path):
            assert SchemaValidator.get_schema("negcache-env", client) is None
            assert SchemaValidator.get_schema("negcache-env", client) is None

        assert client._post.call_count == 1

    def test_cache_write_failure_still_returns_schema(self, tmp_path):
        """A read-only or full filesystem must not discard a fetched schema."""
        schema_data = _introspection_json()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"data": {"__schema": schema_data}}
        client = MagicMock()
        client._post.return_value = response
        client._api_endpoint.return_value = "https://example.test/graphql"
        client._get_headers.return_value = {}

        read_only = tmp_path / "missing" / "nested"
        with patch.object(
            SchemaValidator,
            "_schema_cache_path",
            return_value=read_only / "schema_x.json",
        ):
            with patch("wizsec._schema.open", side_effect=OSError("read-only")):
                schema = SchemaValidator._fetch_and_cache("rofs-env", client)

        assert schema is not None
