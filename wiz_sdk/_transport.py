##
## 
##   TTTTTTT   RRRR     AAAAA   N     N   SSSSS   PPPPPP    OOOOO   RRRR     TTTTTTT
##      T      R   R   A     A  NN    N   S       P     P  O     O  R   R       T
##      T      RRRR    AAAAAAA  N N   N   SSSSS   PPPPPP   O     O  RRRR        T
##      T      R   R   A     A  N  N  N       S   P        O     O  R   R       T
##      T      R    R  A     A  N     N   SSSSS   P         OOOOO   R    R      T
##
##
## Author: James Husted             Email: james@husted.dev
## Repo: https://github.com/HusteDev/wiz-sdk
##

# _transport.py - Transport-agnostic business logic
import asyncio
import aiohttp
import requests
import threading
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Union, List, Callable
from .config import Config
from .exceptions import WizAPIError, WizTimeoutError


class TransportProtocol(ABC):
    """Abstract base for sync/async HTTP transport"""
    
    @abstractmethod
    def post(self, url: str, json: Dict[str, Any], headers: Dict[str, str]) -> Any:
        """Send POST request - returns response object"""
        pass
    
    @abstractmethod
    def get_status_code(self, response: Any) -> int:
        """Extract status code from response"""
        pass
    
    @abstractmethod
    def get_json(self, response: Any) -> Dict[str, Any]:
        """Extract JSON from response"""
        pass
    
    @abstractmethod
    def sleep(self, seconds: float) -> None:
        """Sleep/delay execution"""
        pass


class SyncTransport(TransportProtocol):
    """Synchronous HTTP transport using requests"""
    
    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        
    def post(self, url: str, json: Dict[str, Any], headers: Dict[str, str]) -> requests.Response:
        return self.session.post(url=url, json=json, headers=headers, timeout=Config.api_timeout())
    
    def get_status_code(self, response: requests.Response) -> int:
        return response.status_code
    
    def get_json(self, response: requests.Response) -> Dict[str, Any]:
        return response.json()
    
    def sleep(self, seconds: float) -> None:
        import time
        time.sleep(seconds)


class AsyncTransport(TransportProtocol):
    """Asynchronous HTTP transport using aiohttp"""
    
    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        self.session = session
        self._owns_session = session is None
        
    async def __aenter__(self):
        if self._owns_session:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=Config.api_timeout())
            )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._owns_session and self.session:
            await self.session.close()
    
    async def post(self, url: str, json: Dict[str, Any], headers: Dict[str, str]) -> aiohttp.ClientResponse:
        if not self.session:
            raise RuntimeError("Async transport not properly initialized - use async with")
        
        response = await self.session.post(url=url, json=json, headers=headers)
        # Store response data for later access
        response._json_data = await response.json()
        return response
    
    def get_status_code(self, response: aiohttp.ClientResponse) -> int:
        return response.status
    
    def get_json(self, response: aiohttp.ClientResponse) -> Dict[str, Any]:
        return response._json_data
    
    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class RequestExecutor:
    """Shared request execution logic - works with any transport"""
    
    def __init__(self, transport: TransportProtocol, client: "WizClient"):
        self.transport = transport
        self.client = client
        
    def execute_request(self, query: str, vars: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a single request (sync or async based on transport)"""
        url = self.client._api_endpoint()
        headers = self.client._get_headers()
        payload = {"query": query, "variables": vars}
        
        retries = 0
        while retries <= self.client._max_retries:
            try:
                response = self.transport.post(url, payload, headers)
                status_code = self.transport.get_status_code(response)
                
                if status_code == 200:
                    return self.transport.get_json(response)
                else:
                    self._handle_error_response(response, retries)
                    
            except Exception as e:
                self._handle_exception(e, retries)
            
            retries += 1
            if retries <= self.client._max_retries:
                backoff_time = self._calculate_backoff(retries)
                self.transport.sleep(backoff_time)
        
        raise WizAPIError("Request failed after all retries")
    
    def _handle_error_response(self, response: Any, retry_count: int) -> None:
        """Handle HTTP error responses"""
        status = self.transport.get_status_code(response)
        if status == 429:  # Rate limited
            # Will retry with backoff
            return
        elif status >= 500:  # Server error
            # Will retry
            return
        else:
            # Client error - don't retry
            raise WizAPIError(f"HTTP {status} error", status_code=status)
    
    def _handle_exception(self, e: Exception, retry_count: int) -> None:
        """Handle request exceptions"""
        if "timeout" in str(e).lower():
            if retry_count >= self.client._max_retries:
                raise WizTimeoutError("Request timed out after retries")
        # Other exceptions will be retried
    
    def _calculate_backoff(self, retry_count: int) -> float:
        """Calculate exponential backoff with jitter"""
        import random
        base_delay = self.client._query_retry_time
        return base_delay * (2 ** (retry_count - 1)) * (1 + random.uniform(0, 0.5))