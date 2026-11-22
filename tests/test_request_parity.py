"""Sync and async requests share one set of response decisions.

Each scenario scripts the same sequence of HTTP responses and runs it through
WizRequest and AsyncWizRequest; both must reach the same data, errors and
number of HTTP calls.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from wizsec._request import AsyncWizRequest, WizRequest
from wizsec.config import Config

QUERY = """
query Users($first: Int, $after: String) {
  users(first: $first, after: $after) {
    nodes { id }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def _resp(status, body=None, text="", headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    r.text = text
    r.headers = headers or {}
    return r


def _page(ids, cursor=None):
    return _resp(
        200,
        {
            "data": {
                "users": {
                    "nodes": [{"id": i} for i in ids],
                    "pageInfo": {
                        "hasNextPage": cursor is not None,
                        "endCursor": cursor,
                    },
                }
            }
        },
    )


@pytest.fixture()
def client(mock_config):
    c = MagicMock()
    c.environment = "gov"
    c._query_retry_time = 0
    c._max_retries = 2
    c._env_state.rate_backoff_remaining.return_value = 0
    limiter = MagicMock()
    limiter.try_acquire.return_value = True
    c._get_limiter.return_value = limiter
    c._api_endpoint.return_value = "https://api.example/graphql"
    c._get_headers.return_value = {}
    c._async_semaphore = None
    del c._async_semaphore
    return c


def _run_sync(client, responses, **kw):
    """Drive WizRequest through its queue by re-running _execute_page on enqueue."""
    script = iter(responses)
    calls = []

    def post(**kwargs):
        calls.append(kwargs)
        return next(script)

    client._post.side_effect = post
    with patch.object(Config, "validate_queries", return_value=False):
        req = WizRequest(client=client, query=QUERY, vars={"first": 2}, **kw)
    client._enqueue_request.side_effect = lambda r: r._execute_page()
    with (
        patch("wizsec._request.time.sleep"),
        patch.object(Config, "serverless", return_value=False),
    ):
        req._execute_page()
    return req, calls


def _run_async(client, responses, **kw):
    script = iter(responses)
    calls = []

    async def post(**kwargs):
        calls.append(kwargs)
        return next(script)

    session = MagicMock()
    session.post = post
    client._async_session = session

    async def go():
        with patch.object(Config, "validate_queries", return_value=False):
            req = AsyncWizRequest(client=client, query=QUERY, vars={"first": 2}, **kw)

        async def no_sleep(_):
            return None

        with patch("wizsec._request.asyncio.sleep", side_effect=no_sleep):
            await req._execute_page()
        return req

    return asyncio.run(go()), calls


# Factories: response bodies are mutated as pages merge, so each run needs
# fresh objects.
SCENARIOS = {
    "two_pages": lambda: [_page([1, 2], "c1"), _page([3])],
    "retry_5xx_then_ok": lambda: [_resp(503, text="unavailable"), _page([1])],
    "429_then_ok": lambda: [_resp(429, headers={"Retry-After": "1"}), _page([1])],
    "graphql_rate_limit_then_ok": lambda: [
        _resp(200, {"errors": [{"message": "Rate limit exceeded"}]}),
        _page([1]),
    ],
    "4xx_not_retried": lambda: [_resp(400, text="bad query"), _page([1])],
    "5xx_exhausts_retries": lambda: [_resp(502, text="bad gateway") for _ in range(3)],
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_sync_and_async_agree(client, name):
    sync_req, sync_calls = _run_sync(client, SCENARIOS[name](), paginate=True)
    async_req, async_calls = _run_async(client, SCENARIOS[name](), paginate=True)

    assert len(sync_calls) == len(async_calls)
    assert sync_req.data == async_req.data
    assert [e["message"] for e in sync_req.errors] == [
        e["message"] for e in async_req.errors
    ]
    assert type(sync_req.error) is type(async_req.error)


def test_async_4xx_is_not_retried(client):
    req, calls = _run_async(client, SCENARIOS["4xx_not_retried"](), paginate=False)
    assert len(calls) == 1
    assert req.error is not None


def test_page_event_numbering_matches(client):
    sync_pages, async_pages = [], []
    _run_sync(
        client,
        SCENARIOS["two_pages"](),
        paginate=True,
        on_page_event=lambda e: sync_pages.append(e["page_info"]["page"]),
    )
    _run_async(
        client,
        SCENARIOS["two_pages"](),
        paginate=True,
        on_page_event=lambda e: async_pages.append(e["page_info"]["page"]),
    )
    assert sync_pages == async_pages == [1, 2]


def test_async_refreshes_token_between_pages(client):
    _run_async(client, SCENARIOS["two_pages"](), paginate=True)
    # submit() checks the first page; _execute_page checks each later page.
    assert client._check_token.call_count == 1


def test_retry_delay_is_linear_with_jitter(client):
    client._query_retry_time = 2
    with patch.object(Config, "validate_queries", return_value=False):
        req = WizRequest(client=client, query=QUERY, paginate=False)
    for attempt in (1, 2, 3):
        delay = req._retry_delay(attempt)
        assert 2 * attempt <= delay < 2 * attempt * 1.5
