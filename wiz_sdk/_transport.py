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

import httpx
from typing import Optional, Dict, Any


# ── Exception ────────────────────────────────────────────────────────

class TransportError(Exception):
    """Wraps the underlying HTTP library's errors."""

    def __init__(self, message: str, original_error: Optional[Exception] = None):
        super().__init__(message)
        self.original_error = original_error


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
):
    """Synchronous POST request."""
    try:
        return httpx.post(
            url,
            data=data,
            json=json,
            headers=headers,
            proxy=proxy,
            verify=verify,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise TransportError(str(exc), original_error=exc) from exc


def get(url: str, **kwargs):
    """Synchronous GET request."""
    try:
        return httpx.get(url, **kwargs)
    except httpx.HTTPError as exc:
        raise TransportError(str(exc), original_error=exc) from exc


def stream_get(url: str):
    """Context manager for a streaming GET request."""
    return httpx.stream("GET", url)


# ── Async client factory ─────────────────────────────────────────────

def create_async_client(
    *,
    timeout: float = 180,
    max_connections: int = 100,
    verify: Any = True,
):
    """Create a configured async HTTP client."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        limits=httpx.Limits(max_connections=max_connections),
        verify=verify,
    )
