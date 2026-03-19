"""Extended tests for _request.py — WizResponse, WizBatchResponse, query setter, and more."""

import logging
import threading
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from wiz_sdk.config import Config
from wiz_sdk._request import (
    _RequestBase,
    WizRequest,
    WizResponse,
    WizBatchRequest,
    WizBatchResponse,
)
from wiz_sdk.exceptions import WizQueryError


# ── Helpers ──────────────────────────────────────────────────────────

@pytest.fixture()
def mock_wiz_client(mock_config):
    """Create a mock WizClient with enough state for request tests."""
    client = MagicMock()
    client.environment = "gov"
    client._query_retry_time = 0.01
    client._max_retries = 2
    client._is_service_account = True
    client._limiter_key.return_value = "query_service"

    # Mock the limiter to be a no-op
    mock_limiter = MagicMock()
    mock_limiter.try_acquire = MagicMock()
    client._get_limiter.return_value = mock_limiter

    return client


def _make_wiz_request(client, query=None, paginate=True, **kwargs):
    """Helper to create a WizRequest without triggering real network."""
    with patch.object(Config, "validate_queries", return_value=False):
        return WizRequest(
            client=client,
            query=query,
            paginate=paginate,
            **kwargs,
        )


# ── Query Setter ────────────────────────────────────────────────────

class TestQuerySetter:
    def test_valid_query(self, mock_wiz_client):
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }")
        assert req.query is not None
        assert "users" in req.query

    def test_invalid_type_raises(self, mock_wiz_client):
        with pytest.raises(WizQueryError, match="must be a string"):
            _make_wiz_request(mock_wiz_client, query=123)

    def test_pagination_injects_after(self, mock_wiz_client):
        req = _make_wiz_request(
            mock_wiz_client,
            query="query Q($first: Int) { items(first: $first) { nodes { id } pageInfo { hasNextPage endCursor } } }",
            paginate=True,
        )
        assert "$after" in req.query
        assert "after: $after" in req.query

    def test_no_pagination_skips_injection(self, mock_wiz_client):
        query = "query Q($first: Int) { items(first: $first) { nodes { id } pageInfo { hasNextPage endCursor } } }"
        req = _make_wiz_request(mock_wiz_client, query=query, paginate=False)
        assert "$after" not in req.query

    def test_report_detection(self, mock_wiz_client):
        """Report detection checks for createReport/rerunReport mutations."""
        req = _make_wiz_request(
            mock_wiz_client,
            query='mutation M { createReport(input: {}) { report { id } } }',
            paginate=False,
        )
        assert req._current_query_info["source"] == "createReport"
        assert req._report is True

    def test_query_metadata_parsed(self, mock_wiz_client):
        req = _make_wiz_request(
            mock_wiz_client,
            query="query ListUsers { users { id name } }",
        )
        assert req._current_query_info["request_name"] == "ListUsers"
        assert req._current_query_info["source"] == "users"

    def test_schema_validation_called_when_enabled(self, mock_wiz_client):
        with patch.object(Config, "validate_queries", return_value=True), \
             patch("wiz_sdk._request.SchemaValidator") as mock_sv:
            # Don't use _make_wiz_request since it patches validate_queries to False
            WizRequest(
                client=mock_wiz_client,
                query="query Q { users { id } }",
            )
            mock_sv.validate_query.assert_called_once()


# ── WizResponse ─────────────────────────────────────────────────────

class TestWizResponse:
    def test_wraps_request_data(self, mock_wiz_client):
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }")
        req.data = {"users": [{"id": "1"}]}
        resp = WizResponse(req)
        assert resp.data == {"users": [{"id": "1"}]}

    def test_wraps_errors(self, mock_wiz_client):
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }")
        req.errors = [{"message": "bad"}]
        resp = WizResponse(req)
        assert resp.errors == [{"message": "bad"}]

    def test_success_property(self, mock_wiz_client):
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }")
        req.data = {"users": []}
        resp = WizResponse(req)
        assert resp.success is True

    def test_failure_property(self, mock_wiz_client):
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }")
        req.errors = [{"message": "oops"}]
        resp = WizResponse(req)
        assert resp.success is False

    def test_node_type(self, mock_wiz_client):
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }")
        resp = WizResponse(req)
        assert resp.node_type == "users"

    def test_submit_delegates(self, mock_wiz_client):
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }")
        resp = WizResponse(req)
        with patch.object(req, "submit", return_value=req) as mock_submit:
            resp.submit()
            mock_submit.assert_called_once()

    def test_dir_hides_private(self, mock_wiz_client):
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }")
        resp = WizResponse(req)
        public_attrs = dir(resp)
        assert all(not a.startswith("_") for a in public_attrs)

    def test_exposed_fields_copied(self, mock_wiz_client):
        req = _make_wiz_request(
            mock_wiz_client,
            query="query Q { users { id } }",
            vars={"first": 10},
        )
        resp = WizResponse(req)
        assert resp.vars == {"first": 10}


# ── WizBatchResponse ───────────────────────────────────────────────

class TestWizBatchResponse:
    def _make_response(self, data=None, errors=None):
        """Create a mock WizResponse."""
        resp = MagicMock()
        resp.data = data
        resp.errors = errors or []
        resp.success = data is not None and not errors
        return resp

    def test_get_result(self, mock_config):
        r0 = self._make_response(data={"users": []})
        batch = WizBatchResponse({0: r0})
        assert batch.get_result(0) is r0
        assert batch.get_result(99) is None

    def test_getitem(self, mock_config):
        r0 = self._make_response(data={"users": []})
        batch = WizBatchResponse({0: r0})
        assert batch[0] is r0

    def test_all_successful_true(self, mock_config):
        results = {
            0: self._make_response(data={"a": 1}),
            1: self._make_response(data={"b": 2}),
        }
        batch = WizBatchResponse(results)
        assert batch.all_successful() is True

    def test_all_successful_false(self, mock_config):
        results = {
            0: self._make_response(data={"a": 1}),
            1: self._make_response(errors=[{"message": "fail"}]),
        }
        batch = WizBatchResponse(results)
        assert batch.all_successful() is False

    def test_success_count(self, mock_config):
        results = {
            0: self._make_response(data={"a": 1}),
            1: self._make_response(errors=[{"message": "fail"}]),
            2: self._make_response(data={"c": 3}),
        }
        batch = WizBatchResponse(results)
        assert batch.success_count() == 2
        assert batch.failure_count() == 1
        assert batch.total_count() == 3

    def test_success_rate(self, mock_config):
        results = {
            0: self._make_response(data={"a": 1}),
            1: self._make_response(errors=[{"message": "fail"}]),
        }
        batch = WizBatchResponse(results)
        assert batch.success_rate() == 50.0

    def test_success_rate_empty(self, mock_config):
        batch = WizBatchResponse({})
        assert batch.success_rate() == 0.0

    def test_get_all_data(self, mock_config):
        results = {
            0: self._make_response(data={"users": [1, 2]}),
            1: self._make_response(errors=[{"message": "fail"}]),
            2: self._make_response(data={"projects": [3]}),
        }
        batch = WizBatchResponse(results)
        all_data = batch.get_all_data()
        assert len(all_data) == 2

    def test_get_all_errors(self, mock_config):
        results = {
            0: self._make_response(data={"a": 1}),
            1: self._make_response(errors=[{"message": "fail1"}]),
            2: self._make_response(errors=[{"message": "fail2"}, {"message": "fail3"}]),
        }
        batch = WizBatchResponse(results)
        all_errors = batch.get_all_errors()
        assert len(all_errors) == 3
        assert all("request_id" in e for e in all_errors)

    def test_iterate_results(self, mock_config):
        results = {0: self._make_response(data={"a": 1}), 1: self._make_response(data={"b": 2})}
        batch = WizBatchResponse(results)
        items = list(batch.iterate_results())
        assert len(items) == 2

    def test_len(self, mock_config):
        results = {0: self._make_response(data={"a": 1})}
        batch = WizBatchResponse(results)
        assert len(batch) == 1

    def test_repr(self, mock_config):
        results = {0: self._make_response(data={"a": 1}), 1: self._make_response(errors=[{"message": "x"}])}
        batch = WizBatchResponse(results)
        assert "1/2" in repr(batch)


# ── WizRequest repr ────────────────────────────────────────────────

class TestWizRequestRepr:
    def test_repr(self, mock_wiz_client):
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }")
        req._status_code = 200
        req.data = {"users": []}
        r = repr(req)
        assert "200" in r
        assert "True" in r
