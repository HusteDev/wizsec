"""Tests for the 1.1.0 release features: report deadlock fix, typed errors,
transient-error clearing, streaming pagination, and wizsec doctor."""

import threading
import types
from unittest.mock import MagicMock, patch

import pytest
import yaml

from wizsec.client import WizClient
from wizsec.config import Config
from wizsec.exceptions import WizAPIError, WizRateLimitError
from wizsec._registry import EnvironmentState
from wizsec._request import AsyncWizRequest, WizRequest, WizResponse

QUERY = (
    "query Issues($first: Int, $after: String) { issues(first: $first, after: $after)"
    " { nodes { id } pageInfo { hasNextPage endCursor } } }"
)

CREATE_REPORT = (
    "mutation CreateReport($input: CreateReportInput!) "
    "{ createReport(input: $input) { report { id } } }"
)


def _mock_client(env_name):
    client = MagicMock()
    client._env_state = EnvironmentState(env_name)
    client._max_retries = 1
    client._query_retry_time = 0
    client._is_service_account = True
    client._limiter_key.return_value = "query_service"
    client._api_endpoint.return_value = "https://api.test/graphql"
    client._get_headers.return_value = {}
    limiter = MagicMock()
    limiter.try_acquire.return_value = True
    client._get_limiter.return_value = limiter
    return client


def _resp(status_code, payload=None, text="err", headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    resp.text = text
    resp.headers = headers or {}
    return resp


def _make_req(client, query=QUERY, **kw):
    with patch.object(Config, "validate_queries", return_value=False):
        return WizRequest(client=client, query=query, paginate=False, **kw)


# ---------------------------------------------------------------------------
# Report workflow deadlock fix
# ---------------------------------------------------------------------------


class TestReportWorkflowOnQueueWorker:
    def test_report_completes_through_real_queue_worker(self):
        """Report polling must not deadlock the single queue worker thread."""
        client = _mock_client("report-deadlock-env")
        # Bind the real queue/worker machinery onto the mock client.
        client._enqueue_request = types.MethodType(WizClient._enqueue_request, client)
        client._start_worker = types.MethodType(WizClient._start_worker, client)
        client._process_queue = types.MethodType(WizClient._process_queue, client)
        client._check_token.return_value = None

        client._post = MagicMock(
            side_effect=[
                _resp(
                    200,
                    {"data": {"createReport": {"report": {"id": "rpt-1"}}}},
                ),
                _resp(
                    200,
                    {
                        "data": {
                            "report": {
                                "lastRun": {
                                    "status": "COMPLETED",
                                    "progress": 100,
                                    "url": "https://dl.example/report",
                                }
                            }
                        }
                    },
                ),
            ]
        )

        req = _make_req(
            client,
            query=CREATE_REPORT,
            report_request={"name": "test-report", "stream": False},
        )

        done = {}

        def run():
            with patch.object(
                WizRequest, "_download_report", return_value=b"report-bytes"
            ):
                req.submit()
            done["finished"] = True

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(15)
        client._env_state.stop_event.set()

        assert done.get("finished"), "report workflow deadlocked on the queue worker"
        assert req.success()
        assert req.data["report_data"] == b"report-bytes"


# ---------------------------------------------------------------------------
# Typed errors + transient-error clearing
# ---------------------------------------------------------------------------


class TestTypedErrors:
    def test_final_failure_records_wiz_api_error(self):
        client = _mock_client("typed-final-env")
        client._post = MagicMock(return_value=_resp(500, text="boom"))
        req = _make_req(client)
        with patch("wizsec._request.time.sleep"):
            req._execute_page()

        assert not req.success()
        assert isinstance(req.error, WizAPIError)
        assert req.error.status_code == 500

        response = WizResponse(req)
        assert response.error is req.error
        with pytest.raises(WizAPIError):
            response.raise_on_error()

    def test_rate_limit_give_up_records_wiz_rate_limit_error(self):
        client = _mock_client("typed-429-env")
        client._post = MagicMock(
            return_value=_resp(429, text="slow down", headers={"Retry-After": "0"})
        )
        req = _make_req(client)
        with patch("wizsec._request.time.sleep"):
            req._execute_page()

        assert not req.success()
        assert isinstance(req.error, WizRateLimitError)
        assert req.error.retry_after == 0

    def test_raise_on_error_returns_self_on_success(self):
        client = _mock_client("typed-ok-env")
        client._post = MagicMock(
            return_value=_resp(200, {"data": {"issues": {"nodes": []}}})
        )
        req = _make_req(client)
        req._execute_page()
        response = WizResponse(req)
        assert response.raise_on_error() is response
        assert response.error is None


class TestTransientErrorClearing:
    def test_sync_retry_success_clears_transient_errors(self):
        client = _mock_client("poison-sync-env")
        client._post = MagicMock(
            side_effect=[
                _resp(500, text="flaky"),
                _resp(200, {"data": {"issues": {"nodes": [{"id": "1"}]}}}),
            ]
        )
        req = _make_req(client)
        with patch("wizsec._request.time.sleep"):
            req._execute_page()

        assert req.success()
        assert req.errors == []
        assert req.error is None

    @pytest.mark.asyncio
    async def test_async_retry_success_clears_transient_errors(self):
        client = _mock_client("poison-async-env")
        responses = [
            _resp(500, text="flaky"),
            _resp(200, {"data": {"issues": {"nodes": [{"id": "1"}]}}}),
        ]
        calls = [0]

        async def fake_post(**kwargs):
            resp = responses[calls[0]]
            calls[0] += 1
            return resp

        session = MagicMock()
        session.post = fake_post
        client._async_session = session

        with patch.object(Config, "validate_queries", return_value=False):
            req = AsyncWizRequest(client=client, query=QUERY, paginate=False)
        await req._execute_page()

        assert req.success()
        assert req.errors == []


# ---------------------------------------------------------------------------
# Streaming pagination
# ---------------------------------------------------------------------------


def _stub_request_factory(pages, created):
    class StubRequest:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.query = kwargs.get("query")
            self.vars = kwargs.get("vars") or {}
            self.errors = []
            self.error = None
            self.data = None
            self._current_query_info = {"source": "issues"}

        def submit(self):
            self.data = pages[len(created) - 1]
            return self

        def success(self):
            return True

        def _page_info(self, data):
            for value in (data or {}).values():
                if isinstance(value, dict) and "pageInfo" in value:
                    info = value["pageInfo"]
                    return {
                        "hasNextPage": info.get("hasNextPage", False),
                        "endCursor": info.get("endCursor"),
                    }
            return None

    return StubRequest


PAGES = [
    {
        "issues": {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "pageInfo": {"hasNextPage": True, "endCursor": "cur-1"},
        }
    },
    {
        "issues": {
            "nodes": [{"id": "c"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    },
]


class TestIterateNodes:
    def test_yields_all_nodes_and_advances_cursor(self):
        client = MagicMock()
        created = []
        with patch("wizsec._request.WizRequest", _stub_request_factory(PAGES, created)):
            nodes = list(
                WizClient.iterate_nodes(client, query=QUERY, vars={"first": 2})
            )

        assert [n["id"] for n in nodes] == ["a", "b", "c"]
        assert len(created) == 2
        assert created[0]["vars"].get("after") is None
        assert created[1]["vars"]["after"] == "cur-1"
        # each page request must skip split probing
        assert all(kw["paginate"] is False for kw in created)

    def test_early_break_stops_fetching(self):
        client = MagicMock()
        created = []
        with patch("wizsec._request.WizRequest", _stub_request_factory(PAGES, created)):
            iterator = WizClient.iterate_nodes(client, query=QUERY)
            first = next(iterator)
            iterator.close()

        assert first["id"] == "a"
        assert len(created) == 1  # second page never requested

    @pytest.mark.asyncio
    async def test_async_iterator_yields_all_nodes(self):
        client = MagicMock()
        created = []

        sync_stub = _stub_request_factory(PAGES, created)

        class AsyncStub(sync_stub):  # type: ignore[valid-type, misc]
            async def submit(self):
                self.data = PAGES[len(created) - 1]
                return self

        with patch("wizsec._request.AsyncWizRequest", AsyncStub):
            nodes = [
                n async for n in WizClient.iterate_nodes_async(client, query=QUERY)
            ]

        assert [n["id"] for n in nodes] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# wizsec doctor
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_doctor_healthy_setup(self, tmp_path, capsys):
        from wizsec._cli import main

        config = tmp_path / "wiz.config"
        config.write_text(
            yaml.safe_dump({"app": {"config_schema": 2}, "api": {"timeout": 120}})
        )
        creds = tmp_path / "wiz.credentials"
        creds.write_text("[default]\nclient_id = abc\nclient_secret = xyz\n")

        code = main(
            [
                "doctor",
                "--config-file",
                str(config),
                "--creds-file",
                str(creds),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "config file parsed" in out
        assert "1 profile(s)" in out
        assert "rate budgets" in out
        assert "query_service=8/s" in out
        assert "no problems" in out

    def test_doctor_missing_config_fails(self, tmp_path, capsys):
        from wizsec._cli import main

        code = main(
            [
                "doctor",
                "--config-file",
                str(tmp_path / "missing.config"),
                "--creds-file",
                str(tmp_path / "missing.credentials"),
            ]
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "config file missing" in out
        assert "problem(s)" in out

    def test_doctor_stale_schema_names_the_remedy(self, tmp_path, capsys):
        """Nothing refetches on age, so the warning has to say how to fix it."""
        import os
        import time

        from wizsec._cli import main
        from wizsec._schema import SCHEMA_STALE_DAYS

        config = tmp_path / "wiz.config"
        config.write_text(yaml.safe_dump({"app": {"config_schema": 2}}))
        creds = tmp_path / "wiz.credentials"
        creds.write_text("[default]\nclient_id = abc\nclient_secret = xyz\n")

        cache = tmp_path / "schema_gov.json"
        cache.write_text("{}")
        old = time.time() - (SCHEMA_STALE_DAYS + 17) * 86400
        os.utime(cache, (old, old))

        code = main(
            [
                "doctor",
                "--config-file",
                str(config),
                "--creds-file",
                str(creds),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0  # stale is a WARN, not a failure
        assert "schema cache schema_gov.json: 47 day(s) old" in out
        assert "run 'wizsec schema refresh'" in out

    def test_doctor_fresh_schema_has_no_remedy(self, tmp_path, capsys):
        from wizsec._cli import main

        config = tmp_path / "wiz.config"
        config.write_text(yaml.safe_dump({"app": {"config_schema": 2}}))
        creds = tmp_path / "wiz.credentials"
        creds.write_text("[default]\nclient_id = abc\nclient_secret = xyz\n")
        (tmp_path / "schema_gov.json").write_text("{}")

        assert (
            main(
                [
                    "doctor",
                    "--config-file",
                    str(config),
                    "--creds-file",
                    str(creds),
                ]
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "schema cache schema_gov.json: 0 day(s) old" in out
        assert "schema refresh" not in out

    def test_doctor_respects_headroom_override(self, tmp_path, capsys):
        from wizsec._cli import main

        config = tmp_path / "wiz.config"
        config.write_text(
            yaml.safe_dump(
                {
                    "app": {"config_schema": 2},
                    "rate_limit": {
                        "headroom": 0.5,
                        "overrides": {"query_service": 4},
                    },
                }
            )
        )
        code = main(
            [
                "doctor",
                "--config-file",
                str(config),
                "--creds-file",
                str(tmp_path / "c"),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "query_service=4/s" in out
        assert "query_user=50/s" in out
