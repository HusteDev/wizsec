"""Tests for utils.py — utility functions."""

import configparser
import csv
import json
import logging
import os
import pickle
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from wizsec.utils import (
    extract_fields,
    parse_query_metadata,
    ensure_pagination_variables,
    return_formatted_duration,
    resource_path,
    safe_write_json,
    disable_in_serverless,
    dump_to_json,
    store_data,
    load_credentials_from_file,
    write_credentials_to_file,
    is_in_last_x_intervals,
    determine_time_format,
    find_file_with_extension,
    load_file_if_in_last_x_interval,
    load_file,
    load_json,
    load_csv,
    load_xml,
    load_text,
    load_data,
)
from wizsec.exceptions import WizFileError

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


# ── disable_in_serverless ──────────────────────────────────────────


class TestDisableInServerless:
    def test_runs_normally_when_not_serverless(self, mock_config):
        @disable_in_serverless
        def my_func(x):
            return x * 2

        with patch.object(mock_config, "serverless", return_value=False):
            assert my_func(5) == 10

    def test_skips_when_serverless(self, mock_config):
        @disable_in_serverless
        def my_func(x):
            return x * 2

        with patch.object(mock_config, "serverless", return_value=True):
            assert my_func(5) is None

    def test_preserves_function_name(self, mock_config):
        @disable_in_serverless
        def my_special_func():
            pass

        assert my_special_func.__name__ == "my_special_func"


# ── dump_to_json ───────────────────────────────────────────────────


class TestDumpToJson:
    def test_writes_json_file(self, tmp_path, mock_config):
        save_dir = tmp_path / "saved-data"
        save_dir.mkdir(parents=True)
        mock_config._CONFIG["saved_data"]["enabled"] = True
        mock_config._CONFIG["saved_data"]["directory"] = str(save_dir)

        with (
            patch.object(mock_config, "serverless", return_value=False),
            patch.object(mock_config, "saved_data_enabled", return_value=True),
            patch.object(mock_config, "saved_data_directory", return_value=save_dir),
        ):
            dump_to_json({"key": "val"}, filename="test_dump.json")

        output = save_dir / "test_dump.json"
        assert output.exists()
        assert json.loads(output.read_text())["key"] == "val"

    def test_no_data_does_not_write(self, mock_config):
        with patch.object(mock_config, "serverless", return_value=False):
            # Should not raise, just log debug
            dump_to_json(None, filename="empty.json")

    def test_saved_data_disabled_does_not_write(self, tmp_path, mock_config):
        with (
            patch.object(mock_config, "serverless", return_value=False),
            patch.object(mock_config, "saved_data_enabled", return_value=False),
        ):
            dump_to_json({"data": 1}, filename="no_write.json")

        assert not (tmp_path / "no_write.json").exists()


# ── store_data ─────────────────────────────────────────────────────


class TestStoreData:
    def test_pickle_round_trip(self, tmp_path):
        data = {"items": [1, 2, 3], "nested": {"a": "b"}}
        pkl_path = tmp_path / "test.pkl"

        result = store_data(data, str(pkl_path))
        assert result == data
        assert pkl_path.exists()

        with open(pkl_path, "rb") as f:
            loaded = pickle.load(f)
        assert loaded == data

    def test_default_path_when_none(self, tmp_path):
        with patch("pathlib.Path.cwd", return_value=tmp_path):
            store_data({"x": 1})
        assert (tmp_path / "data.pkl").exists()

    def test_handles_write_error(self, tmp_path):
        # Writing to a directory path should fail gracefully (returns None)
        result = store_data({"x": 1}, str(tmp_path))
        assert result is None


# ── load_credentials_from_file ─────────────────────────────────────


class TestLoadCredentialsFromFile:
    def test_loads_existing_profile(self, tmp_path, mock_config):
        cred_file = tmp_path / "creds.ini"
        config = configparser.ConfigParser()
        config["myprofile"] = {
            "client_id": "id123",
            "client_secret": "secret456",
            "environment": "prod",
        }
        with open(cred_file, "w") as f:
            config.write(f)

        with patch.object(mock_config, "serverless", return_value=False):
            result = load_credentials_from_file("myprofile", str(cred_file))

        assert result["client_id"] == "id123"
        assert result["client_secret"] == "secret456"
        assert result["environment"] == "prod"

    def test_missing_file_returns_empty(self, tmp_path, mock_config):
        with patch.object(mock_config, "serverless", return_value=False):
            result = load_credentials_from_file(
                "default", str(tmp_path / "nonexistent.ini")
            )
        assert result == {}

    def test_missing_profile_returns_empty(self, tmp_path, mock_config):
        cred_file = tmp_path / "creds.ini"
        config = configparser.ConfigParser()
        config["other"] = {"client_id": "x", "client_secret": "y"}
        with open(cred_file, "w") as f:
            config.write(f)

        with patch.object(mock_config, "serverless", return_value=False):
            result = load_credentials_from_file("missing_profile", str(cred_file))
        assert result == {}

    def test_profile_without_environment(self, tmp_path, mock_config):
        cred_file = tmp_path / "creds.ini"
        config = configparser.ConfigParser()
        config["simple"] = {"client_id": "abc", "client_secret": "def"}
        with open(cred_file, "w") as f:
            config.write(f)

        with patch.object(mock_config, "serverless", return_value=False):
            result = load_credentials_from_file("simple", str(cred_file))
        assert result["client_id"] == "abc"
        assert result["environment"] is None

    def test_skipped_in_serverless(self, mock_config):
        with patch.object(mock_config, "serverless", return_value=True):
            result = load_credentials_from_file("any", "/fake/path")
        assert result is None


# ── write_credentials_to_file ──────────────────────────────────────


class TestWriteCredentialsToFile:
    def test_creates_new_file_with_profile(self, tmp_path, mock_config):
        cred_file = tmp_path / "new_creds.ini"

        with patch.object(mock_config, "serverless", return_value=False):
            write_credentials_to_file("default", "myid", "mysecret", str(cred_file))

        config = configparser.ConfigParser()
        config.read(cred_file)
        assert config["default"]["client_id"] == "myid"
        assert config["default"]["client_secret"] == "mysecret"

    def test_updates_existing_profile(self, tmp_path, mock_config):
        cred_file = tmp_path / "creds.ini"
        config = configparser.ConfigParser()
        config["default"] = {"client_id": "old", "client_secret": "old"}
        with open(cred_file, "w") as f:
            config.write(f)

        with patch.object(mock_config, "serverless", return_value=False):
            write_credentials_to_file("default", "new_id", "new_secret", str(cred_file))

        config2 = configparser.ConfigParser()
        config2.read(cred_file)
        assert config2["default"]["client_id"] == "new_id"
        assert config2["default"]["client_secret"] == "new_secret"

    def test_writes_environment(self, tmp_path, mock_config):
        cred_file = tmp_path / "creds.ini"

        with patch.object(mock_config, "serverless", return_value=False):
            write_credentials_to_file(
                "prod", "id", "secret", str(cred_file), environment="us1"
            )

        config = configparser.ConfigParser()
        config.read(cred_file)
        assert config["prod"]["environment"] == "us1"

    def test_no_environment_key_when_none(self, tmp_path, mock_config):
        cred_file = tmp_path / "creds.ini"

        with patch.object(mock_config, "serverless", return_value=False):
            write_credentials_to_file("test", "id", "secret", str(cred_file))

        config = configparser.ConfigParser()
        config.read(cred_file)
        assert "environment" not in config["test"]

    def test_skipped_in_serverless(self, mock_config):
        with patch.object(mock_config, "serverless", return_value=True):
            result = write_credentials_to_file("a", "b", "c", "/fake")
        assert result is None


# ── is_in_last_x_intervals ────────────────────────────────────────


class TestIsInLastXIntervals:
    def test_recent_datetime_within_interval(self):
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        assert (
            is_in_last_x_intervals(recent, interval_value=1, interval_type="days")
            is True
        )

    def test_old_datetime_outside_interval(self):
        old = datetime.now(timezone.utc) - timedelta(days=10)
        assert (
            is_in_last_x_intervals(old, interval_value=1, interval_type="days") is False
        )

    def test_string_input_iso_format(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        assert (
            is_in_last_x_intervals(recent, interval_value=1, interval_type="days")
            is True
        )

    def test_invalid_interval_type_raises(self):
        with pytest.raises(ValueError, match="Invalid interval type"):
            is_in_last_x_intervals(datetime.now(timezone.utc), interval_type="weeks")

    def test_zero_or_negative_interval_returns_true(self):
        old = datetime.now(timezone.utc) - timedelta(days=100)
        assert (
            is_in_last_x_intervals(old, interval_value=0, interval_type="days") is True
        )

    def test_minutes_interval(self):
        recent = datetime.now(timezone.utc) - timedelta(minutes=2)
        assert (
            is_in_last_x_intervals(recent, interval_value=5, interval_type="minutes")
            is True
        )

    def test_seconds_interval(self):
        old = datetime.now(timezone.utc) - timedelta(days=1)
        assert (
            is_in_last_x_intervals(old, interval_value=10, interval_type="seconds")
            is False
        )

    def test_naive_datetime_treated_as_utc(self):
        recent = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        assert (
            is_in_last_x_intervals(recent, interval_value=1, interval_type="days")
            is True
        )


# ── determine_time_format ──────────────────────────────────────────


class TestDetermineTimeFormat:
    def test_iso_8601_with_z(self):
        fmt = determine_time_format("2024-07-31T16:14:51Z")
        assert fmt is not None
        assert "T" in fmt

    def test_iso_8601_with_microseconds(self):
        fmt = determine_time_format("2024-07-31T16:14:51.123456")
        assert fmt is not None

    def test_date_only(self):
        fmt = determine_time_format("2024-07-31")
        assert fmt == "%Y-%m-%d"

    def test_compact_date(self):
        fmt = determine_time_format("20240731")
        assert fmt == "%Y%m%d"

    def test_time_only(self):
        fmt = determine_time_format("16:14:51")
        assert fmt == "%H:%M:%S"

    def test_unrecognized_format_returns_none(self):
        assert determine_time_format("not-a-date") is None

    def test_date_with_slashes(self):
        fmt = determine_time_format("2024/07/31")
        assert fmt == "%Y/%m/%d"

    def test_full_datetime_space_separated(self):
        fmt = determine_time_format("2024-07-31 16:14:51")
        assert fmt == "%Y-%m-%d %H:%M:%S"


# ── find_file_with_extension ──────────────────────────────────────


class TestFindFileWithExtension:
    def test_finds_file_with_given_extension(self, tmp_path):
        target = tmp_path / "data.json"
        target.write_text("{}")
        result = find_file_with_extension(
            str(tmp_path / "data.json"), [".pkl", ".json"]
        )
        assert result == str(target)

    def test_tries_extension_order(self, tmp_path):
        # Only CSV exists
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("a,b")
        result = find_file_with_extension(
            str(tmp_path / "data"), [".json", ".pkl", ".csv"]
        )
        assert result == str(csv_file)

    def test_returns_empty_when_not_found(self, tmp_path):
        result = find_file_with_extension(str(tmp_path / "missing"), [".json", ".csv"])
        assert result == ""

    def test_prefers_explicit_extension(self, tmp_path):
        json_file = tmp_path / "data.json"
        json_file.write_text("{}")
        pkl_file = tmp_path / "data.pkl"
        pkl_file.write_bytes(b"")
        # When extension is explicit, only check that exact file
        result = find_file_with_extension(
            str(tmp_path / "data.json"), [".pkl", ".json"]
        )
        assert result == str(json_file)

    def test_explicit_extension_not_found(self, tmp_path):
        result = find_file_with_extension(str(tmp_path / "data.xml"), [".json"])
        assert result == ""


# ── load_file_if_in_last_x_interval ───────────────────────────────


class TestLoadFileIfInLastXInterval:
    def test_loads_recent_file(self, tmp_path):
        json_file = tmp_path / "data.json"
        # File must be > 1000 bytes to pass the size check
        data = {"result": 42, "padding": "x" * 1100}
        json_file.write_text(json.dumps(data))
        result = load_file_if_in_last_x_interval(
            str(tmp_path / "data"), interval_value=1, interval_type="days"
        )
        assert result["result"] == 42

    def test_negative_interval_returns_empty(self, tmp_path):
        json_file = tmp_path / "data.json"
        json_file.write_text(json.dumps({"x": 1}))
        result = load_file_if_in_last_x_interval(
            str(tmp_path / "data"), interval_value=-1, interval_type="days"
        )
        assert result == {}

    def test_no_interval_loads_directly(self, tmp_path):
        json_file = tmp_path / "data.json"
        json_file.write_text(json.dumps({"direct": True}))
        result = load_file_if_in_last_x_interval(
            str(tmp_path / "data"), interval_value=None
        )
        assert result == {"direct": True}

    def test_file_not_found_returns_empty(self, tmp_path):
        result = load_file_if_in_last_x_interval(
            str(tmp_path / "nonexistent"), interval_value=1, interval_type="days"
        )
        assert result == {}


# ── load_file ──────────────────────────────────────────────────────


class TestLoadFile:
    def test_dispatches_json(self, tmp_path):
        f = tmp_path / "test.json"
        f.write_text(json.dumps({"a": 1}))
        result = load_file(str(f))
        assert result == {"a": 1}

    def test_dispatches_csv(self, tmp_path):
        f = tmp_path / "test.csv"
        f.write_text("name,age\nAlice,30\nBob,25\n")
        with patch(
            "wizsec.utils.mimetypes.guess_type", return_value=("text/csv", None)
        ):
            result = load_file(str(f))
        assert len(result) == 2
        assert result[0]["name"] == "Alice"

    def test_dispatches_xml(self, tmp_path):
        f = tmp_path / "test.xml"
        f.write_text("<root><item>hello</item></root>")
        result = load_file(str(f))
        assert result.tag == "root"

    def test_dispatches_text(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        result = load_file(str(f))
        assert result == "hello world"

    def test_dispatches_pickle(self, tmp_path):
        f = tmp_path / "test.pkl"
        data = {"key": [1, 2, 3]}
        with open(f, "wb") as fh:
            pickle.dump(data, fh)
        result = load_file(str(f))
        assert result == data

    def test_unknown_mime_returns_empty(self, tmp_path):
        f = tmp_path / "test.xyz"
        f.write_text("unknown")
        result = load_file(str(f))
        assert result == {}


# ── load_json ──────────────────────────────────────────────────────


class TestLoadJson:
    def test_loads_plain_dict(self, tmp_path):
        f = tmp_path / "test.json"
        f.write_text(json.dumps({"x": 1, "y": 2}))
        assert load_json(str(f)) == {"x": 1, "y": 2}

    def test_normalizes_single_key_list(self, tmp_path):
        """When JSON is {key: [list-of-dicts]}, missing keys are filled with None."""
        data = {"items": [{"a": 1}, {"b": 2}]}
        f = tmp_path / "test.json"
        f.write_text(json.dumps(data))
        result = load_json(str(f))
        # Both items should have keys 'a' and 'b'
        for item in result["items"]:
            assert "a" in item
            assert "b" in item

    def test_loads_list_directly(self, tmp_path):
        f = tmp_path / "test.json"
        f.write_text(json.dumps([1, 2, 3]))
        assert load_json(str(f)) == [1, 2, 3]


# ── load_csv ───────────────────────────────────────────────────────


class TestLoadCsv:
    def test_loads_csv_to_dicts(self, tmp_path):
        f = tmp_path / "test.csv"
        f.write_text("name,value\nfoo,1\nbar,2\n")
        result = load_csv(str(f))
        assert len(result) == 2
        assert result[0]["name"] == "foo"
        assert result[1]["value"] == "2"

    def test_empty_csv(self, tmp_path):
        f = tmp_path / "test.csv"
        f.write_text("name,value\n")
        result = load_csv(str(f))
        assert result == []


# ── load_xml ───────────────────────────────────────────────────────


class TestLoadXml:
    def test_returns_root_element(self, tmp_path):
        f = tmp_path / "test.xml"
        f.write_text("<root><child attr='val'>text</child></root>")
        root = load_xml(str(f))
        assert isinstance(root, ET.Element)
        assert root.tag == "root"
        assert root.find("child").text == "text"
        assert root.find("child").attrib["attr"] == "val"


# ── load_text ──────────────────────────────────────────────────────


class TestLoadText:
    def test_loads_plain_text(self, tmp_path):
        f = tmp_path / "test.txt"
        content = "line1\nline2\nline3"
        f.write_text(content)
        assert load_text(str(f)) == content

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        assert load_text(str(f)) == ""


# ── load_data ──────────────────────────────────────────────────────


class TestLoadData:
    def test_loads_pickle(self, tmp_path):
        f = tmp_path / "test.pkl"
        data = {"nested": {"list": [1, 2, 3]}}
        with open(f, "wb") as fh:
            pickle.dump(data, fh)
        result = load_data(str(f))
        assert result == data

    def test_corrupt_pickle_raises(self, tmp_path):
        f = tmp_path / "bad.pkl"
        f.write_bytes(b"not a pickle")
        with pytest.raises(WizFileError, match="Failed to load data"):
            load_data(str(f))

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(WizFileError, match="Failed to load data"):
            load_data(str(tmp_path / "nonexistent.pkl"))


# ── safe_write_json — error path (lines 86-100) ─────────────────────


class TestSafeWriteJsonErrorPath:
    def test_raises_on_write_error(self, tmp_path, mock_config):
        """When writing fails, the exception propagates and temp file is cleaned up."""
        data = {"key": "value"}
        save_dir = tmp_path / "output"
        save_dir.mkdir()
        temp_dir = tmp_path / "temp"
        temp_dir.mkdir()

        with patch("wizsec.utils.json.dump", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                safe_write_json(data, "test.json", save_dir, temp_dir)

    def test_temp_file_cleaned_up_on_error(self, tmp_path, mock_config):
        """Temp file is removed in the finally block even when an error occurs."""
        data = {"key": "value"}
        save_dir = tmp_path / "output"
        save_dir.mkdir()
        temp_dir = tmp_path / "temp"
        temp_dir.mkdir()

        with patch("wizsec.utils.json.dump", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                safe_write_json(data, "test.json", save_dir, temp_dir)

        # No temp file should remain
        assert len(list(temp_dir.glob("*"))) == 0

    def test_serialize_object_fallback(self, tmp_path, mock_config):
        """Custom object without __dict__ is serialized as str()."""

        class NoDict:
            __slots__ = ("x",)

            def __init__(self, x):
                self.x = x

            def __str__(self):
                return "no-dict-obj"

        save_dir = tmp_path / "output"
        save_dir.mkdir()
        temp_dir = tmp_path / "temp"
        temp_dir.mkdir()

        safe_write_json({"item": NoDict(42)}, "test.json", save_dir, temp_dir)
        import json as _json

        content = _json.loads((save_dir / "test.json").read_text())
        assert content["item"] == "no-dict-obj"

    def test_serialize_object_uses_vars(self, tmp_path, mock_config):
        """Custom object with __dict__ is serialized using vars()."""

        class WithDict:
            def __init__(self):
                self.name = "example"

        save_dir = tmp_path / "output"
        save_dir.mkdir()
        temp_dir = tmp_path / "temp"
        temp_dir.mkdir()

        safe_write_json({"obj": WithDict()}, "test.json", save_dir, temp_dir)
        import json as _json

        content = _json.loads((save_dir / "test.json").read_text())
        assert content["obj"]["name"] == "example"


# ── dump_to_json — additional branches (lines 118-226) ──────────────


class TestDumpToJsonBranches:
    def test_list_of_lists_with_duplicate_headers(self, tmp_path, mock_config):
        """dump_to_json handles list-of-lists data with duplicate column headers."""
        save_dir = tmp_path / "saved-data"
        save_dir.mkdir(parents=True)

        # List where first row is a header with duplicates
        data = [["col", "col", "other"], [1, 2, 3], [4, 5, 6]]

        with (
            patch.object(mock_config, "serverless", return_value=False),
            patch.object(mock_config, "saved_data_enabled", return_value=True),
            patch.object(mock_config, "saved_data_directory", return_value=save_dir),
        ):
            dump_to_json(data, filename="dup_headers.json")

        output = save_dir / "dup_headers.json"
        assert output.exists()

    def test_list_of_dicts_written_as_json(self, tmp_path, mock_config):
        """dump_to_json handles a list of dicts (non-list-of-lists)."""
        save_dir = tmp_path / "saved-data"
        save_dir.mkdir(parents=True)

        data = [{"a": 1}, {"a": 2}]

        with (
            patch.object(mock_config, "serverless", return_value=False),
            patch.object(mock_config, "saved_data_enabled", return_value=True),
            patch.object(mock_config, "saved_data_directory", return_value=save_dir),
        ):
            dump_to_json(data, filename="list_of_dicts.json")

        output = save_dir / "list_of_dicts.json"
        assert output.exists()

    def test_filepath_takes_precedence_over_filename(self, tmp_path, mock_config):
        """dump_to_json uses filepath when provided instead of default save directory."""
        save_dir = tmp_path / "custom-dir"
        save_dir.mkdir(parents=True)
        target_file = save_dir / "custom_output.json"

        with (
            patch.object(mock_config, "serverless", return_value=False),
            patch.object(mock_config, "saved_data_enabled", return_value=True),
            patch.object(mock_config, "saved_data_directory", return_value=save_dir),
        ):
            dump_to_json({"x": 1}, filepath=str(target_file))

        assert target_file.exists()

    def test_no_filename_uses_dumpfile_default(self, tmp_path, mock_config):
        """dump_to_json falls back to dumpFile.json when no filename or filepath."""
        save_dir = tmp_path / "saved-data"
        save_dir.mkdir(parents=True)

        with (
            patch.object(mock_config, "serverless", return_value=False),
            patch.object(mock_config, "saved_data_enabled", return_value=True),
            patch.object(mock_config, "saved_data_directory", return_value=save_dir),
        ):
            dump_to_json({"y": 2})

        output = save_dir / "dumpFile.json"
        assert output.exists()


# ── extract_fields — fragment_spread and inline_fragment (lines 298-304) ──


class TestExtractFieldsBranches:
    def test_fragment_spread_recorded_as_fragment(self):
        from graphql import parse

        doc = parse("{ user { id ...UserFields } }")
        selection_set = doc.definitions[0].selection_set.selections[0].selection_set
        fields = extract_fields(selection_set)
        assert "UserFields" in fields
        assert fields["UserFields"] == "FRAGMENT"

    def test_inline_fragment_fields_merged(self):
        from graphql import parse

        doc = parse("{ search { ... on User { name } } }")
        selection_set = doc.definitions[0].selection_set.selections[0].selection_set
        fields = extract_fields(selection_set)
        assert "name" in fields


# ── has_totalcount_field (lines 419-424) ──────────────────────────


class TestHasTotalcountField:
    def test_returns_false_for_empty_fields(self):
        from wizsec.utils import has_totalcount_field

        assert has_totalcount_field({}) is False

    def test_returns_false_when_root_not_dict(self):
        from wizsec.utils import has_totalcount_field

        assert has_totalcount_field({"issues": None}) is False

    def test_returns_true_when_totalcount_present(self):
        from wizsec.utils import has_totalcount_field

        assert (
            has_totalcount_field({"issues": {"nodes": None, "totalCount": None}})
            is True
        )

    def test_returns_false_when_totalcount_absent(self):
        from wizsec.utils import has_totalcount_field

        assert has_totalcount_field({"issues": {"nodes": None}}) is False


# ── build_totalcount_probe_query (lines 436-497) ──────────────────


class TestBuildTotalcountProbeQuery:
    def test_returns_none_for_invalid_query(self):
        from wizsec.utils import build_totalcount_probe_query

        assert build_totalcount_probe_query("not graphql {{") is None

    def test_returns_none_for_mutation(self):
        from wizsec.utils import build_totalcount_probe_query

        result = build_totalcount_probe_query(
            "mutation M { createIssue(input: {}) { id } }"
        )
        assert result is None

    def test_returns_none_when_no_connection_field(self):
        from wizsec.utils import build_totalcount_probe_query

        result = build_totalcount_probe_query("query Q { user { id name } }")
        assert result is None

    def test_returns_probe_query_with_totalcount(self):
        from wizsec.utils import build_totalcount_probe_query

        result = build_totalcount_probe_query(
            "query Q($first: Int, $filterBy: IssueFilters) { issues(first: $first, filterBy: $filterBy) { nodes { id } totalCount } }"
        )
        assert result is not None
        assert "totalCount" in result
        # $first and $after variable definitions should be dropped
        assert "$first" not in result
        assert "$after" not in result

    def test_returns_none_when_no_totalcount_child(self):
        from wizsec.utils import build_totalcount_probe_query

        result = build_totalcount_probe_query(
            "query Q { issues { nodes { id } pageInfo { hasNextPage } } }"
        )
        assert result is None


# ── inject_subscription_filter (lines 513-521) ────────────────────


class TestInjectSubscriptionFilter:
    def test_injects_at_top_level_key(self):
        from wizsec.utils import inject_subscription_filter

        result = inject_subscription_filter({}, "subscriptionId", ["id-1"])
        assert result == {"subscriptionId": ["id-1"]}

    def test_injects_nested_key(self):
        from wizsec.utils import inject_subscription_filter

        result = inject_subscription_filter(
            {"filterBy": {"severity": "HIGH"}},
            "filterBy.subscriptionId",
            ["id-1"],
        )
        assert result["filterBy"]["severity"] == "HIGH"
        assert result["filterBy"]["subscriptionId"] == ["id-1"]

    def test_creates_intermediate_dicts(self):
        from wizsec.utils import inject_subscription_filter

        result = inject_subscription_filter({}, "a.b.c", ["x"])
        assert result["a"]["b"]["c"] == ["x"]

    def test_does_not_mutate_original(self):
        from wizsec.utils import inject_subscription_filter

        original = {"filterBy": {"severity": "HIGH"}}
        inject_subscription_filter(original, "filterBy.subscriptionId", ["id-1"])
        assert "subscriptionId" not in original["filterBy"]

    def test_overwrites_existing_non_dict_value(self):
        from wizsec.utils import inject_subscription_filter

        # When an intermediate key exists but isn't a dict, it gets replaced
        result = inject_subscription_filter(
            {"filterBy": "not-a-dict"},
            "filterBy.subscriptionId",
            ["id-1"],
        )
        assert result["filterBy"]["subscriptionId"] == ["id-1"]


# ── write_credentials_to_file — error branches (lines 575-585) ────


class TestWriteCredentialsErrors:
    def test_io_error_raises_wiz_file_error(self, tmp_path, mock_config):
        """IOError during write raises WizFileError."""
        cred_file = tmp_path / "creds.ini"

        with patch.object(mock_config, "serverless", return_value=False):
            with patch("builtins.open", side_effect=IOError("permission denied")):
                with pytest.raises(WizFileError, match="Failed to write credentials"):
                    write_credentials_to_file("default", "id", "secret", str(cred_file))

    def test_unexpected_error_raises_wiz_file_error(self, tmp_path, mock_config):
        """Unexpected exceptions during write raise WizFileError."""
        cred_file = tmp_path / "creds.ini"

        with patch.object(mock_config, "serverless", return_value=False):
            with patch("builtins.open", side_effect=RuntimeError("unexpected")):
                with pytest.raises(WizFileError, match="Unexpected error"):
                    write_credentials_to_file("default", "id", "secret", str(cred_file))


# ── load_file_if_in_last_x_interval — file too old path (line 720) ──


class TestLoadFileIfInLastXIntervalOld:
    def test_file_modified_too_long_ago_returns_empty(self, tmp_path):
        """File that was modified before the interval returns empty dict."""
        json_file = tmp_path / "data.json"
        # Must be > 1000 bytes
        json_file.write_text(json.dumps({"old": True, "padding": "x" * 1100}))

        # Pretend the file is old (modified 10 days ago)
        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        import os

        os.utime(str(json_file), (old_time, old_time))

        result = load_file_if_in_last_x_interval(
            str(tmp_path / "data"), interval_value=1, interval_type="days"
        )
        assert result == {}

    def test_file_smaller_than_1000_bytes_returns_empty(self, tmp_path):
        """File smaller than 1000 bytes returns empty dict even if recent."""
        json_file = tmp_path / "data.json"
        json_file.write_text(json.dumps({"small": True}))  # well under 1000 bytes

        result = load_file_if_in_last_x_interval(
            str(tmp_path / "data"), interval_value=1, interval_type="days"
        )
        assert result == {}
