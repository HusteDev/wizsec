"""Tests for _schema.py — SchemaValidator."""

import json
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from graphql import build_client_schema, introspection_from_schema, build_schema

from wiz_sdk._schema import SchemaValidator, INTROSPECTION_QUERY
from wiz_sdk.exceptions import WizSchemaValidationError


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

        with patch.object(SchemaValidator, "_schema_cache_path", return_value=cache_file):
            schema = SchemaValidator.get_schema("disktest")
        assert schema is not None
        assert "disktest" in SchemaValidator._schemas

    def test_corrupted_cache_returns_none(self, tmp_path):
        cache_file = tmp_path / "schema_bad.json"
        cache_file.write_text("not json")

        with patch.object(SchemaValidator, "_schema_cache_path", return_value=cache_file):
            schema = SchemaValidator.get_schema("bad")
        assert schema is None


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
        mock_result = MagicMock()
        mock_result.success.return_value = True
        mock_result.data = {"__schema": schema_data}

        mock_response = MagicMock()
        mock_response.submit.return_value = mock_result

        mock_client = MagicMock()
        mock_client.create_request.return_value = mock_response

        cache_file = tmp_path / "schema_fetch.json"
        with patch.object(SchemaValidator, "_schema_cache_path", return_value=cache_file):
            schema = SchemaValidator._fetch_and_cache("fetch", mock_client)

        assert schema is not None
        assert cache_file.exists()
        assert "fetch" not in SchemaValidator._fetching

    def test_failed_fetch_returns_none(self):
        mock_result = MagicMock()
        mock_result.success.return_value = False
        mock_result.errors = [{"message": "unauthorized"}]

        mock_response = MagicMock()
        mock_response.submit.return_value = mock_result

        mock_client = MagicMock()
        mock_client.create_request.return_value = mock_response

        schema = SchemaValidator._fetch_and_cache("fail", mock_client)
        assert schema is None

    def test_recursion_guard(self):
        """Should not fetch if already fetching for this environment."""
        mock_client = MagicMock()
        SchemaValidator._fetching.add("recursive")
        result = SchemaValidator.get_schema("recursive", client=mock_client)
        assert result is None
        mock_client.create_request.assert_not_called()
        SchemaValidator._fetching.discard("recursive")


class TestIntrospectionQuery:
    def test_query_is_parseable(self):
        from graphql import parse
        doc = parse(INTROSPECTION_QUERY)
        assert len(doc.definitions) > 0
