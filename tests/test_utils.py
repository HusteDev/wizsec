"""Tests for utils.py — utility functions."""

import json
import logging
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from wiz_sdk.utils import (
    extract_fields,
    parse_query_metadata,
    ensure_pagination_variables,
    return_formatted_duration,
    resource_path,
    safe_write_json,
)


# ── extract_fields ──────────────────────────────────────────────────

class TestExtractFields:
    def test_simple_fields(self):
        from graphql import parse
        doc = parse("{ user { id name email } }")
        selection_set = doc.definitions[0].selection_set.selections[0].selection_set
        fields = extract_fields(selection_set)
        assert "id" in fields
        assert "name" in fields
        assert "email" in fields
        assert fields["id"] is None  # leaf fields

    def test_nested_fields(self):
        from graphql import parse
        doc = parse("{ user { id profile { bio avatar } } }")
        selection_set = doc.definitions[0].selection_set.selections[0].selection_set
        fields = extract_fields(selection_set)
        assert "id" in fields
        assert isinstance(fields["profile"], dict)
        assert "bio" in fields["profile"]
        assert "avatar" in fields["profile"]


# ── parse_query_metadata ────────────────────────────────────────────

class TestParseQueryMetadata:
    def test_named_query(self):
        result = parse_query_metadata("query ListUsers { users { id } }")
        assert result["request_name"] == "ListUsers"
        assert result["source"] == "users"
        assert "users" in result["fields"]

    def test_mutation(self):
        result = parse_query_metadata("mutation CreateProject { createProject { id } }")
        assert "mutation" in result["request_type"].lower()
        assert result["request_name"] == "CreateProject"
        assert result["source"] == "createProject"

    def test_anonymous_query(self):
        result = parse_query_metadata("{ users { id name } }")
        assert result["request_name"] == ""
        assert result["source"] == "users"

    def test_nested_fields_in_metadata(self):
        result = parse_query_metadata("""
            query ListProjects {
                projects {
                    nodes { id name }
                    pageInfo { hasNextPage endCursor }
                }
            }
        """)
        assert "projects" in result["fields"]
        assert "nodes" in result["fields"]["projects"]
        assert "pageInfo" in result["fields"]["projects"]


# ── ensure_pagination_variables ─────────────────────────────────────

class TestEnsurePaginationVariables:
    def test_injects_after_when_missing(self):
        query = "query Q($first: Int) { items(first: $first) { nodes { id } pageInfo { hasNextPage endCursor } } }"
        result = ensure_pagination_variables(query)
        assert "$after: String" in result
        assert "after: $after" in result

    def test_skips_when_after_already_present(self):
        query = "query Q($first: Int, $after: String) { items(first: $first, after: $after) { nodes { id } pageInfo { hasNextPage endCursor } } }"
        result = ensure_pagination_variables(query)
        assert result == query

    def test_skips_mutations(self):
        query = "mutation M($input: Input!) { createItem(input: $input) { id } }"
        result = ensure_pagination_variables(query)
        assert result == query

    def test_skips_without_relay_pattern(self):
        query = "query Q { user(id: 1) { id name } }"
        result = ensure_pagination_variables(query)
        assert result == query

    def test_handles_anonymous_query(self):
        query = "{ items { nodes { id } pageInfo { hasNextPage endCursor } } }"
        result = ensure_pagination_variables(query)
        assert "$after: String" in result

    def test_unparseable_query_returns_unchanged(self):
        query = "this is not graphql {{"
        result = ensure_pagination_variables(query)
        assert result == query

    def test_skips_when_field_already_has_after_arg(self):
        query = "query Q($first: Int) { items(first: $first, after: null) { nodes { id } pageInfo { hasNextPage endCursor } } }"
        result = ensure_pagination_variables(query)
        # Should not add $after to var defs since the field already has the arg
        assert result == query

    def test_no_variable_definitions(self):
        """Query with relay pattern but no variable definitions at all."""
        query = "query Q { items { nodes { id } pageInfo { hasNextPage endCursor } } }"
        result = ensure_pagination_variables(query)
        assert "$after: String" in result


# ── return_formatted_duration ───────────────────────────────────────

class TestReturnFormattedDuration:
    def test_seconds_only(self):
        result = return_formatted_duration(5.5)
        assert "5" in result
        assert "second" in result.lower() or "s" in result

    def test_minutes(self):
        result = return_formatted_duration(125)
        assert "2" in result  # 2 minutes
        assert "5" in result  # 5 seconds

    def test_hours(self):
        result = return_formatted_duration(3665)
        assert "1" in result  # 1 hour

    def test_zero(self):
        result = return_formatted_duration(0)
        assert result is not None


# ── resource_path ───────────────────────────────────────────────────

class TestResourcePath:
    def test_returns_absolute_path(self):
        result = resource_path("some/file.txt")
        assert Path(result).is_absolute()

    def test_with_meipass(self):
        """PyInstaller sets sys._MEIPASS."""
        import sys
        with patch.object(sys, "_MEIPASS", "/tmp/meipass", create=True):
            result = resource_path("data/config.yml")
            assert "/tmp/meipass" in result or "\\tmp\\meipass" in result


# ── safe_write_json ─────────────────────────────────────────────────

class TestSafeWriteJson:
    def test_writes_and_moves(self, tmp_path, mock_config):
        data = {"key": "value"}
        save_dir = tmp_path / "output"
        save_dir.mkdir()
        temp_dir = tmp_path / "temp"
        temp_dir.mkdir()

        safe_write_json(data, "test.json", save_dir, temp_dir)

        output = save_dir / "test.json"
        assert output.exists()
        assert json.loads(output.read_text())["key"] == "value"

    def test_cleans_up_temp_on_success(self, tmp_path, mock_config):
        data = {"a": 1}
        save_dir = tmp_path / "out"
        save_dir.mkdir()
        temp_dir = tmp_path / "tmp"
        temp_dir.mkdir()

        safe_write_json(data, "clean.json", save_dir, temp_dir)

        # Temp file should not remain
        temp_files = list(temp_dir.glob("*"))
        assert len(temp_files) == 0
