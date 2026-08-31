"""Additional tests for wizsec/_request.py to improve coverage.

Targets the uncovered branches in:
  - AsyncWizRequest._execute_page()  (rate limiting, 429, timeout, pagination)
  - AsyncWizBatchRequest             (add_request, submit, progress, exceptions)
  - WizRequest._execute_page()       (4xx non-retryable, local limiter full path)
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
    client._env_state.rate_backoff_remaining.return_value = 0

    mock_limiter = MagicMock()
    mock_limiter.try_acquire = MagicMock(return_value=True)
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
# WizRequest._execute_page  (local limiter full path)
# ---------------------------------------------------------------------------


class TestExecutePageBucketFull:
    def test_limiter_full_waits_without_recording_error(self, mock_client):
        """A full local limiter (try_acquire -> False) waits without poisoning success."""
        mock_client._check_token.return_value = None
        mock_client._api_endpoint.return_value = "https://api.wiz.io/graphql"
        mock_client._get_headers.return_value = {}

        call_count = [0]

        def try_acquire_side_effect(key, **kwargs):
            call_count[0] += 1
            return call_count[0] >= 3

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

        assert call_count[0] >= 2
        assert req.errors == []
        assert req.data == {"users": [{"id": "1"}]}


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
    async def test_async_rate_limit_uses_environment_backoff_state(self, mock_client):
        """Async request checks shared environment backoff instead of client-local state."""
        req = _make_async_req(mock_client, paginate=False)

        _make_async_client(
            mock_client,
            status_code=200,
            json_data={"data": {"users": []}},
        )
        await req._execute_page()
        mock_client._env_state.rate_backoff_remaining.assert_called()

    @pytest.mark.asyncio
    async def test_async_429_sets_environment_backoff(self, mock_client):
        """429 responses set shared environment backoff without consuming retries."""
        from wizsec.config import Config

        max_waits = Config.rate_limit_max_backoff_waits()
        req = _make_async_req(mock_client, paginate=False)
        mock_client._max_retries = 0

        _make_async_client(
            mock_client,
            status_code=429,
        )
        with patch("wizsec._request.asyncio.sleep", new_callable=AsyncMock):
            await req._execute_page()
        # Backoff is set on every 429; persistence is bounded by the wait cap
        # (not by max_retries), and giving up records an explicit error.
        mock_client._env_state.set_rate_backoff.assert_called_with(10)
        assert mock_client._env_state.set_rate_backoff.call_count == max_waits + 1
        assert any("gave up" in e["message"] for e in req.errors)

    @pytest.mark.asyncio
    async def test_limiter_full_spins(self, mock_client):
        """A full async limiter (try_acquire -> False) causes a short spin-wait."""
        req = _make_async_req(mock_client, paginate=False)

        # Limiter reports full twice then grants a slot
        acquire_calls = [0]

        def try_acquire(key, **kwargs):
            acquire_calls[0] += 1
            return acquire_calls[0] >= 3

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

        # The spin-wait loop should have fired a short sleep per full-limiter miss
        from wizsec._request import _LIMITER_SPIN_SECONDS

        assert acquire_calls[0] >= 3
        assert _LIMITER_SPIN_SECONDS in sleep_calls

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

        assert isinstance(result, WizBatchResponse)
        assert result.total_count() == 1
        failed = result.get_result(0)
        assert failed is not None
        assert failed.success is False
        assert "request failed hard" in failed.errors[0]["message"]

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
# WizRequest._report_workflow  (bounded polling, terminal statuses)
# ---------------------------------------------------------------------------


class TestReportWorkflowPolling:
    def _prime_report(self, mock_client, source="createReport"):
        # The query has to be the real mutation now: _report_workflow reads the
        # report id out of data[<source>], so a stand-in "query Q { users { id } }"
        # no longer lines up with a _response keyed by the mutation name.
        req = _make_req(
            mock_client, query=f"mutation M {{ {source} {{ report {{ id }} }} }}"
        )
        req._response = {"data": {source: {"report": {"id": "rpt-1"}}}}
        return req

    def test_polling_gives_up_after_max_failed_polls(self, mock_client):
        from wizsec.config import Config

        req = self._prime_report(mock_client)
        poll_count = [0]

        def fake_execute():
            poll_count[0] += 1
            req.data = None  # unsuccessful poll

        with (
            patch.object(req, "_execute_page", side_effect=fake_execute),
            patch("wizsec._request.time.sleep"),
        ):
            req._report_workflow(MagicMock())

        assert poll_count[0] == Config.report_max_retries() + 1
        assert any("polling failed" in e["message"] for e in req.errors)

    def test_failed_report_status_terminates(self, mock_client):
        req = self._prime_report(mock_client)

        def fake_execute():
            req.data = {
                "report": {"lastRun": {"status": "FAILED", "progress": 0, "url": None}}
            }

        with (
            patch.object(req, "_execute_page", side_effect=fake_execute),
            patch("wizsec._request.time.sleep"),
        ):
            req._report_workflow(MagicMock())

        assert any("ended with status FAILED" in e["message"] for e in req.errors)

    def test_transient_poll_failure_does_not_poison_success(self, mock_client):
        req = self._prime_report(mock_client)
        req.stream_report = False
        req.report_name = "r"
        calls = [0]

        def fake_execute():
            calls[0] += 1
            if calls[0] == 1:
                req.data = None  # one transient failure
                return
            req.data = {
                "report": {
                    "lastRun": {
                        "status": "COMPLETED",
                        "progress": 100,
                        "url": "https://dl.example",
                    }
                }
            }

        with (
            patch.object(req, "_execute_page", side_effect=fake_execute),
            patch.object(req, "_download_report", return_value=b"bytes"),
            patch("wizsec._request.time.sleep"),
        ):
            req._report_workflow(MagicMock())

        assert req.errors == []
        assert req.data["report_data"] == b"bytes"


# ---------------------------------------------------------------------------
# _get_cached_or_fetch_entities  (failure must not poison the session cache)
# ---------------------------------------------------------------------------


class TestGetCachedOrFetchEntities:
    def _patches(self):
        return (
            patch.object(
                Config, "query_splitting_split_by", return_value="cloudAccounts"
            ),
            patch.object(
                Config, "query_splitting_cache_subscriptions", return_value=True
            ),
        )

    def test_failed_fetch_is_not_cached(self, mock_client):
        from wizsec._registry import EnvironmentState
        from wizsec._request import _get_cached_or_fetch_entities

        env_state = EnvironmentState("split-cache-fail")
        mock_client._env_state = env_state

        fetch_req = MagicMock()
        fetch_req.success.return_value = False
        fetch_req.data = None

        p1, p2 = self._patches()
        with p1, p2, patch("wizsec._request.WizRequest", return_value=fetch_req):
            assert _get_cached_or_fetch_entities(mock_client) == []

        # a transient failure must leave the cache unset so the next
        # attempt fetches again
        assert env_state._cached_split_entities is None

        fetch_req.success.return_value = True
        fetch_req.data = {"cloudAccounts": {"nodes": [{"id": "acc-1"}]}}
        p1, p2 = self._patches()
        with p1, p2, patch("wizsec._request.WizRequest", return_value=fetch_req):
            assert _get_cached_or_fetch_entities(mock_client) == [{"id": "acc-1"}]
        assert env_state._cached_split_entities == [{"id": "acc-1"}]

    def test_cache_hit_skips_fetch(self, mock_client):
        from wizsec._registry import EnvironmentState
        from wizsec._request import _get_cached_or_fetch_entities

        env_state = EnvironmentState("split-cache-hit")
        env_state._cached_split_entities = [{"id": "cached"}]
        mock_client._env_state = env_state

        p1, p2 = self._patches()
        with p1, p2, patch("wizsec._request.WizRequest") as mock_req_cls:
            assert _get_cached_or_fetch_entities(mock_client) == [{"id": "cached"}]
            mock_req_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Report workflow: rerunReport gate, poll deadline, async rejection
# ---------------------------------------------------------------------------


class TestReportMutationGate:
    """rerunReport reached the workflow nowhere except the query setter, so it
    completed "successfully" with no report data attached and no warning."""

    @pytest.mark.parametrize("source", ["createReport", "rerunReport"])
    def test_report_mutations_trigger_the_workflow(self, mock_client, source):
        req = _make_req(
            mock_client, query=f"mutation M {{ {source} {{ report {{ id }} }} }}"
        )
        assert req._generate_report() is True

    @pytest.mark.parametrize(
        "query",
        [
            "query Q { users { id } }",
            "mutation M { deleteReport { report { id } } }",
            "query ReportDownloadUrl($reportId: ID!) { report(id: $reportId) { name } }",
        ],
    )
    def test_non_report_queries_do_not(self, mock_client, query):
        assert _make_req(mock_client, query=query)._generate_report() is False

    def test_workflow_reads_the_id_from_whichever_mutation_ran(self, mock_client):
        """The id path was hardcoded to data['createReport']."""
        req = _make_req(
            mock_client,
            query="mutation M { rerunReport { report { id } } }",
            report_request={"name": "R", "stream": False},
        )
        assert req._generate_report() is True  # populates stream_report/report_name
        req._response = {"data": {"rerunReport": {"report": {"id": "rerun-99"}}}}

        def fake_execute():
            req.data = {
                "report": {
                    "lastRun": {
                        "status": "COMPLETED",
                        "progress": 100,
                        "url": "https://example.com/r.csv",
                    }
                }
            }

        with (
            patch.object(req, "_execute_page", side_effect=fake_execute),
            patch.object(req, "_download_report", return_value=b"rows"),
            patch("wizsec._request.time.sleep"),
        ):
            req._report_workflow(MagicMock())

        assert req._report_id == "rerun-99"
        assert req.data["report_data"] == b"rows"

    def test_installing_the_polling_query_clears_the_gate(self, mock_client):
        """Guards the recursion: _poll_report_status re-enters _execute_page,
        which re-checks _generate_report against the polling query."""
        req = _make_req(
            mock_client, query="mutation M { rerunReport { report { id } } }"
        )
        assert req._generate_report() is True
        req.query = (
            "query ReportDownloadUrl($reportId: ID!) "
            "{ report(id: $reportId) { name } }"
        )
        assert req._generate_report() is False


class TestReportPollDeadline:
    """reports.max_retries only caps failed polls, and the status checks only
    exit on COMPLETED or a terminal failure, so a run stuck in a healthy
    non-terminal status polled forever."""

    def _prime(self, mock_client):
        req = _make_req(
            mock_client,
            query="mutation M { createReport { report { id } } }",
            report_request={"name": "R", "stream": False},
        )
        req._generate_report()  # populates stream_report/report_name
        req._response = {"data": {"createReport": {"report": {"id": "rpt-1"}}}}
        return req

    class _Clock:
        """A monotonic clock the test advances explicitly.

        wizsec._request does `import time`, so patching
        "wizsec._request.time.monotonic" replaces the attribute on the global
        time module -- every caller in the process sees it for the duration.
        A plain iter() of readings is therefore order-dependent on something
        the test does not control: one incidental clock read anywhere shifts
        the whole sequence, and the deadline check silently sees the wrong
        value. Advancing on demand makes extra reads harmless.
        """

        def __init__(self, start=0.0):
            self.now = start

        def __call__(self):
            return self.now

        def advance(self, seconds):
            self.now += seconds

    def _in_progress(self, req, clock=None, step=0.0):
        """Report a healthy in-progress poll, advancing the clock per poll."""

        def fake_execute():
            if clock is not None:
                clock.advance(step)
            req.data = {
                "report": {
                    "lastRun": {"status": "IN_PROGRESS", "progress": 10, "url": None}
                }
            }

        return fake_execute

    def test_endless_in_progress_run_hits_the_deadline(self, mock_client):
        req = self._prime(mock_client)
        # Each poll burns a quarter of the default 3600s deadline, so the run
        # stays healthy for a few rounds and then runs out of time.
        clock = self._Clock()
        with (
            patch.object(
                req,
                "_execute_page",
                side_effect=self._in_progress(req, clock, step=900.0),
            ),
            patch("wizsec._request.time.sleep"),
            patch("wizsec._request.time.monotonic", clock),
        ):
            result = req._report_workflow(MagicMock())

        assert result is req
        assert req.success() is False
        assert any("did not complete within" in e["message"] for e in req.errors)
        assert "IN_PROGRESS" in req.errors[-1]["message"]  # reports last status

    def test_deadline_sets_a_typed_timeout_error(self, mock_client):
        from wizsec.exceptions import WizTimeoutError

        req = self._prime(mock_client)
        clock = self._Clock()
        with (
            patch.object(
                req,
                "_execute_page",
                side_effect=self._in_progress(req, clock, step=2000.0),
            ),
            patch("wizsec._request.time.sleep"),
            patch("wizsec._request.time.monotonic", clock),
        ):
            req._report_workflow(MagicMock())

        assert isinstance(req.error, WizTimeoutError)

    def test_deadline_is_configurable(self, mock_client):
        req = self._prime(mock_client)
        clock = self._Clock()  # one poll pushes past a 30s deadline
        with (
            patch.object(Config, "report_timeout", return_value=30),
            patch.object(
                req,
                "_execute_page",
                side_effect=self._in_progress(req, clock, step=50.0),
            ),
            patch("wizsec._request.time.sleep"),
            patch("wizsec._request.time.monotonic", clock),
        ):
            req._report_workflow(MagicMock())

        assert any("within 30s" in e["message"] for e in req.errors)

    def test_a_run_that_completes_in_time_is_unaffected(self, mock_client):
        req = self._prime(mock_client)

        def fake_execute():
            req.data = {
                "report": {
                    "lastRun": {
                        "status": "COMPLETED",
                        "progress": 100,
                        "url": "https://example.com/r.csv",
                    }
                }
            }

        with (
            patch.object(req, "_execute_page", side_effect=fake_execute),
            patch.object(req, "_download_report", return_value=b"rows"),
            patch("wizsec._request.time.sleep"),
        ):
            req._report_workflow(MagicMock())

        assert req.errors == []
        assert req.data["report_data"] == b"rows"


class TestAsyncReportsAreRejected:
    """report_request was accepted, stored, and silently ignored: the async
    class has no report workflow at all."""

    def test_async_request_rejects_report_request(self, mock_client):
        from wizsec.exceptions import WizConfigurationError

        with patch.object(Config, "validate_queries", return_value=False):
            with pytest.raises(WizConfigurationError, match="sync-only"):
                AsyncWizRequest(
                    client=mock_client,
                    query="mutation M { createReport { report { id } } }",
                    report_request={"name": "R", "stream": True},
                )

    def test_async_batch_passthrough_is_rejected_too(self, mock_client):
        """add_request forwards **kwargs straight to AsyncWizRequest."""
        from wizsec.exceptions import WizConfigurationError

        batch = AsyncWizBatchRequest(mock_client)
        with patch.object(Config, "validate_queries", return_value=False):
            with pytest.raises(WizConfigurationError, match="sync-only"):
                batch.add_request(
                    query="mutation M { createReport { report { id } } }",
                    report_request={"name": "R"},
                )

    @pytest.mark.parametrize("falsy", [None, {}])
    def test_absent_or_empty_report_request_is_fine(self, mock_client, falsy):
        with patch.object(Config, "validate_queries", return_value=False):
            req = AsyncWizRequest(
                client=mock_client,
                query="query Q { users { id } }",
                report_request=falsy,
            )
        assert req._report_request == {}

    def test_sync_request_still_accepts_report_request(self, mock_client):
        req = _make_req(
            mock_client,
            query="mutation M { createReport { report { id } } }",
            report_request={"name": "R", "stream": False},
        )
        assert req._generate_report() is True
        assert req.report_name == "R"
