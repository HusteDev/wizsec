"""Shared query-splitting and rate-slot helpers on _RequestBase."""

import asyncio
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from wizsec._request import AsyncWizRequest, WizRequest
from wizsec.config import Config

QUERY = "query Q { issues(first: 10) { nodes { id } totalCount } }"
PROBE = "query Probe { issues { totalCount } }"


@pytest.fixture()
def client(mock_config):
    c = MagicMock()
    c.environment = "gov"
    c._query_retry_time = 0
    c._max_retries = 0
    c._env_state.rate_backoff_remaining.return_value = 0
    return c


def _req(cls, client, **kw):
    with patch.object(Config, "validate_queries", return_value=False):
        return cls(client=client, query=QUERY, vars={"first": 10}, **kw)


def _split_config(stack, mode="static", filter_path="filterBy.project"):
    stack.enter_context(patch.object(Config, "serverless", return_value=False))
    stack.enter_context(
        patch.object(Config, "query_splitting_enabled", return_value=True)
    )
    stack.enter_context(
        patch.object(Config, "query_splitting_detection_mode", return_value=mode)
    )
    stack.enter_context(
        patch.object(Config, "query_splitting_threshold", return_value=100)
    )
    stack.enter_context(
        patch.object(Config, "query_splitting_filter_path", return_value=filter_path)
    )
    stack.enter_context(
        patch.object(Config, "query_splitting_max_concurrent", return_value=4)
    )
    stack.enter_context(
        patch.object(Config, "query_splitting_split_by", return_value="project")
    )
    stack.enter_context(
        patch("wizsec._request.has_totalcount_field", return_value=True)
    )
    stack.enter_context(
        patch("wizsec._request.build_totalcount_probe_query", return_value=PROBE)
    )


class TestSplitPlan:
    def test_schema_mode_rejects_unsupported_source(self, client):
        req = _req(WizRequest, client)
        with ExitStack() as stack:
            _split_config(stack, mode="schema")
            stack.enter_context(
                patch("wizsec._request._schema_supports_totalcount", return_value=False)
            )
            assert req._split_plan() is None

    def test_schema_mode_accepts_supported_source(self, client):
        req = _req(WizRequest, client)
        with ExitStack() as stack:
            _split_config(stack, mode="schema")
            stack.enter_context(
                patch("wizsec._request._schema_supports_totalcount", return_value=True)
            )
            assert req._split_plan() == ("issues", PROBE)

    def test_no_source_is_not_a_candidate(self, client):
        req = _req(WizRequest, client)
        req._current_query_info = {"request_type": "query", "source": ""}
        with ExitStack() as stack:
            _split_config(stack)
            assert req._split_plan() is None


class TestSplitFilterPath:
    def _probe(self, total):
        probe = MagicMock()
        probe.success.return_value = True
        probe.data = {"issues": {"totalCount": total}}
        return probe

    def test_over_threshold_returns_filter_path(self, client):
        req = _req(WizRequest, client)
        with ExitStack() as stack:
            _split_config(stack)
            assert (
                req._split_filter_path(self._probe(500), "issues") == "filterBy.project"
            )

    def test_missing_filter_path_returns_none(self, client):
        req = _req(WizRequest, client)
        with ExitStack() as stack:
            _split_config(stack, filter_path="")
            assert req._split_filter_path(self._probe(500), "issues") is None


class TestSplitEntities:
    def test_empty_entities_rejected(self, client):
        req = _req(WizRequest, client)
        with ExitStack() as stack:
            _split_config(stack)
            assert req._split_entities_ok([]) is False
            assert req._split_entities_ok([{"id": "p1"}]) is True

    def test_add_split_requests_skips_entities_without_id(self, client):
        req = _req(WizRequest, client)
        batch = MagicMock()
        inner = {0: MagicMock(), 1: MagicMock()}
        batch.add_request.side_effect = [0, 1]
        batch._requests = inner
        with patch(
            "wizsec._request.inject_subscription_filter",
            side_effect=lambda v, path, ids: {**v, "ids": ids},
        ):
            req._add_split_requests(
                batch, [{"id": "a"}, {"name": "no-id"}, {"id": "b"}], "filterBy.project"
            )
        assert batch.add_request.call_count == 2
        assert [c.kwargs["vars"]["ids"] for c in batch.add_request.call_args_list] == [
            ["a"],
            ["b"],
        ]
        assert inner[0]._is_sub_request is True
        assert inner[1]._is_sub_request is True


def _fake_batch_response(nodes_per_entity):
    responses = []
    for i, nodes in enumerate(nodes_per_entity):
        r = MagicMock()
        r.data = {"issues": {"nodes": nodes, "totalCount": len(nodes)}}
        r.errors = []
        r.success.return_value = True
        responses.append((i, r))
    return responses


class TestSyncSplitEndToEnd:
    def test_split_merges_entity_results(self, client):
        req = _req(WizRequest, client, paginate=True)

        def run_probe(probe):
            probe.data = {"issues": {"totalCount": 500}}
            probe.errors = []
            probe._done_event.set()

        client._enqueue_request.side_effect = run_probe
        merged = {"issues": {"nodes": [{"id": 1}, {"id": 2}]}}
        with ExitStack() as stack:
            _split_config(stack)
            stack.enter_context(
                patch(
                    "wizsec._request._get_cached_or_fetch_entities",
                    return_value=[{"id": "p1"}, {"id": "p2"}],
                )
            )
            run = stack.enter_context(
                patch.object(WizRequest, "_run_split_async", autospec=True)
            )

            async def fake_run(self, entities, filter_path):
                assert filter_path == "filterBy.project"
                return ["r1", "r2"]

            run.side_effect = fake_run
            stack.enter_context(
                patch("wizsec._request._merge_split_results", return_value=merged)
            )
            assert req._maybe_split() is True
        assert req.data == merged
        assert req._done_event.is_set()

    def test_no_entities_falls_back(self, client):
        req = _req(WizRequest, client, paginate=True)

        def run_probe(probe):
            probe.data = {"issues": {"totalCount": 500}}
            probe.errors = []
            probe._done_event.set()

        client._enqueue_request.side_effect = run_probe
        with ExitStack() as stack:
            _split_config(stack)
            stack.enter_context(
                patch("wizsec._request._get_cached_or_fetch_entities", return_value=[])
            )
            assert req._maybe_split() is False

    def test_run_split_async_builds_batch(self, client):
        req = _req(WizRequest, client, paginate=True)
        session_cm = MagicMock()

        async def aenter(*_):
            return client

        async def aexit(*_):
            return None

        session_cm.__aenter__ = aenter
        session_cm.__aexit__ = aexit
        client.async_session.return_value = session_cm

        batch = MagicMock()

        async def submit():
            return _fake_batch_response([[{"id": 1}], [{"id": 2}]])

        batch.submit = submit
        with ExitStack() as stack:
            _split_config(stack)
            stack.enter_context(
                patch("wizsec._request.AsyncWizBatchRequest", return_value=batch)
            )
            add = stack.enter_context(patch.object(WizRequest, "_add_split_requests"))
            result = asyncio.run(
                req._run_split_async([{"id": "p1"}, {"id": "p2"}], "filterBy.project")
            )
        add.assert_called_once()
        assert len(result) == 2


class TestAsyncSplitEndToEnd:
    def test_split_merges_entity_results(self, client):
        req = _req(AsyncWizRequest, client, paginate=True)
        merged = {"issues": {"nodes": [{"id": 1}]}}

        async def probe_submit(probe):
            probe.data = {"issues": {"totalCount": 500}}
            probe.errors = []
            return probe

        batch = MagicMock()

        async def submit():
            return _fake_batch_response([[{"id": 1}]])

        batch.submit = submit

        async def go():
            with ExitStack() as stack:
                _split_config(stack)
                stack.enter_context(
                    patch.object(
                        AsyncWizRequest,
                        "submit",
                        autospec=True,
                        side_effect=probe_submit,
                    )
                )
                stack.enter_context(
                    patch(
                        "wizsec._request._get_cached_or_fetch_entities",
                        return_value=[{"id": "p1"}],
                    )
                )
                stack.enter_context(
                    patch("wizsec._request.AsyncWizBatchRequest", return_value=batch)
                )
                add = stack.enter_context(
                    patch.object(AsyncWizRequest, "_add_split_requests")
                )
                stack.enter_context(
                    patch("wizsec._request._merge_split_results", return_value=merged)
                )
                ok = await req._maybe_split_async()
                add.assert_called_once()
                return ok

        assert asyncio.run(go()) is True
        assert req.data == merged

    def test_probe_below_threshold_skips(self, client):
        req = _req(AsyncWizRequest, client, paginate=True)

        async def probe_submit(probe):
            probe.data = {"issues": {"totalCount": 5}}
            probe.errors = []
            return probe

        async def go():
            with ExitStack() as stack:
                _split_config(stack)
                stack.enter_context(
                    patch.object(
                        AsyncWizRequest,
                        "submit",
                        autospec=True,
                        side_effect=probe_submit,
                    )
                )
                return await req._maybe_split_async()

        assert asyncio.run(go()) is False

    def test_no_entities_falls_back(self, client):
        req = _req(AsyncWizRequest, client, paginate=True)

        async def probe_submit(probe):
            probe.data = {"issues": {"totalCount": 500}}
            probe.errors = []
            return probe

        async def go():
            with ExitStack() as stack:
                _split_config(stack)
                stack.enter_context(
                    patch.object(
                        AsyncWizRequest,
                        "submit",
                        autospec=True,
                        side_effect=probe_submit,
                    )
                )
                stack.enter_context(
                    patch(
                        "wizsec._request._get_cached_or_fetch_entities", return_value=[]
                    )
                )
                return await req._maybe_split_async()

        assert asyncio.run(go()) is False


class TestAcquireSlot:
    """Both paths wait out a shared server backoff, including one that starts
    while waiting on the local limiter."""

    def test_sync_waits_for_backoff(self, client):
        req = _req(WizRequest, client)
        client._env_state.rate_backoff_remaining.side_effect = [2, 0, 1, 0, 0]
        limiter = MagicMock()
        limiter.try_acquire.return_value = True
        with patch("wizsec._request.time.sleep") as sleep:
            req._acquire_slot(limiter)
        sleep.assert_any_call(2)
        assert limiter.try_acquire.call_count == 2

    def test_async_waits_for_backoff(self, client):
        req = _req(AsyncWizRequest, client)
        client._env_state.rate_backoff_remaining.side_effect = [2, 0, 1, 0, 0]
        limiter = MagicMock()
        limiter.try_acquire.return_value = True
        sleeps = []

        async def fake_sleep(t):
            sleeps.append(t)

        with patch("wizsec._request.asyncio.sleep", side_effect=fake_sleep):
            asyncio.run(req._acquire_slot(limiter))
        assert 2 in sleeps
        assert limiter.try_acquire.call_count == 2


def test_graphql_rate_limited_tolerates_bad_json(client):
    req = _req(WizRequest, client)
    resp = MagicMock()
    resp.json.side_effect = ValueError("not json")
    assert req._graphql_rate_limited(resp) is False
