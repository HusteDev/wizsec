"""Tests for _transport.py — the HTTP transport layer."""

from unittest.mock import patch, MagicMock

import pytest

from wizsec._transport import post, get, stream_get, create_async_client, TransportError


class TestTransportError:
    def test_message(self):
        err = TransportError("connection refused")
        assert str(err) == "connection refused"
        assert err.original_error is None

    def test_original_error(self):
        orig = ConnectionError("boom")
        err = TransportError("wrapped", original_error=orig)
        assert err.original_error is orig


class TestPost:
    @patch("wizsec._transport.httpx.post")
    def test_success(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        resp = post("https://example.com", json={"key": "val"}, headers={"X-Test": "1"})
        assert resp.status_code == 200
        mock_post.assert_called_once_with(
            "https://example.com",
            data=None,
            json={"key": "val"},
            headers={"X-Test": "1"},
            proxy=None,
            verify=True,
            timeout=None,
        )

    @patch("wizsec._transport.httpx.post")
    def test_with_proxy_and_verify(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        post(
            "https://example.com",
            proxy="http://proxy:8080",
            verify="/ca.pem",
            timeout=30,
        )
        _, kwargs = mock_post.call_args
        assert kwargs["proxy"] == "http://proxy:8080"
        assert kwargs["verify"] == "/ca.pem"
        assert kwargs["timeout"] == 30

    @patch("wizsec._transport.httpx.post")
    def test_wraps_httpx_error(self, mock_post):
        import httpx

        mock_post.side_effect = httpx.ConnectError("refused")
        with pytest.raises(TransportError) as exc_info:
            post("https://example.com")
        assert "refused" in str(exc_info.value)
        assert isinstance(exc_info.value.original_error, httpx.HTTPError)


class TestGet:
    @patch("wizsec._transport.httpx.get")
    def test_success(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        resp = get("https://example.com/file")
        assert resp.status_code == 200

    @patch("wizsec._transport.httpx.get")
    def test_wraps_httpx_error(self, mock_get):
        import httpx

        mock_get.side_effect = httpx.TimeoutException("timed out")
        with pytest.raises(TransportError):
            get("https://example.com/file")


class TestStreamGet:
    @patch("wizsec._transport.httpx.stream")
    def test_returns_context_manager(self, mock_stream):
        mock_stream.return_value = MagicMock()
        result = stream_get("https://example.com/stream")
        mock_stream.assert_called_once_with("GET", "https://example.com/stream")
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
