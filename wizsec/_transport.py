##
##
##   TTTTTTT  RRRRR     AAA    N     N   SSSSS   PPPPPP    OOOOO   RRRRR   TTTTTTT
##      T     R   R    A   A   NN    N  S        P    P   O     O  R   R      T
##      T     RRRRR   AAAAAAA  N N   N  SSSSS    PPPPPP   O     O  RRRRR      T
##      T     R  R    A     A  N  N  N      S    P        O     O  R  R       T
##      T     R   R   A     A  N     N  SSSSS    P         OOOOO   R   R      T
##
##
##

# _transport.py

"""
Transport layer — the sole owner of httpx in the SDK.

All HTTP I/O flows through this module. To swap httpx for another
library, only this file needs to change.
"""

import atexit
import contextlib
import os
import threading
import httpx
from typing import Optional, Dict, Any, Tuple

# ── Exception ────────────────────────────────────────────────────────


class TransportError(Exception):
    """Wraps the underlying HTTP library's errors."""

    def __init__(
        self, message: str, original_error: Optional[Exception] = None
    ) -> None:
        super().__init__(message)
        self.original_error = original_error


# ── TLS verification ─────────────────────────────────────────────────


def default_verify() -> Any:
    """Return the CA bundle path to verify against, or True for the default.

    ``REQUESTS_CA_BUNDLE`` is honoured for backwards compatibility with the
    requests-era configuration; ``SSL_CERT_FILE`` is the variable httpx and
    OpenSSL use natively.
    """
    return (
        os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or True
    )


# ── Pooled sync clients ──────────────────────────────────────────────
# One httpx.Client per (proxy, verify) pair, so repeated calls (pagination,
# retries, auth) reuse TCP/TLS connections instead of reconnecting each time.

_clients: Dict[Tuple[Optional[str], str], httpx.Client] = {}
_clients_lock = threading.Lock()


def _sync_client(proxy: Optional[str], verify: Any) -> httpx.Client:
    key = (proxy, str(verify))
    with _clients_lock:
        client = _clients.get(key)
        if client is None or client.is_closed:
            client = httpx.Client(proxy=proxy, verify=verify)
            _clients[key] = client
        return client


def close_clients() -> None:
    """Close every pooled sync client (called automatically at exit)."""
    with _clients_lock:
        for client in _clients.values():
            client.close()
        _clients.clear()


atexit.register(close_clients)


def _timeout_arg(timeout: Optional[float]) -> Any:
    return httpx.USE_CLIENT_DEFAULT if timeout is None else timeout


# ── Sync helpers ─────────────────────────────────────────────────────


def post(
    url: str,
    *,
    data: Optional[Dict[str, Any]] = None,
    json: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    proxy: Optional[str] = None,
    verify: Any = True,
    timeout: Optional[float] = None,
) -> httpx.Response:
    """Synchronous POST request over a pooled connection."""
    try:
        return _sync_client(proxy, verify).post(
            url,
            data=data,
            json=json,
            headers=headers,
            timeout=_timeout_arg(timeout),
        )
    except httpx.HTTPError as exc:
        raise TransportError(str(exc), original_error=exc) from exc


def get(
    url: str,
    *,
    proxy: Optional[str] = None,
    verify: Any = True,
    timeout: Optional[float] = None,
) -> httpx.Response:
    """Synchronous GET request over a pooled connection."""
    try:
        return _sync_client(proxy, verify).get(url, timeout=_timeout_arg(timeout))
    except httpx.HTTPError as exc:
        raise TransportError(str(exc), original_error=exc) from exc


def stream_get(
    url: str,
    *,
    proxy: Optional[str] = None,
    verify: Any = True,
    timeout: Optional[float] = None,
) -> contextlib.AbstractContextManager[httpx.Response]:
    """Context manager for a streaming GET request."""
    return _sync_client(proxy, verify).stream(
        "GET", url, timeout=_timeout_arg(timeout)
    )


# ── Async client factory ─────────────────────────────────────────────


def create_async_client(
    *,
    timeout: float = 180,
    max_connections: int = 100,
    verify: Any = True,
    proxy: Optional[str] = None,
) -> httpx.AsyncClient:
    """Create a configured async HTTP client."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        limits=httpx.Limits(max_connections=max_connections),
        verify=verify,
        proxy=proxy,
    )
