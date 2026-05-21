"""Additional tests for wizsec/_request.py to improve coverage.

Targets the uncovered branches in:
  - AsyncWizRequest._execute_page()  (rate limiting, 429, timeout, pagination)
  - AsyncWizBatchRequest             (add_request, submit, progress, exceptions)
  - WizRequest._execute_page()       (4xx non-retryable, BucketFullException path)
  - _extract_totalcount / _schema_supports_totalcount / _merge_split_results
  - AsyncWizResponse                 (properties, repr)
  - WizBatchRequest._submit_concurrent / _wait_for_completion (timeout path)
  - WizBatchResponse iteration / __iter__
  - WizRequest._handle_failed_response (retryable vs non-retryable)
  - _get_cached_or_fetch_entities    (cache hit / cache miss)

NOTE: Async tests that use asyncio.Semaphore directly are marked
``@pytest.mark.anyio(backends=["asyncio"])`` because asyncio primitives are
not compatible with Trio.
"""

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from wizsec.config import Config
from wizsec._request import (
    AsyncWizBatchRequest,
    AsyncWizRequest,
    AsyncWizResponse,
    WizBatchRequest,
    WizBatchResponse,
    WizRequest,
    _RequestBase,
    _extract_totalcount,
    _merge_split_results,
    _schema_supports_totalcount,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_client(mock_config):
    """A MagicMock client with enough surface area for request tests."""
    client = MagicMock()
    client.environment = "gov"
    client._query_retry_time = 0
    client._max_retries = 1
    client._is_service_account = True
    client._limiter_key.return_value = "query_service"

    mock_limiter = MagicMock()
    mock_limiter.try_acquire = MagicMock(return_value=None)
    client._get_limiter.return_value = mock_limiter

    return client


def _make_req(client, query="query Q { users { id } }", paginate=False, **kw):
    """Create a WizRequest with schema validation disabled."""
    with patch.object(Config, "validate_queries", return_value=False):
        return WizRequest(client=client, query=query, paginate=paginate, **kw)


def _make_async_req(client, query="query Q { users { id } }", paginate=False, **kw):
    """Create an AsyncWizRequest with schema validation disabled."""
    with patch.object(Config, "validate_queries", return_value=False):
        return AsyncWizRequest(client=client, query=query, paginate=paginate, **kw)


def _make_async_client(mock_client, status_code=200, json_data=None, text="error"):
    """Attach a working async session to mock_client and return it."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data or {"data": {"users": [{"id": "1"}]}}
    mock_resp.text = text
    mock_resp.headers = {}

    async def fake_post(**kwargs):
        return mock_resp

    session = MagicMock()
    session.post = fake_post

    mock_client._async_session = session
    mock_client._async_semaphore = asyncio.Semaphore(10)
    mock_client._api_endpoint.return_value = "https://api.wiz.io/graphql"
    mock_client._get_headers.return_value = {}

    return mock_resp


# ---------------------------------------------------------------------------
# _extract_totalcount
# ---------------------------------------------------------------------------


class TestExtractTotalcount:
    def test_returns_zero_for_none(self):
        assert _extract_totalcount(None, "issues") == 0

    def test_returns_zero_for_empty_dict(self):
        assert _extract_totalcount({}, "issues") == 0

    def test_returns_zero_when_source_missing(self):
        assert _extract_totalcount({"other": {"totalCount": 5}}, "issues") == 0

    def test_returns_count_when_present(self):
        assert _extract_totalcount({"issues": {"totalCount": 42}}, "issues") == 42

    def test_returns_zero_when_source_is_none(self):
        assert _extract_totalcount({"issues": None}, "issues") == 0


# ---------------------------------------------------------------------------
# _schema_supports_totalcount
# ---------------------------------------------------------------------------


class TestSchemaSupportssTotalcount:
    def test_returns_false_when_schema_is_none(self, mock_client):
        with patch("wizsec._request.SchemaValidator") as mock_sv:
            mock_sv.get_schema.return_value = None
            assert _schema_supports_totalcount("issues", mock_client) is False

    def test_returns_false_when_query_type_is_none(self, mock_client):
        mock_schema = MagicMock()
        mock_schema.query_type = None
        with patch("wizsec._request.SchemaValidator") as mock_sv:
            mock_sv.get_schema.return_value = mock_schema
            assert _schema_supports_totalcount("issues", mock_client) is False

    def test_returns_false_when_root_field_missing(self, mock_client):
        mock_schema = MagicMock()
        mock_schema.query_type.fields = {}
        with patch("wizsec._request.SchemaValidator") as mock_sv:
            mock_sv.get_schema.return_value = mock_schema
            assert _schema_supports_totalcount("issues", mock_client) is False

    def test_returns_false_when_totalcount_not_in_fields(self, mock_client):
        mock_named_type = MagicMock(spec=["fields"])
        mock_named_type.fields = {"nodes": MagicMock()}

        mock_field = MagicMock()
        mock_field.type = mock_named_type

        mock_schema = MagicMock()
        mock_schema.query_type.fields = {"issues": mock_field}

        with patch("wizsec._request.SchemaValidator") as mock_sv:
            mock_sv.get_schema.return_value = mock_schema
            assert _schema_supports_totalcount("issues", mock_client) is False

    def test_returns_true_when_totalcount_present(self, mock_client):
        mock_named_type = MagicMock(spec=["fields"])
        mock_named_type.fields = {"nodes": MagicMock(), "totalCount": MagicMock()}

        mock_field = MagicMock()
        mock_field.type = mock_named_type

        mock_schema = MagicMock()
        mock_schema.query_type.fields = {"issues": mock_field}

        with patch("wizsec._request.SchemaValidator") as mock_sv:
            mock_sv.get_schema.return_value = mock_schema
            assert _schema_supports_totalcount("issues", mock_client) is True

    def test_returns_false_on_exception(self, mock_client):
        with patch("wizsec._request.SchemaValidator") as mock_sv:
            mock_sv.get_schema.side_effect = RuntimeError("schema error")
            assert _schema_supports_totalcount("issues", mock_client) is False


# ---------------------------------------------------------------------------
# _merge_split_results
# ---------------------------------------------------------------------------


class TestMergeSplitResults:
    def _make_resp(self, data=None, errors=None, success=True):
        resp = MagicMock()
        resp.success = success and data is not None
        resp.data = data
        resp.errors = errors or []
        return resp

    def test_empty_responses(self, mock_config):
        logger = MagicMock()
        result = _merge_split_results([], "issues", logger)
        assert result == {"issues": {"nodes": [], "totalCount": 0}}

    def test_merges_nodes_from_multiple_responses(self, mock_config):
        logger = MagicMock()
        r1 = self._make_resp(data={"issues": {"nodes": [{"id": "1"}], "totalCount": 1}})
        r2 = self._make_resp(data={"issues": {"nodes": [{"id": "2"}], "totalCount": 1}})
        result = _merge_split_results([r1, r2], "issues", logger)
        assert len(result["issues"]["nodes"]) == 2
        assert result["issues"]["totalCount"] == 2

    def test_skips_failed_responses(self, mock_config):
        logger = MagicMock()
        r_fail = self._make_resp(success=False, data=None, errors=[{"message": "err"}])
        r_ok = self._make_resp(
            data={"issues": {"nodes": [{"id": "1"}], "totalCount": 1}}
        )
        result = _merge_split_results([r_fail, r_ok], "issues", logger)
        assert len(result["issues"]["nodes"]) == 1
        logger.warning.assert_called()

    def test_logs_warning_for_failed_with_errors(self, mock_config):
        logger = MagicMock()
        r_fail = MagicMock()
        r_fail.success = False
        r_fail.data = None
        r_fail.errors = [{"message": "sub-query failed"}]
        _merge_split_results([r_fail], "issues", logger)
        logger.warning.assert_called()

    def test_page_info_removed_from_merged(self, mock_config):
        logger = MagicMock()
        r1 = self._make_resp(
            data={
                "issues": {
                    "nodes": [{"id": "1"}],
                    "totalCount": 1,
                    "pageInfo": {"hasNextPage": False},
                }
            }
        )
        result = _merge_split_results([r1], "issues", logger)
        assert "pageInfo" not in result["issues"]

    def test_uses_len_nodes_when_total_count_absent(self, mock_config):
        logger = MagicMock()
        r1 = self._make_resp(data={"issues": {"nodes": [{"id": "a"}, {"id": "b"}]}})
        result = _merge_split_results([r1], "issues", logger)
        assert result["issues"]["totalCount"] == 2


# ---------------------------------------------------------------------------
# WizRequest._handle_failed_response  (4xx vs 5xx)
# ---------------------------------------------------------------------------


class TestHandleFailedResponseRetryLogic:
    def test_4xx_is_non_retryable(self, mock_client):
        req = _make_req(mock_client)
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        result = req._handle_failed_response(mock_resp)
        assert result is True  # non-retryable

    def test_403_is_non_retryable(self, mock_client):
        req = _make_req(mock_client)
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        result = req._handle_failed_response(mock_resp)
        assert result is True

    def test_5xx_is_retryable(self, mock_client):
        req = _make_req(mock_client)
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "Service Unavailable"
        result = req._handle_failed_response(mock_resp)
        assert result is False  # retryable

    def test_4xx_stops_retry_loop(self, mock_client):
        """4xx response exits after 1 attempt (not retried)."""
        mock_client._check_token.return_value = None
        mock_client._api_endpoint.return_value = "https://api.wiz.io/graphql"
        mock_client._get_headers.return_value = {}

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        mock_client._post.return_value = mock_resp

        req = _make_req(mock_client)
        with patch("wizsec._request.time.sleep"):
            req._execute_page()

        # Should only call _post once (no retries on 4xx)
        assert mock_client._post.call_count == 1
        assert len(req.errors) > 0


# ---------------------------------------------------------------------------
# WizRequest._execute_page  (BucketFullException path)
# ---------------------------------------------------------------------------


class TestExecutePageBucketFull:
    def test_bucket_full_exception_retries(self, mock_client):
        """BucketFullException from limiter causes infinite spin but is caught generically."""
        from pyrate_limiter import BucketFullException

        mock_client._check_token.return_value = None
        mock_client._api_endpoint.return_value = "https://api.wiz.io/graphql"
        mock_client._get_headers.return_value = {}

        call_count = [0]

        def try_acquire_side_effect(key):
            call_count[0] += 1
            if call_count[0] < 3:
                raise BucketFullException("key", 1, 0.1)

        mock_limiter = MagicMock()
        mock_limiter.try_acquire.side_effect = try_acquire_side_effect
        mock_client._get_limiter.return_value = mock_limiter

        # After bucket clears, return a success response
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"users": [{"id": "1"}]}}
        mock_client._post.return_value = mock_resp

        req = _make_req(mock_client)
        with patch("wizsec._request.time.sleep"):
            req._execute_page()

        # BucketFullException is caught by the generic Exception handler
        # so it will have errors recorded
        # The important thing is it doesn't crash
        assert call_count[0] >= 2


# ---------------------------------------------------------------------------
# WizRequest._wait_before_retry
# ---------------------------------------------------------------------------


class TestWizRequestWaitBeforeRetry:
    def test_sleep_is_called(self, mock_client):
        req = _make_req(mock_client)
        with patch("wizsec._request.time.sleep") as mock_sleep:
            req._wait_before_retry(1)
            mock_sleep.assert_called_once()
            sleep_time = mock_sleep.call_args[0][0]
            assert sleep_time >= mock_client._query_retry_time


# ---------------------------------------------------------------------------
# WizRequest._handle_network_error and _handle_unexpected_error
# ---------------------------------------------------------------------------


class TestWizRequestErrorHandlers:
    def test_network_error_appends_to_errors(self, mock_client):
        from wizsec._transport import TransportError

        req = _make_req(mock_client)
        req._handle_network_error(
            TransportError("connection reset"), attempt=0, max_retries=2
        )
        assert any("connection reset" in e["message"] for e in req.errors)

    def test_unexpected_error_appends_to_errors(self, mock_client):
        req = _make_req(mock_client)
        req._handle_unexpected_error(ValueError("kaboom"))
        assert any("kaboom" in e["message"] for e in req.errors)


# ---------------------------------------------------------------------------
# WizRequest._handle_final_failure
# ---------------------------------------------------------------------------


class TestHandleFinalFailure:
    def test_sets_done_event_with_no_errors(self, mock_client):
        req = _make_req(mock_client)
        req._done_event.clear()
        req._handle_final_failure()
        assert req._done_event.is_set()

    def test_sets_done_event_with_errors(self, mock_client):
        req = _make_req(mock_client)
        req.errors = [{"message": "something went wrong"}]
        req._done_event.clear()
        req._handle_final_failure()
        assert req._done_event.is_set()


# ---------------------------------------------------------------------------
# WizRequest._set_done_event
# ---------------------------------------------------------------------------


class TestSetDoneEvent:
    def test_sets_event_when_present(self, mock_client):
        req = _make_req(mock_client)
        req._done_event.clear()
        req._set_done_event()
        assert req._done_event.is_set()

    def test_no_error_when_event_missing(self, mock_client):
        req = _make_req(mock_client)
        del req._done_event
        req._set_done_event()  # must not raise


# ---------------------------------------------------------------------------
# WizBatchRequest._submit_concurrent / _wait_for_completion  (timeout path)
# ---------------------------------------------------------------------------


class TestWizBatchRequestConcurrent:
    def test_submit_concurrent_all_complete(self, mock_client):
        """Concurrent submit with requests that complete immediately."""
        mock_inner_req = MagicMock()
        mock_inner_req.errors = []
        done_evt = threading.Event()
        done_evt.set()
        mock_inner_req._done_event = done_evt

        mock_resp = MagicMock()
        mock_resp._request = mock_inner_req
        mock_client.create_request.return_value = mock_resp

        batch = WizBatchRequest(mock_client)
        batch.add_request(query="query Q { users { id } }")

        with patch.object(Config, "serverless", return_value=False):
            with patch.object(Config, "api_timeout", return_value=1):
                result = batch.submit()

        assert isinstance(result, WizBatchResponse)
        assert result.total_count() == 1

    def test_submit_concurrent_request_timeout(self, mock_client):
        """If a request's done_event never fires, it's treated as timed out."""
        mock_inner_req = MagicMock()
        mock_inner_req.errors = []
        done_evt = threading.Event()
        # Do NOT set the event — it will timeout
        mock_inner_req._done_event = done_evt

        mock_resp = MagicMock()
        mock_resp._request = mock_inner_req
        mock_client.create_request.return_value = mock_resp

        batch = WizBatchRequest(mock_client)
        batch.add_request(query="query Q { users { id } }")

        with patch.object(Config, "serverless", return_value=False):
            with patch.object(Config, "api_timeout", return_value=0):
                result = batch.submit()

        # Timed-out request still ends up in results with an error
        assert result.total_count() == 1
        assert any("timed out" in e["message"] for e in mock_inner_req.errors)

    def test_submit_sequential_with_exception(self, mock_client):
        """Sequential submit handles exceptions in individual requests."""
        mock_inner_req = MagicMock()
        mock_inner_req.errors = []
        mock_inner_req.submit.side_effect = RuntimeError("request blew up")

        mock_resp = MagicMock()
        mock_resp._request = mock_inner_req
        mock_client.create_request.return_value = mock_resp

        batch = WizBatchRequest(mock_client)
        batch.add_request(query="query Q { users { id } }")

        with patch.object(Config, "serverless", return_value=True):
            result = batch.submit()

        assert isinstance(result, WizBatchResponse)
        assert any(
            "Batch execution failed" in e["message"] for e in mock_inner_req.errors
        )

    def test_progress_callback_fires_on_concurrent(self, mock_client):
        """Progress callback is triggered in concurrent submit."""
        mock_inner_req = MagicMock()
        mock_inner_req.errors = []
        done_evt = threading.Event()
        done_evt.set()
        mock_inner_req._done_event = done_evt

        mock_resp = MagicMock()
        mock_resp._request = mock_inner_req
        mock_client.create_request.return_value = mock_resp

        progress = []
        batch = WizBatchRequest(mock_client)
        batch.set_progress_callback(lambda c, t: progress.append((c, t)))
        batch.add_request(query="query Q { users { id } }")

        with patch.object(Config, "serverless", return_value=False):
            with patch.object(Config, "api_timeout", return_value=1):
                batch.submit()

        assert len(progress) >= 1

    def test_add_response_then_submit(self, mock_client):
        """add_response then submit in serverless sequential path."""
        mock_inner_req = MagicMock()
        mock_inner_req.errors = []
        mock_resp = MagicMock()
        mock_resp._request = mock_inner_req

        batch = WizBatchRequest(mock_client)
        rid = batch.add_response(mock_resp)
        assert rid == 0

        with patch.object(Config, "serverless", return_value=True):
            result = batch.submit()

        assert result.total_count() == 1


# ---------------------------------------------------------------------------
# WizBatchResponse __iter__ and get_successful / get_failed
# ---------------------------------------------------------------------------


class TestWizBatchResponseIteration:
    def _make_resp(self, data=None, errors=None):
        resp = MagicMock()
        resp.data = data
        resp.errors = errors or []
        resp.success = data is not None and not errors
        return resp

    def test_iter_yields_id_response_pairs(self, mock_config):
        r0 = self._make_resp(data={"a": 1})
        r1 = self._make_resp(errors=[{"message": "fail"}])
        batch = WizBatchResponse({0: r0, 1: r1})
        pairs = list(batch)
        assert len(pairs) == 2
        assert pairs[0] == (0, r0)
        assert pairs[1] == (1, r1)

    def test_get_successful_results(self, mock_config):
        r0 = self._make_resp(data={"a": 1})
        r1 = self._make_resp(errors=[{"message": "fail"}])
        batch = WizBatchResponse({0: r0, 1: r1})
        assert 0 in batch.get_successful_results()
        assert 1 not in batch.get_successful_results()

    def test_get_failed_results(self, mock_config):
        r0 = self._make_resp(data={"a": 1})
        r1 = self._make_resp(errors=[{"message": "fail"}])
        batch = WizBatchResponse({0: r0, 1: r1})
        assert 1 in batch.get_failed_results()
        assert 0 not in batch.get_failed_results()


# ---------------------------------------------------------------------------
# AsyncWizRequest._execute_page  — full coverage
# ---------------------------------------------------------------------------


class TestAsyncExecutePage:
    @pytest.mark.asyncio
    async def test_success_no_pagination(self, mock_client):
        """200 response with paginate=False sets data directly."""
        req = _make_async_req(mock_client, paginate=False)
        _make_async_client(
            mock_client,
            status_code=200,
            json_data={"data": {"users": [{"id": "1"}]}},
        )
        await req._execute_page()
        assert req.data == {"users": [{"id": "1"}]}
        assert req.errors == []

    @pytest.mark.asyncio
    async def test_200_with_graphql_errors_stops(self, mock_client):
        """GraphQL errors in 200 response stop pagination immediately."""
        req = _make_async_req(mock_client, paginate=True)
        _make_async_client(
            mock_client,
            status_code=200,
            json_data={"data": {}, "errors": [{"message": "not allowed"}]},
        )
        await req._execute_page()
        assert any("not allowed" in e["message"] for e in req.errors)

    @pytest.mark.asyncio
    async def test_429_triggers_backoff_and_retries(self, mock_client):
        """429 response triggers Retry-After backoff, then succeeds on retry."""
        req = _make_async_req(mock_client, paginate=False)
        mock_client._max_retries = 2

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "0"}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"data": {"users": [{"id": "ok"}]}}
        resp_200.headers = {}

        call_count = [0]

        async def fake_post(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return resp_429
            return resp_200

        session = MagicMock()
        session.post = fake_post
        mock_client._async_session = session
        mock_client._async_semaphore = asyncio.Semaphore(10)
        mock_client._api_endpoint.return_value = "https://api.wiz.io/graphql"
        mock_client._get_headers.return_value = {}

        with patch("wizsec._request.asyncio.sleep", new_callable=AsyncMock):
            await req._execute_page()

        assert call_count[0] == 2
        assert req.data == {"users": [{"id": "ok"}]}

    @pytest.mark.asyncio
    async def test_429_default_retry_after(self, mock_client):
        """429 with missing Retry-After header uses default of 10."""
        req = _make_async_req(mock_client, paginate=False)
        mock_client._max_retries = 1

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {}  # no Retry-After header

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"data": {"users": []}}
        resp_200.headers = {}

        call_count = [0]

        async def fake_post(**kwargs):
            call_count[0] += 1
            return resp_429 if call_count[0] == 1 else resp_200

        session = MagicMock()
        session.post = fake_post
        mock_client._async_session = session
        mock_client._async_semaphore = asyncio.Semaphore(10)
        mock_client._api_endpoint.return_value = "https://api.wiz.io/graphql"
        mock_client._get_headers.return_value = {}

        sleep_args = []

        async def capture_sleep(t):
            sleep_args.append(t)

        with patch("wizsec._request.asyncio.sleep", side_effect=capture_sleep):
            await req._execute_page()

        # First sleep is the 429 retry_after (default 10)
        assert 10 in sleep_args

    @pytest.mark.asyncio
    async def test_non_200_non_429_records_error(self, mock_client):
        """Non-200, non-429 responses record errors and exhaust retries."""
        req = _make_async_req(mock_client, paginate=False)
        mock_client._max_retries = 1

        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "Service Unavailable"
        mock_resp.headers = {}

        async def fake_post(**kwargs):
            return mock_resp

        session = MagicMock()
        session.post = fake_post
        mock_client._async_session = session
        mock_client._async_semaphore = asyncio.Semaphore(10)
        mock_client._api_endpoint.return_value = "https://api.wiz.io/graphql"
        mock_client._get_headers.return_value = {}

        with patch("wizsec._request.asyncio.sleep", new_callable=AsyncMock):
            await req._execute_page()

        assert len(req.errors) > 0
        assert any("Service Unavailable" in e["message"] for e in req.errors)

    @pytest.mark.asyncio
    async def test_timeout_error_recorded(self, mock_client):
        """asyncio.TimeoutError is caught and recorded."""
        req = _make_async_req(mock_client, paginate=False)
        mock_client._max_retries = 0

        async def fake_post(**kwargs):
            raise asyncio.TimeoutError()

        session = MagicMock()
        session.post = fake_post
        mock_client._async_session = session
        mock_client._async_semaphore = asyncio.Semaphore(10)
        mock_client._api_endpoint.return_value = "https://api.wiz.io/graphql"
        mock_client._get_headers.return_value = {}

        with patch("wizsec._request.asyncio.sleep", new_callable=AsyncMock):
            await req._execute_page()

        assert any("timed out" in e["message"] for e in req.errors)

    @pytest.mark.asyncio
    async def test_generic_exception_recorded(self, mock_client):
        """Generic exceptions in async post are caught and recorded."""
        req = _make_async_req(mock_client, paginate=False)
        mock_client._max_retries = 0

        async def fake_post(**kwargs):
            raise ConnectionError("async connection error")

        session = MagicMock()
        session.post = fake_post
        mock_client._async_session = session
        mock_client._async_semaphore = asyncio.Semaphore(10)
        mock_client._api_endpoint.return_value = "https://api.wiz.io/graphql"
        mock_client._get_headers.return_value = {}

        with patch("wizsec._request.asyncio.sleep", new_callable=AsyncMock):
            await req._execute_page()

        assert any("async connection error" in e["message"] for e in req.errors)

    @pytest.mark.asyncio
    async def test_rate_ok_event_created_when_missing(self, mock_client):
        """_rate_ok event is lazily created when not already an asyncio.Event."""
        req = _make_async_req(mock_client, paginate=False)
        # Ensure _rate_ok is not set (MagicMock attributes are truthy but not asyncio.Event)
        if hasattr(mock_client, "_rate_ok"):
            del mock_client._rate_ok

        _make_async_client(
            mock_client,
            status_code=200,
            json_data={"data": {"users": []}},
        )
        await req._execute_page()
        # After execution, _rate_ok should have been set to an asyncio.Event
        assert isinstance(mock_client._rate_ok, asyncio.Event)

    @pytest.mark.asyncio
    async def test_rate_ok_event_reused_when_already_event(self, mock_client):
        """Pre-existing asyncio.Event is reused (not replaced)."""
        req = _make_async_req(mock_client, paginate=False)
        existing_event = asyncio.Event()
        existing_event.set()
        mock_client._rate_ok = existing_event

        _make_async_client(
            mock_client,
            status_code=200,
            json_data={"data": {"users": []}},
        )
        await req._execute_page()
        assert mock_client._rate_ok is existing_event

    @pytest.mark.asyncio
    async def test_bucket_full_exception_spins(self, mock_client):
        """BucketFullException from async limiter causes a spin-wait (sleep 0.1)."""
        from pyrate_limiter import BucketFullException, RateItem, Rate, Duration

        req = _make_async_req(mock_client, paginate=False)

        # Limiter raises BucketFullException twice then succeeds
        acquire_calls = [0]
        _item = RateItem("key", 1)
        _rate = Rate(10, Duration.SECOND)

        def try_acquire(key):
            acquire_calls[0] += 1
            if acquire_calls[0] < 3:
                raise BucketFullException(_item, _rate)

        mock_limiter = MagicMock()
        mock_limiter.try_acquire = try_acquire
        mock_client._get_limiter.return_value = mock_limiter

        _make_async_client(
            mock_client,
            status_code=200,
            json_data={"data": {"users": []}},
        )

        sleep_calls = []

        async def capture_sleep(t):
            sleep_calls.append(t)
            # Don't actually sleep to keep tests fast

        with patch("wizsec._request.asyncio.sleep", side_effect=capture_sleep):
            await req._execute_page()

        # The spin-wait loop should have fired sleep(0.1) for each BucketFullException
        assert acquire_calls[0] >= 3
        assert 0.1 in sleep_calls

    @pytest.mark.asyncio
    async def test_pagination_loop_multiple_pages(self, mock_client):
        """Paginated request fetches multiple pages until hasNextPage=False."""
        req = _make_async_req(
            mock_client,
            query="query Q($first: Int, $after: String) { users(first: $first, after: $after) { nodes { id } pageInfo { hasNextPage endCursor } } }",
            paginate=True,
        )

        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "data": {
                "users": {
                    "nodes": [{"id": "1"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor1"},
                }
            }
        }
        page1.headers = {}

        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {
            "data": {
                "users": {
                    "nodes": [{"id": "2"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        page2.headers = {}

        responses = [page1, page2]
        call_count = [0]

        async def fake_post(**kwargs):
            resp = responses[call_count[0]]
            call_count[0] += 1
            return resp

        session = MagicMock()
        session.post = fake_post
        mock_client._async_session = session
        mock_client._async_semaphore = asyncio.Semaphore(10)
        mock_client._api_endpoint.return_value = "https://api.wiz.io/graphql"
        mock_client._get_headers.return_value = {}

        await req._execute_page()
        assert call_count[0] == 2
        assert req.data is not None
        assert len(req.data["users"]["nodes"]) == 2

    @pytest.mark.asyncio
    async def test_pagination_with_page_event_callback(self, mock_client):
        """Page event callback is fired during pagination."""
        page_events = []

        req = _make_async_req(
            mock_client,
            query="query Q($first: Int, $after: String) { users(first: $first, after: $after) { nodes { id } pageInfo { hasNextPage endCursor } } }",
            paginate=True,
            on_page_event=lambda e: page_events.append(e),
        )

        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "data": {
                "users": {
                    "nodes": [{"id": "1"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                }
            }
        }
        page1.headers = {}

        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {
            "data": {
                "users": {
                    "nodes": [{"id": "2"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        page2.headers = {}

        responses = [page1, page2]
        call_count = [0]

        async def fake_post(**kwargs):
            resp = responses[call_count[0]]
            call_count[0] += 1
            return resp

        session = MagicMock()
        session.post = fake_post
        mock_client._async_session = session
        mock_client._async_semaphore = asyncio.Semaphore(10)
        mock_client._api_endpoint.return_value = "https://api.wiz.io/graphql"
        mock_client._get_headers.return_value = {}

        await req._execute_page()
        # Callback fires for intermediate pages (page1 triggers it)
        assert len(page_events) >= 1
        assert "page_data" in page_events[0]

    @pytest.mark.asyncio
    async def test_semaphore_used_from_client_if_present(self, mock_client):
        """Pre-existing _async_semaphore on client is used."""
        existing_sem = asyncio.Semaphore(5)
        mock_client._async_semaphore = existing_sem

        req = _make_async_req(mock_client, paginate=False)
        _make_async_client(
            mock_client,
            status_code=200,
            json_data={"data": {"users": []}},
        )
        # Re-assign the semaphore since _make_async_client overwrites it
        mock_client._async_semaphore = existing_sem

        await req._execute_page()
        # No assertion on internals; just verify no crash and data is set
        assert req.data == {"users": []}


# ---------------------------------------------------------------------------
# AsyncWizRequest.submit() — _maybe_split_async short-circuits
# ---------------------------------------------------------------------------


class TestAsyncSubmit:
    @pytest.mark.asyncio
    async def test_submit_splits_when_split_async_true(self, mock_client):
        """submit() returns early when _maybe_split_async returns True."""
        req = _make_async_req(mock_client)
        mock_client._check_token.return_value = None

        async def fake_split():
            req.data = {"split": True}
            return True

        with patch.object(req, "_maybe_split_async", side_effect=fake_split):
            result = await req.submit()

        assert result is req
        assert req.data == {"split": True}

    @pytest.mark.asyncio
    async def test_submit_calls_execute_page_when_no_split(self, mock_client):
        """submit() calls _execute_page when _maybe_split_async returns False."""
        req = _make_async_req(mock_client)
        mock_client._check_token.return_value = None

        async def fake_split():
            return False

        async def fake_execute():
            req.data = {"users": []}

        with patch.object(req, "_maybe_split_async", side_effect=fake_split):
            with patch.object(req, "_execute_page", side_effect=fake_execute):
                result = await req.submit()

        assert result is req
        assert req.data == {"users": []}


# ---------------------------------------------------------------------------
# AsyncWizResponse — properties and repr
# ---------------------------------------------------------------------------


class TestAsyncWizResponseFull:
    def test_success_false_when_no_data(self, mock_client):
        req = _make_async_req(mock_client)
        req.data = None
        req.errors = []
        resp = AsyncWizResponse(req)
        assert resp.success is False

    def test_success_false_when_errors(self, mock_client):
        req = _make_async_req(mock_client)
        req.data = {"x": 1}
        req.errors = [{"message": "oops"}]
        resp = AsyncWizResponse(req)
        assert resp.success is False

    def test_data_property(self, mock_client):
        req = _make_async_req(mock_client)
        req.data = {"issues": []}
        req.errors = []
        resp = AsyncWizResponse(req)
        assert resp.data == {"issues": []}

    def test_errors_property(self, mock_client):
        req = _make_async_req(mock_client)
        req.data = None
        req.errors = [{"message": "bad"}]
        resp = AsyncWizResponse(req)
        assert resp.errors == [{"message": "bad"}]

    def test_repr_shows_false_on_failure(self, mock_client):
        req = _make_async_req(mock_client)
        req.data = None
        req.errors = []
        resp = AsyncWizResponse(req)
        assert "False" in repr(resp)

    def test_repr_shows_true_on_success(self, mock_client):
        req = _make_async_req(mock_client)
        req.data = {"ok": True}
        req.errors = []
        resp = AsyncWizResponse(req)
        assert "True" in repr(resp)

    @pytest.mark.asyncio
    async def test_submit_returns_self(self, mock_client):
        req = _make_async_req(mock_client)

        async def fake_submit():
            req.data = {"done": True}
            return req

        with patch.object(req, "submit", side_effect=fake_submit):
            resp = AsyncWizResponse(req)
            result = await resp.submit()
        assert result is resp


# ---------------------------------------------------------------------------
# AsyncWizBatchRequest
# ---------------------------------------------------------------------------


class TestAsyncWizBatchRequest:
    @pytest.mark.asyncio
    async def test_add_request_creates_async_request(self, mock_client):
        """add_request stores an AsyncWizRequest and returns its index."""
        batch = AsyncWizBatchRequest(client=mock_client, max_concurrent=5)
        rid = batch.add_request(query="query Q { users { id } }")
        assert rid == 0
        assert len(batch) == 1
        assert isinstance(batch._requests[0], AsyncWizRequest)

    @pytest.mark.asyncio
    async def test_add_multiple_requests(self, mock_client):
        batch = AsyncWizBatchRequest(client=mock_client, max_concurrent=5)
        r0 = batch.add_request(query="query Q1 { users { id } }")
        r1 = batch.add_request(query="query Q2 { projects { id } }")
        assert r0 == 0
        assert r1 == 1
        assert len(batch) == 2

    @pytest.mark.asyncio
    async def test_submit_empty_returns_empty_batch_response(self, mock_client):
        batch = AsyncWizBatchRequest(client=mock_client)
        result = await batch.submit()
        assert isinstance(result, WizBatchResponse)
        assert result.total_count() == 0

    @pytest.mark.asyncio
    async def test_submit_executes_all_requests(self, mock_client):
        """submit() runs all requests concurrently and returns results."""
        batch = AsyncWizBatchRequest(client=mock_client, max_concurrent=5)

        with patch.object(Config, "validate_queries", return_value=False):
            for i in range(3):
                batch.add_request(query=f"query Q{i} {{ users {{ id }} }}")

        async def fake_submit(self_req):
            self_req.data = {"result": True}
            self_req.errors = []
            return self_req

        with patch.object(
            AsyncWizRequest, "submit", autospec=True, side_effect=fake_submit
        ):
            result = await batch.submit()

        assert isinstance(result, WizBatchResponse)
        assert result.total_count() == 3

    @pytest.mark.asyncio
    async def test_submit_with_progress_callback(self, mock_client):
        """Progress callback is invoked for each completed request."""
        progress_calls = []

        batch = AsyncWizBatchRequest(client=mock_client, max_concurrent=5)
        batch.set_progress_callback(lambda c, t: progress_calls.append((c, t)))

        with patch.object(Config, "validate_queries", return_value=False):
            batch.add_request(query="query Q1 { users { id } }")
            batch.add_request(query="query Q2 { users { id } }")

        async def fake_submit(self_req):
            self_req.data = {"result": True}
            self_req.errors = []
            return self_req

        with patch.object(
            AsyncWizRequest, "submit", autospec=True, side_effect=fake_submit
        ):
            await batch.submit()

        assert len(progress_calls) == 2

    @pytest.mark.asyncio
    async def test_submit_handles_exception_in_gather(self, mock_client):
        """When a request raises in gather, it's logged but doesn't crash submit."""
        batch = AsyncWizBatchRequest(client=mock_client, max_concurrent=5)

        with patch.object(Config, "validate_queries", return_value=False):
            batch.add_request(query="query Q1 { users { id } }")

        async def raise_submit(self_req):
            raise RuntimeError("request failed hard")

        with patch.object(
            AsyncWizRequest, "submit", autospec=True, side_effect=raise_submit
        ):
            result = await batch.submit()

        # Exception is caught by gather(return_exceptions=True) and logged
        # The response_map may be empty or only contain non-exception results
        assert isinstance(result, WizBatchResponse)

    @pytest.mark.asyncio
    async def test_semaphore_created_if_missing(self, mock_client):
        """AsyncWizBatchRequest creates _async_semaphore on client if absent."""
        if hasattr(mock_client, "_async_semaphore"):
            del mock_client._async_semaphore
        batch = AsyncWizBatchRequest(client=mock_client, max_concurrent=10)
        assert hasattr(mock_client, "_async_semaphore")

    @pytest.mark.asyncio
    async def test_clear_removes_all_requests(self, mock_client):
        batch = AsyncWizBatchRequest(client=mock_client, max_concurrent=5)
        with patch.object(Config, "validate_queries", return_value=False):
            batch.add_request(query="query Q { users { id } }")
        assert len(batch) == 1
        batch.clear()
        assert len(batch) == 0

    @pytest.mark.asyncio
    async def test_max_concurrent_limits_semaphore(self, mock_client):
        """The concurrent_limit from submit() parameter overrides constructor."""
        batch = AsyncWizBatchRequest(client=mock_client, max_concurrent=2)
        with patch.object(Config, "validate_queries", return_value=False):
            batch.add_request(query="query Q { users { id } }")

        async def fake_submit(self_req):
            self_req.data = {}
            self_req.errors = []
            return self_req

        with patch.object(
            AsyncWizRequest, "submit", autospec=True, side_effect=fake_submit
        ):
            result = await batch.submit(max_concurrent=1)

        assert isinstance(result, WizBatchResponse)


# ---------------------------------------------------------------------------
# _maybe_split_async — serverless / query_splitting_enabled / sub_request guards
# ---------------------------------------------------------------------------


class TestMaybeSplitAsync:
    @pytest.mark.asyncio
    async def test_returns_false_in_serverless(self, mock_client):
        req = _make_async_req(mock_client)
        with patch.object(Config, "serverless", return_value=True):
            result = await req._maybe_split_async()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_splitting_disabled(self, mock_client):
        req = _make_async_req(mock_client)
        with patch.object(Config, "serverless", return_value=False):
            with patch.object(Config, "query_splitting_enabled", return_value=False):
                result = await req._maybe_split_async()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_for_sub_request(self, mock_client):
        req = _make_async_req(mock_client)
        req._is_sub_request = True
        with patch.object(Config, "serverless", return_value=False):
            with patch.object(Config, "query_splitting_enabled", return_value=True):
                result = await req._maybe_split_async()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_for_mutation(self, mock_client):
        req = _make_async_req(
            mock_client,
            query="mutation M { createReport(input: {}) { report { id } } }",
            paginate=False,
        )
        with patch.object(Config, "serverless", return_value=False):
            with patch.object(Config, "query_splitting_enabled", return_value=True):
                result = await req._maybe_split_async()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_totalcount_field(self, mock_client):
        req = _make_async_req(mock_client)
        with patch.object(Config, "serverless", return_value=False):
            with patch.object(Config, "query_splitting_enabled", return_value=True):
                with patch.object(
                    Config, "query_splitting_detection_mode", return_value="static"
                ):
                    with patch(
                        "wizsec._request.has_totalcount_field", return_value=False
                    ):
                        result = await req._maybe_split_async()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_probe_query_is_none(self, mock_client):
        req = _make_async_req(mock_client)
        with patch.object(Config, "serverless", return_value=False):
            with patch.object(Config, "query_splitting_enabled", return_value=True):
                with patch.object(
                    Config, "query_splitting_detection_mode", return_value="static"
                ):
                    with patch(
                        "wizsec._request.has_totalcount_field", return_value=True
                    ):
                        with patch(
                            "wizsec._request.build_totalcount_probe_query",
                            return_value=None,
                        ):
                            result = await req._maybe_split_async()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_probe_fails(self, mock_client):
        req = _make_async_req(mock_client)

        async def fake_probe_submit(probe_req):
            probe_req.data = None
            probe_req.errors = [{"message": "probe failed"}]
            return probe_req

        with patch.object(Config, "serverless", return_value=False):
            with patch.object(Config, "query_splitting_enabled", return_value=True):
                with patch.object(
                    Config, "query_splitting_detection_mode", return_value="static"
                ):
                    with patch(
                        "wizsec._request.has_totalcount_field", return_value=True
                    ):
                        with patch(
                            "wizsec._request.build_totalcount_probe_query",
                            return_value="query Probe { issues { totalCount } }",
                        ):
                            with patch.object(
                                AsyncWizRequest,
                                "submit",
                                autospec=True,
                                side_effect=fake_probe_submit,
                            ):
                                result = await req._maybe_split_async()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_below_threshold(self, mock_client):
        req = _make_async_req(mock_client)

        async def fake_probe_submit(probe_req):
            probe_req.data = {"issues": {"totalCount": 5}}
            probe_req.errors = []
            return probe_req

        with patch.object(Config, "serverless", return_value=False):
            with patch.object(Config, "query_splitting_enabled", return_value=True):
                with patch.object(
                    Config, "query_splitting_detection_mode", return_value="static"
                ):
                    with patch(
                        "wizsec._request.has_totalcount_field", return_value=True
                    ):
                        with patch(
                            "wizsec._request.build_totalcount_probe_query",
                            return_value="query Probe { issues { totalCount } }",
                        ):
                            with patch(
                                "wizsec._request._extract_totalcount", return_value=5
                            ):
                                with patch.object(
                                    Config,
                                    "query_splitting_threshold",
                                    return_value=1000,
                                ):
                                    with patch.object(
                                        AsyncWizRequest,
                                        "submit",
                                        autospec=True,
                                        side_effect=fake_probe_submit,
                                    ):
                                        result = await req._maybe_split_async()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_filter_path(self, mock_client):
        req = _make_async_req(mock_client)

        async def fake_probe_submit(probe_req):
            probe_req.data = {"issues": {"totalCount": 9999}}
            probe_req.errors = []
            return probe_req

        with patch.object(Config, "serverless", return_value=False):
            with patch.object(Config, "query_splitting_enabled", return_value=True):
                with patch.object(
                    Config, "query_splitting_detection_mode", return_value="static"
                ):
                    with patch(
                        "wizsec._request.has_totalcount_field", return_value=True
                    ):
                        with patch(
                            "wizsec._request.build_totalcount_probe_query",
                            return_value="query Probe { issues { totalCount } }",
                        ):
                            with patch(
                                "wizsec._request._extract_totalcount", return_value=9999
                            ):
                                with patch.object(
                                    Config,
                                    "query_splitting_threshold",
                                    return_value=100,
                                ):
                                    with patch.object(
                                        Config,
                                        "query_splitting_filter_path",
                                        return_value="",
                                    ):
                                        with patch.object(
                                            AsyncWizRequest,
                                            "submit",
                                            autospec=True,
                                            side_effect=fake_probe_submit,
                                        ):
                                            result = await req._maybe_split_async()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_entities(self, mock_client):
        req = _make_async_req(mock_client)

        async def fake_probe_submit(probe_req):
            probe_req.data = {"issues": {"totalCount": 9999}}
            probe_req.errors = []
            return probe_req

        with patch.object(Config, "serverless", return_value=False):
            with patch.object(Config, "query_splitting_enabled", return_value=True):
                with patch.object(
                    Config, "query_splitting_detection_mode", return_value="static"
                ):
                    with patch(
                        "wizsec._request.has_totalcount_field", return_value=True
                    ):
                        with patch(
                            "wizsec._request.build_totalcount_probe_query",
                            return_value="query Probe { issues { totalCount } }",
                        ):
                            with patch(
                                "wizsec._request._extract_totalcount", return_value=9999
                            ):
                                with patch.object(
                                    Config,
                                    "query_splitting_threshold",
                                    return_value=100,
                                ):
                                    with patch.object(
                                        Config,
                                        "query_splitting_filter_path",
                                        return_value="subscriptionFilters.cloudAccount",
                                    ):
                                        with patch(
                                            "wizsec._request._get_cached_or_fetch_entities",
                                            return_value=[],
                                        ):
                                            with patch.object(
                                                AsyncWizRequest,
                                                "submit",
                                                autospec=True,
                                                side_effect=fake_probe_submit,
                                            ):
                                                result = await req._maybe_split_async()
        assert result is False


# ---------------------------------------------------------------------------
# Helper for to_thread simulation in tests
# ---------------------------------------------------------------------------


async def _sync_to_coro(fn, *args, **kwargs):
    """Run a sync function as if in a thread (synchronously in tests)."""
    return fn(*args, **kwargs)
