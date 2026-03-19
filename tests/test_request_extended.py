"""Extended tests for _request.py — WizResponse, WizBatchResponse, query setter, and more."""

import asyncio
import logging
import threading
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from wizsec.config import Config
from wizsec._request import (
    _RequestBase,
    WizRequest,
    WizResponse,
    WizBatchRequest,
    WizBatchResponse,
)
from wizsec._transport import TransportError
from wizsec.exceptions import WizQueryError


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
             patch("wizsec._request.SchemaValidator") as mock_sv:
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


# ── QueryCollection Setter ─────────────────────────────────────────

class TestQueryCollectionSetter:
    def test_set_query_collection_from_module(self, mock_wiz_client):
        """Setting queryCollection with a module object stores it."""
        import types
        mod = types.ModuleType("fake_queries")
        mod.GET_USERS = "query GetUsers { users { id } }"
        req = _make_wiz_request(mock_wiz_client)
        req.queryCollection = mod
        assert req.queryCollection is mod

    def test_set_query_collection_from_string_not_loaded(self, mock_wiz_client):
        """Setting queryCollection with a module name string that is not yet loaded imports it."""
        import sys
        req = _make_wiz_request(mock_wiz_client)
        # Use a module not yet in sys.modules by temporarily removing it
        mod_name = "wizsec._request"  # already loaded, so use a unique fake
        fake_mod_name = "_test_fake_qc_module"
        import types
        fake_mod = types.ModuleType(fake_mod_name)
        # Patch importlib.import_module to return our fake module
        with patch("importlib.import_module", return_value=fake_mod) as mock_import:
            req.queryCollection = fake_mod_name
            mock_import.assert_called_once_with(fake_mod_name)
        assert req.queryCollection is fake_mod

    def test_set_query_collection_invalid_type_raises(self, mock_wiz_client):
        """Setting queryCollection with an invalid type raises WizQueryError."""
        req = _make_wiz_request(mock_wiz_client)
        with pytest.raises(WizQueryError, match="QueryCollection must be a module"):
            req.queryCollection = 42

    def test_query_collection_getter_returns_none_when_unset(self, mock_wiz_client):
        """queryCollection returns None when not set."""
        req = _make_wiz_request(mock_wiz_client)
        assert req.queryCollection is None

    def test_query_resolved_from_collection(self, mock_wiz_client):
        """When query string is not valid GraphQL, resolve from queryCollection attribute."""
        import types
        mod = types.ModuleType("fake_queries")
        mod.MY_QUERY = "query MyQuery { users { id } }"
        req = _make_wiz_request(mock_wiz_client, paginate=False)
        req.queryCollection = mod
        req.query = "MY_QUERY"
        assert "users" in req.query


# ── WizRequest.__init__ ────────────────────────────────────────────

class TestWizRequestInit:
    def test_basic_init(self, mock_wiz_client):
        """WizRequest initializes with correct default state."""
        req = _make_wiz_request(
            mock_wiz_client,
            query="query Q { users { id } }",
            vars={"first": 10},
        )
        assert req.vars == {"first": 10}
        assert req.errors == []
        assert req.data is None
        assert req._status_code is None
        assert req._page == 0
        assert hasattr(req, "_done_event")

    def test_init_without_query(self, mock_wiz_client):
        """WizRequest can be initialized without a query."""
        req = _make_wiz_request(mock_wiz_client)
        assert req.errors == []

    def test_init_sets_paginate_flag(self, mock_wiz_client):
        """Paginate flag is stored correctly."""
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }", paginate=False)
        assert req._paginate is False

    def test_init_with_on_page_event(self, mock_wiz_client):
        """on_page_event callback is stored."""
        cb = MagicMock()
        req = _make_wiz_request(
            mock_wiz_client,
            query="query Q { users { id } }",
            on_page_event=cb,
        )
        assert req._page_event is cb


# ── WizRequest._execute_page ──────────────────────────────────────

class TestExecutePage:
    def _setup_client(self, mock_wiz_client, status_code=200, json_data=None):
        """Configure mock client for _execute_page calls."""
        mock_wiz_client._check_token.return_value = None
        mock_wiz_client._api_endpoint.return_value = "https://api.us1.app.wiz.io/graphql"
        mock_wiz_client._get_headers.return_value = {"Authorization": "Bearer fake"}

        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.json.return_value = json_data or {"data": {"users": {"nodes": [{"id": "1"}]}}}
        mock_response.text = "error text"
        mock_wiz_client._post.return_value = mock_response
        return mock_response

    def test_successful_execution(self, mock_wiz_client):
        """Successful _execute_page sets data and status code."""
        self._setup_client(mock_wiz_client, json_data={"data": {"users": [{"id": "1"}]}})
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }", paginate=False)
        req._execute_page()
        assert req._status_code == 200
        assert req.data == {"users": [{"id": "1"}]}

    def test_failed_response_records_error(self, mock_wiz_client):
        """Non-200 response records errors and retries."""
        self._setup_client(mock_wiz_client, status_code=500)
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }", paginate=False)
        with patch("wizsec._request.time.sleep"):
            req._execute_page()
        # Should have errors from each retry + the final failure
        assert len(req.errors) > 0
        assert any("error text" in e["message"] for e in req.errors)

    def test_transport_error_records_error(self, mock_wiz_client):
        """TransportError is caught and recorded."""
        mock_wiz_client._check_token.return_value = None
        mock_wiz_client._api_endpoint.return_value = "https://api.us1.app.wiz.io/graphql"
        mock_wiz_client._get_headers.return_value = {}
        mock_wiz_client._post.side_effect = TransportError("connection reset")
        mock_limiter = MagicMock()
        mock_wiz_client._get_limiter.return_value = mock_limiter

        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }", paginate=False)
        with patch("wizsec._request.time.sleep"):
            req._execute_page()
        assert any("connection reset" in e["message"] for e in req.errors)

    def test_unexpected_error_records_error(self, mock_wiz_client):
        """Unexpected exceptions are caught and recorded."""
        mock_wiz_client._check_token.return_value = None
        mock_wiz_client._api_endpoint.return_value = "https://api.us1.app.wiz.io/graphql"
        mock_wiz_client._get_headers.return_value = {}
        mock_wiz_client._post.side_effect = RuntimeError("something broke")
        mock_limiter = MagicMock()
        mock_wiz_client._get_limiter.return_value = mock_limiter

        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }", paginate=False)
        with patch("wizsec._request.time.sleep"):
            req._execute_page()
        assert any("something broke" in e["message"] for e in req.errors)

    def test_graphql_errors_in_response(self, mock_wiz_client):
        """GraphQL errors in 200 response are extracted."""
        self._setup_client(
            mock_wiz_client,
            json_data={"data": None, "errors": [{"message": "field not found"}]},
        )
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }", paginate=False)
        req._execute_page()
        assert any("field not found" in e["message"] for e in req.errors)


# ── _process_successful_response ──────────────────────────────────

class TestProcessSuccessfulResponse:
    def _setup_req(self, mock_wiz_client, paginate=False):
        mock_wiz_client._check_token.return_value = None
        mock_wiz_client._api_endpoint.return_value = "https://api.us1.app.wiz.io/graphql"
        mock_wiz_client._get_headers.return_value = {}
        return _make_wiz_request(mock_wiz_client, query="query Q { users { id } }", paginate=paginate)

    def test_merges_page_data(self, mock_wiz_client):
        """_process_successful_response merges page data into aggregated data."""
        req = self._setup_req(mock_wiz_client, paginate=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"users": [{"id": "1"}]}}

        result = req._process_successful_response(mock_resp, "url", {})
        assert result is True
        assert req._aggregated_data == {"users": [{"id": "1"}]}

    def test_errors_cause_early_return(self, mock_wiz_client):
        """When response contains errors, processing stops immediately."""
        req = self._setup_req(mock_wiz_client, paginate=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {}, "errors": [{"message": "bad query"}]}

        result = req._process_successful_response(mock_resp, "url", {})
        assert result is True
        assert len(req.errors) == 1

    def test_report_detection_triggers_workflow(self, mock_wiz_client):
        """When _generate_report returns True, _report_workflow is called."""
        req = self._setup_req(mock_wiz_client, paginate=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"createReport": {"report": {"id": "r1"}}}}

        with patch.object(req, "_generate_report", return_value=True), \
             patch.object(req, "_report_workflow") as mock_wf:
            result = req._process_successful_response(mock_resp, "url", {})
            assert result is True
            mock_wf.assert_called_once()


# ── _handle_failed_response ───────────────────────────────────────

class TestHandleFailedResponse:
    def test_records_error_and_logs(self, mock_wiz_client):
        """_handle_failed_response appends error from response text."""
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }", paginate=False)
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        req._handle_failed_response(mock_resp)
        assert req.errors == [{"message": "Forbidden"}]


# ── _handle_standard_pagination ───────────────────────────────────

class TestHandleStandardPagination:
    def test_no_paginate_sets_data_immediately(self, mock_wiz_client):
        """When paginate=False, data is set and done event is fired."""
        req = _make_wiz_request(
            mock_wiz_client, query="query Q { users { id } }", paginate=False
        )
        page_data = {"users": [{"id": "1"}]}
        result = req._handle_standard_pagination(page_data)
        assert result is True
        assert req.data == page_data

    def test_paginate_with_next_page(self, mock_wiz_client):
        """When hasNextPage=True, sets after cursor and re-enqueues."""
        req = _make_wiz_request(
            mock_wiz_client,
            query="query Q($first: Int) { users(first: $first) { nodes { id } pageInfo { hasNextPage endCursor } } }",
            paginate=True,
        )
        page_data = {
            "users": {
                "nodes": [{"id": "1"}],
                "pageInfo": {"hasNextPage": True, "endCursor": "cursor123"},
            }
        }
        req._handle_standard_pagination(page_data)
        assert req.vars["after"] == "cursor123"
        mock_wiz_client._enqueue_request.assert_called_with(req)

    def test_paginate_with_no_next_page_finalizes(self, mock_wiz_client):
        """When hasNextPage=False, pagination is finalized."""
        req = _make_wiz_request(
            mock_wiz_client,
            query="query Q($first: Int) { users(first: $first) { nodes { id } pageInfo { hasNextPage endCursor } } }",
            paginate=True,
        )
        # Simulate first page was already merged
        req._aggregated_data = {"users": {"nodes": [{"id": "1"}]}}
        page_data = {
            "users": {
                "nodes": [{"id": "1"}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
        req._handle_standard_pagination(page_data)
        assert req.data is not None
        assert req._done_event.is_set()


# ── _handle_serverless_pagination ─────────────────────────────────

class TestHandleServerlessPagination:
    def test_no_pagination_sets_data_from_response(self, mock_wiz_client):
        """When not paginating in serverless mode, data comes from response."""
        req = _make_wiz_request(
            mock_wiz_client, query="query Q { users { id } }", paginate=False
        )
        req._response = {"data": {"users": [{"id": "1"}]}}
        result = req._handle_serverless_pagination("url", {})
        assert result is True
        assert req.data == {"users": [{"id": "1"}]}

    def test_pagination_loops_until_no_next_page(self, mock_wiz_client):
        """Serverless pagination loops inline until hasNextPage=False."""
        req = _make_wiz_request(
            mock_wiz_client,
            query="query Q($first: Int) { users(first: $first) { nodes { id } pageInfo { hasNextPage endCursor } } }",
            paginate=True,
        )
        # Force _paginate to True (serverless normally disables it)
        req._paginate = True
        req._aggregated_data = {
            "users": {
                "nodes": [{"id": "1"}],
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
            }
        }
        req._response = {
            "data": {
                "users": {
                    "nodes": [{"id": "1"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                }
            }
        }

        # Second page response
        page2_resp = MagicMock()
        page2_resp.status_code = 200
        page2_resp.json.return_value = {
            "data": {
                "users": {
                    "nodes": [{"id": "2"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        mock_wiz_client._post.return_value = page2_resp

        req._handle_serverless_pagination("url", {})
        assert req.data is not None
        assert len(req.data["users"]["nodes"]) == 2

    def test_pagination_stops_on_non_200(self, mock_wiz_client):
        """Serverless pagination breaks on non-200 response."""
        req = _make_wiz_request(
            mock_wiz_client,
            query="query Q($first: Int) { users(first: $first) { nodes { id } pageInfo { hasNextPage endCursor } } }",
            paginate=True,
        )
        req._paginate = True
        req._response = {
            "data": {
                "users": {
                    "nodes": [{"id": "1"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                }
            }
        }
        req._aggregated_data = req._response["data"].copy()

        bad_resp = MagicMock()
        bad_resp.status_code = 500
        mock_wiz_client._post.return_value = bad_resp

        req._handle_serverless_pagination("url", {})
        assert req.data is not None


# ── _trigger_page_event ───────────────────────────────────────────

class TestTriggerPageEvent:
    def test_fires_callback_with_correct_data(self, mock_wiz_client):
        """_trigger_page_event calls callback with page data and info."""
        cb = MagicMock()
        req = _make_wiz_request(
            mock_wiz_client,
            query="query Q { users { id } }",
            on_page_event=cb,
            vars={"first": 50},
        )
        page_data = {"users": [{"id": "1"}]}
        req._trigger_page_event(page_data)

        cb.assert_called_once()
        call_arg = cb.call_args[0][0]
        assert call_arg["page_data"] == page_data
        assert call_arg["page_info"]["per_page"] == 50
        assert call_arg["page_info"]["page"] == 1
        assert call_arg["errors"] == []

    def test_no_callback_does_nothing(self, mock_wiz_client):
        """_trigger_page_event is a no-op when no callback is set."""
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }")
        # Should not raise
        req._trigger_page_event({"users": []})

    def test_page_counter_increments(self, mock_wiz_client):
        """Page counter increments on each call."""
        cb = MagicMock()
        req = _make_wiz_request(
            mock_wiz_client,
            query="query Q { users { id } }",
            on_page_event=cb,
        )
        req._trigger_page_event({"a": 1})
        req._trigger_page_event({"b": 2})
        assert cb.call_count == 2
        assert cb.call_args_list[0][0][0]["page_info"]["page"] == 1
        assert cb.call_args_list[1][0][0]["page_info"]["page"] == 2


# ── _finalize_pagination ──────────────────────────────────────────

class TestFinalizePagination:
    def test_sets_data_and_done_event(self, mock_wiz_client):
        """_finalize_pagination sets data from aggregated data and fires done event."""
        req = _make_wiz_request(
            mock_wiz_client,
            query="query Q($first: Int) { users(first: $first) { nodes { id } pageInfo { hasNextPage endCursor } } }",
            paginate=True,
        )
        req._aggregated_data = {
            "users": {
                "nodes": [{"id": "1"}, {"id": "2"}],
                "pageInfo": {"hasNextPage": False},
            }
        }
        req._finalize_pagination()
        assert req.data is not None
        assert "pageInfo" not in req.data["users"]
        assert req._done_event.is_set()


# ── WizRequest.submit ─────────────────────────────────────────────

class TestWizRequestSubmit:
    def test_submit_enqueues_and_waits(self, mock_wiz_client):
        """submit() enqueues request and waits for done event."""
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }")

        def enqueue_side_effect(r):
            r._done_event.set()

        mock_wiz_client._enqueue_request.side_effect = enqueue_side_effect
        with patch.object(Config, "serverless", return_value=False):
            result = req.submit()
        assert result is req
        mock_wiz_client._enqueue_request.assert_called_once_with(req)

    def test_submit_serverless_mode(self, mock_wiz_client):
        """In serverless mode, submit runs inline without threading."""
        req = _make_wiz_request(mock_wiz_client, query="query Q { users { id } }")
        with patch.object(Config, "serverless", return_value=True):
            result = req.submit()
        assert result is req
        mock_wiz_client._enqueue_request.assert_called_once_with(req)


# ── _generate_report ──────────────────────────────────────────────

class TestGenerateReport:
    def test_returns_true_for_create_report(self, mock_wiz_client):
        """_generate_report returns True when source is createReport."""
        req = _make_wiz_request(
            mock_wiz_client,
            query='mutation M { createReport(input: {}) { report { id } } }',
            paginate=False,
        )
        assert req._generate_report() is True

    def test_returns_false_for_regular_query(self, mock_wiz_client):
        """_generate_report returns False for regular queries."""
        req = _make_wiz_request(
            mock_wiz_client,
            query="query Q { users { id } }",
            paginate=False,
        )
        assert req._generate_report() is False

    def test_sets_report_name_from_request(self, mock_wiz_client):
        """_generate_report sets report_name from report_request dict."""
        req = _make_wiz_request(
            mock_wiz_client,
            query='mutation M { createReport(input: {}) { report { id } } }',
            paginate=False,
            report_request={"name": "my_report", "stream": False},
        )
        req._generate_report()
        assert req.report_name == "my_report"
        assert req.stream_report is False


# ── WizBatchRequest ───────────────────────────────────────────────

class TestWizBatchRequest:
    def test_init(self, mock_wiz_client):
        """WizBatchRequest initializes with empty state."""
        batch = WizBatchRequest(mock_wiz_client)
        assert batch.size() == 0
        assert len(batch) == 0

    def test_add_request(self, mock_wiz_client):
        """add_request creates a request via client and stores it."""
        mock_resp = MagicMock()
        mock_resp._request = MagicMock()
        mock_wiz_client.create_request.return_value = mock_resp

        batch = WizBatchRequest(mock_wiz_client)
        request_id = batch.add_request(query="query Q { users { id } }")
        assert request_id == 0
        assert batch.size() == 1

    def test_add_multiple_requests(self, mock_wiz_client):
        """Adding multiple requests assigns sequential IDs."""
        mock_resp = MagicMock()
        mock_resp._request = MagicMock()
        mock_wiz_client.create_request.return_value = mock_resp

        batch = WizBatchRequest(mock_wiz_client)
        id0 = batch.add_request(query="query Q1 { users { id } }")
        id1 = batch.add_request(query="query Q2 { projects { id } }")
        assert id0 == 0
        assert id1 == 1
        assert batch.size() == 2

    def test_add_response(self, mock_wiz_client):
        """add_response stores an existing WizResponse."""
        mock_resp = MagicMock()
        mock_resp._request = MagicMock()

        batch = WizBatchRequest(mock_wiz_client)
        request_id = batch.add_response(mock_resp)
        assert request_id == 0
        assert batch.size() == 1

    def test_submit_empty_batch(self, mock_wiz_client):
        """Submitting empty batch returns empty WizBatchResponse."""
        batch = WizBatchRequest(mock_wiz_client)
        result = batch.submit()
        assert isinstance(result, WizBatchResponse)
        assert result.total_count() == 0

    def test_submit_sequential_serverless(self, mock_wiz_client):
        """In serverless mode, requests run sequentially."""
        mock_inner_req = MagicMock()
        mock_inner_req.errors = []
        mock_resp = MagicMock()
        mock_resp._request = mock_inner_req
        mock_wiz_client.create_request.return_value = mock_resp

        batch = WizBatchRequest(mock_wiz_client)
        batch.add_request(query="query Q { users { id } }")

        with patch.object(Config, "serverless", return_value=True):
            result = batch.submit()
        assert isinstance(result, WizBatchResponse)
        mock_inner_req.submit.assert_called_once()

    def test_clear(self, mock_wiz_client):
        """clear() removes all requests."""
        mock_resp = MagicMock()
        mock_resp._request = MagicMock()
        mock_wiz_client.create_request.return_value = mock_resp

        batch = WizBatchRequest(mock_wiz_client)
        batch.add_request(query="query Q { users { id } }")
        assert batch.size() == 1
        batch.clear()
        assert batch.size() == 0


# ── WizBatchRequest Progress Callback ─────────────────────────────

class TestBatchProgressCallback:
    def test_progress_callback_fires(self, mock_wiz_client):
        """Progress callback is called for each completed request."""
        mock_inner_req = MagicMock()
        mock_inner_req.errors = []
        mock_resp = MagicMock()
        mock_resp._request = mock_inner_req
        mock_wiz_client.create_request.return_value = mock_resp

        progress_calls = []
        batch = WizBatchRequest(mock_wiz_client)
        batch.set_progress_callback(lambda c, t: progress_calls.append((c, t)))
        batch.add_request(query="query Q1 { a { id } }")
        batch.add_request(query="query Q2 { b { id } }")

        with patch.object(Config, "serverless", return_value=True):
            batch.submit()

        assert len(progress_calls) == 2
        assert progress_calls[-1] == (2, 2)

    def test_progress_callback_error_does_not_crash(self, mock_wiz_client):
        """If progress callback raises, it is caught gracefully."""
        mock_inner_req = MagicMock()
        mock_inner_req.errors = []
        mock_resp = MagicMock()
        mock_resp._request = mock_inner_req
        mock_wiz_client.create_request.return_value = mock_resp

        def bad_callback(completed, total):
            raise ValueError("callback crashed")

        batch = WizBatchRequest(mock_wiz_client)
        batch.set_progress_callback(bad_callback)
        batch.add_request(query="query Q { a { id } }")

        with patch.object(Config, "serverless", return_value=True):
            # Should not raise
            result = batch.submit()
        assert isinstance(result, WizBatchResponse)


# ── AsyncWizRequest / AsyncWizResponse ────────────────────────────

class TestAsyncWizRequest:
    @pytest.mark.asyncio
    async def test_submit_calls_execute_page(self, mock_wiz_client):
        """AsyncWizRequest.submit checks token and executes page."""
        from wizsec._request import AsyncWizRequest

        with patch.object(Config, "validate_queries", return_value=False):
            req = AsyncWizRequest(
                client=mock_wiz_client,
                query="query Q { users { id } }",
                paginate=False,
            )

        mock_wiz_client._check_token.return_value = None

        # Mock _execute_page to avoid real async HTTP
        with patch.object(req, "_execute_page", new_callable=lambda: lambda: MagicMock(return_value=None)) as mock_exec:
            # Make _execute_page a proper coroutine
            async def fake_execute():
                req.data = {"users": [{"id": "1"}]}
            with patch.object(req, "_execute_page", side_effect=fake_execute):
                result = await req.submit()
        assert result is req
        mock_wiz_client._check_token.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_page_success(self, mock_wiz_client):
        """AsyncWizRequest._execute_page fetches data on success."""
        from wizsec._request import AsyncWizRequest

        with patch.object(Config, "validate_queries", return_value=False):
            req = AsyncWizRequest(
                client=mock_wiz_client,
                query="query Q { users { id } }",
                paginate=False,
            )

        # Set up async session mock
        mock_async_resp = MagicMock()
        mock_async_resp.status_code = 200
        mock_async_resp.json.return_value = {"data": {"users": [{"id": "1"}]}}

        mock_session = MagicMock()

        async def fake_post(**kwargs):
            return mock_async_resp

        mock_session.post = fake_post
        mock_wiz_client._async_session = mock_session
        mock_wiz_client._async_semaphore = asyncio.Semaphore(10)
        mock_wiz_client._api_endpoint.return_value = "https://api.us1.app.wiz.io/graphql"
        mock_wiz_client._get_headers.return_value = {}

        # Make limiter async-compatible
        mock_limiter = MagicMock()
        async def fake_acquire(key):
            pass
        mock_limiter.try_acquire_async = fake_acquire
        mock_wiz_client._get_limiter.return_value = mock_limiter

        await req._execute_page()
        assert req.data == {"users": [{"id": "1"}]}

    @pytest.mark.asyncio
    async def test_execute_page_retries_on_failure(self, mock_wiz_client):
        """AsyncWizRequest retries on non-200 and records errors."""
        from wizsec._request import AsyncWizRequest

        with patch.object(Config, "validate_queries", return_value=False):
            req = AsyncWizRequest(
                client=mock_wiz_client,
                query="query Q { users { id } }",
                paginate=False,
            )

        mock_async_resp = MagicMock()
        mock_async_resp.status_code = 500
        mock_async_resp.text = "Internal Server Error"

        mock_session = MagicMock()
        async def fake_post(**kwargs):
            return mock_async_resp
        mock_session.post = fake_post

        mock_wiz_client._async_session = mock_session
        mock_wiz_client._async_semaphore = asyncio.Semaphore(10)
        mock_wiz_client._api_endpoint.return_value = "https://api.us1.app.wiz.io/graphql"
        mock_wiz_client._get_headers.return_value = {}

        mock_limiter = MagicMock()
        async def fake_acquire(key):
            pass
        mock_limiter.try_acquire_async = fake_acquire
        mock_wiz_client._get_limiter.return_value = mock_limiter

        with patch("wizsec._request.asyncio.sleep", new_callable=lambda: lambda *a, **kw: fake_acquire("x")):
            await req._execute_page()

        assert len(req.errors) > 0

    @pytest.mark.asyncio
    async def test_execute_page_no_async_session_raises(self, mock_wiz_client):
        """AsyncWizRequest raises RuntimeError if no async session."""
        from wizsec._request import AsyncWizRequest

        with patch.object(Config, "validate_queries", return_value=False):
            req = AsyncWizRequest(
                client=mock_wiz_client,
                query="query Q { users { id } }",
            )

        # Remove _async_session if present
        if hasattr(mock_wiz_client, "_async_session"):
            del mock_wiz_client._async_session

        with pytest.raises(RuntimeError, match="Async session not initialized"):
            await req._execute_page()


class TestAsyncWizResponse:
    @pytest.mark.asyncio
    async def test_submit_delegates_to_request(self, mock_wiz_client):
        """AsyncWizResponse.submit delegates to its request."""
        from wizsec._request import AsyncWizRequest, AsyncWizResponse

        with patch.object(Config, "validate_queries", return_value=False):
            req = AsyncWizRequest(
                client=mock_wiz_client,
                query="query Q { users { id } }",
            )

        async def fake_submit():
            req.data = {"users": []}
            return req

        with patch.object(req, "submit", side_effect=fake_submit):
            resp = AsyncWizResponse(req)
            result = await resp.submit()
        assert result is resp

    def test_properties(self, mock_wiz_client):
        """AsyncWizResponse exposes data, errors, and success."""
        from wizsec._request import AsyncWizRequest, AsyncWizResponse

        with patch.object(Config, "validate_queries", return_value=False):
            req = AsyncWizRequest(
                client=mock_wiz_client,
                query="query Q { users { id } }",
            )
        req.data = {"users": []}
        req.errors = []

        resp = AsyncWizResponse(req)
        assert resp.data == {"users": []}
        assert resp.errors == []
        assert resp.success is True

    def test_repr(self, mock_wiz_client):
        """AsyncWizResponse repr shows success status."""
        from wizsec._request import AsyncWizRequest, AsyncWizResponse

        with patch.object(Config, "validate_queries", return_value=False):
            req = AsyncWizRequest(
                client=mock_wiz_client,
                query="query Q { users { id } }",
            )
        req.data = {"users": []}
        req.errors = []

        resp = AsyncWizResponse(req)
        r = repr(resp)
        assert "True" in r
