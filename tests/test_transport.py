"""Tests for _transport.py — the HTTP transport layer."""

from unittest.mock import patch, MagicMock

import pytest

import httpx

from wizsec._transport import (
    _sync_client,
    close_clients,
    create_async_client,
    default_verify,
    get,
    post,
    stream_get,
    TransportError,
)


class TestTransportError:
    def test_message(self):
        err = TransportError("connection refused")
        assert str(err) == "connection refused"
        assert err.original_error is None

    def test_original_error(self):
        orig = ConnectionError("boom")
        err = TransportError("wrapped", original_error=orig)
        assert err.original_error is orig


@pytest.fixture(autouse=True)
def _fresh_pool():
    close_clients()
    yield
    close_clients()


class TestPooling:
    def test_same_settings_reuse_client(self):
        a = _sync_client(None, True)
        b = _sync_client(None, True)
        assert a is b

    def test_different_settings_get_separate_clients(self):
        assert _sync_client(None, True) is not _sync_client("http://proxy:8080", True)

    def test_closed_client_is_replaced(self):
        a = _sync_client(None, True)
        a.close()
        assert _sync_client(None, True) is not a

    def test_close_clients_empties_pool(self):
        a = _sync_client(None, True)
        close_clients()
        assert a.is_closed


class TestDefaultVerify:
    def test_true_without_env(self, monkeypatch):
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        assert default_verify() is True

    def test_requests_ca_bundle_wins(self, monkeypatch):
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/a.pem")
        monkeypatch.setenv("SSL_CERT_FILE", "/b.pem")
        assert default_verify() == "/a.pem"

    def test_ssl_cert_file(self, monkeypatch):
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        monkeypatch.setenv("SSL_CERT_FILE", "/b.pem")
        assert default_verify() == "/b.pem"


class TestPost:
    @patch("wizsec._transport._sync_client")
    def test_success(self, mock_factory):
        mock_factory.return_value.post.return_value = MagicMock(status_code=200)
        resp = post("https://example.com", json={"key": "val"}, headers={"X-Test": "1"})
        assert resp.status_code == 200
        mock_factory.assert_called_once_with(None, True)
        mock_factory.return_value.post.assert_called_once_with(
            "https://example.com",
            data=None,
            json={"key": "val"},
            headers={"X-Test": "1"},
            timeout=httpx.USE_CLIENT_DEFAULT,
        )

    @patch("wizsec._transport._sync_client")
    def test_with_proxy_and_verify(self, mock_factory):
        mock_factory.return_value.post.return_value = MagicMock(status_code=200)
        post(
            "https://example.com",
            proxy="http://proxy:8080",
            verify="/ca.pem",
            timeout=30,
        )
        mock_factory.assert_called_once_with("http://proxy:8080", "/ca.pem")
        _, kwargs = mock_factory.return_value.post.call_args
        assert kwargs["timeout"] == 30

    @patch("wizsec._transport._sync_client")
    def test_wraps_httpx_error(self, mock_factory):
        mock_factory.return_value.post.side_effect = httpx.ConnectError("refused")
        with pytest.raises(TransportError) as exc_info:
            post("https://example.com")
        assert "refused" in str(exc_info.value)
        assert isinstance(exc_info.value.original_error, httpx.HTTPError)


class TestGet:
    @patch("wizsec._transport._sync_client")
    def test_success(self, mock_factory):
        mock_factory.return_value.get.return_value = MagicMock(status_code=200)
        resp = get("https://example.com/file", proxy="http://p:1", timeout=5)
        assert resp.status_code == 200
        mock_factory.assert_called_once_with("http://p:1", True)
        mock_factory.return_value.get.assert_called_once_with(
            "https://example.com/file", timeout=5
        )

    @patch("wizsec._transport._sync_client")
    def test_wraps_httpx_error(self, mock_factory):
        mock_factory.return_value.get.side_effect = httpx.TimeoutException("timed out")
        with pytest.raises(TransportError):
            get("https://example.com/file")


class TestStreamGet:
    @patch("wizsec._transport._sync_client")
    def test_returns_context_manager(self, mock_factory):
        mock_factory.return_value.stream.return_value = MagicMock()
        result = stream_get("https://example.com/stream", verify="/ca.pem")
        mock_factory.assert_called_once_with(None, "/ca.pem")
        mock_factory.return_value.stream.assert_called_once_with(
            "GET", "https://example.com/stream", timeout=httpx.USE_CLIENT_DEFAULT
        )
        assert result is not None


class TestCreateAsyncClient:
    def test_returns_async_client(self):
        import asyncio
        import httpx

        async def _test():
            client = create_async_client(timeout=60, max_connections=50, verify=False)
            assert isinstance(client, httpx.AsyncClient)
            await client.aclose()

        asyncio.run(_test())

    def test_defaults(self):
        import asyncio
        import httpx

        async def _test():
            client = create_async_client()
            assert isinstance(client, httpx.AsyncClient)
            await client.aclose()

        asyncio.run(_test())

    def test_proxy_is_applied(self):
        import asyncio
        from unittest.mock import patch as _patch

        async def _test():
            with _patch("wizsec._transport.httpx.AsyncClient") as ac:
                create_async_client(proxy="http://proxy:8080")
                assert ac.call_args.kwargs["proxy"] == "http://proxy:8080"

        asyncio.run(_test())
