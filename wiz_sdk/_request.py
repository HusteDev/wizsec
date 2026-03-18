##
## 
##   RRRRR   EEEEE    QQQQQ   U     U   EEEEE   SSSSS   TTTTTTT
##   R   R   E       Q     Q  U     U   E       S          T   
##   RRRRR   EEEEE   Q  Q  Q  U     U   EEEEE   SSSSS      T   
##   R  R    E       Q   QQQ  U     U   E           S      T   
##   R   R   EEEEE    QQQQQ    UUUUU    EEEEE   SSSSS      T   
##
##
## Author: James Husted             Email: james@husted.dev
## Repo: https://github.com/HusteDev/wiz-sdk
##

# _request.py
import time
import traceback
import random
import codecs
import csv
import requests
import json
import threading
from typing import Optional, Dict, Any, List, Union, Callable, Iterator
from graphql import parse, GraphQLError
import asyncio
import aiohttp
from .utils import parse_query_metadata
from .config import Config
from .client import WizClient
from .exceptions import WizQueryError, WizAPIError, WizReportError, WizTimeoutError

class WizRequest:
    def __init__(
        self, 
        client: "WizClient", 
        queryCollection: Optional[Union[str, Any]] = None,
        query: Optional[str] = None, 
        vars: Optional[Dict[str, Any]] = None, 
        paginate: bool = True,
        report_request: Optional[Dict[str, Any]] = None,
        on_page_event: Optional[Callable] = None
    ) -> None:
        self._logger = Config.get_logger()
        self._client = client
        self.vars = vars or {}
        self._response = None
        self.errors = []
        self.data = None
        self._status_code = None
        self._paginate = paginate and not Config.serverless()
        self._report_request = report_request or {}
        self._page_event = on_page_event
        self._done_event = threading.Event()
        self._page = 0

        self._queryCollection = None
        if queryCollection:
            self.queryCollection = queryCollection

        # Set query after checking for queryCollection
        if query is not None:
            self.query = query
            
        self._limiter_key = self._client._limiter_key(self)

    @property
    def queryCollection(self) -> Optional[Any]:
        if hasattr(self, "_queryCollection") and self._queryCollection:
            return self._queryCollection
        else:
            self._logger.debug("No queryCollection set")
            return None

    @queryCollection.setter
    def queryCollection(self, module: Union[str, Any]) -> None:
        import importlib, sys, types
        if isinstance(module, str):
            if module not in sys.modules:
                module = importlib.import_module(module)
        elif isinstance(module, types.ModuleType):
            module_name = module.__name__
            if module_name not in sys.modules:
                sys.modules[module_name] = module
                self._logger.debug("Manually added [%s] to sys.modules", module_name)
        else:
            self._logger.warning("Unexpected type for queryCollection: %s", type(module))
            raise WizQueryError("QueryCollection must be a module or a module name string")

        self._queryCollection = module

    @property
    def query(self) -> str:
        return self._query

    @query.setter
    def query(self, QUERY: str) -> None:
        if not isinstance(QUERY, str):
            self._logger.warning("Query must be a string, got: %s", type(QUERY))
            raise WizQueryError(f"Query must be a string, got: {type(QUERY)}")
        
        resolved = None
        try:
            parse(QUERY)
            # self._query = QUERY
            resolved = QUERY
        except GraphQLError:
            # Check for a queryCollection and resolve
            if self.queryCollection:
                try:
                    # resolved_query = getattr(self.queryCollection, QUERY)
                    resolved = getattr(self.queryCollection, QUERY)
                    self._logger.debug(f"Resolved query '{QUERY}' from queryCollection")
                    # self._query = resolved_query
                except AttributeError:
                    self._logger.debug(f"Query '{QUERY}' not found in queryCollection. Using raw query.")
                    # self._query = QUERY
                    resolved = QUERY
            else:
                # self._query = QUERY
                resolved = QUERY

        self._query = resolved

        # Parse query metadata
        self._current_query_info = parse_query_metadata(self._query)

        # Check for report workflow
        if (
            self._current_query_info["request_type"] == "mutation" and
            self._current_query_info["source"].lower() in ["createreport", "rerunreport"]
        ):
            self._logger.debug("__current_query = %s", self._current_query_info)
            self._report = True

    def submit(self) -> "WizRequest":
        """Enqueue this request and wait for completion."""
        if Config.serverless():
            # Skip threading in serverless mode
            self._client._enqueue_request(self)
            return self
    
        self._done_event.clear()
        self._client._enqueue_request(self)
        self._done_event.wait()
        return self

    def _execute_page(self) -> None:
        """Execute a single page of this request."""
        self._client._check_token()
        retries = 0
        url = self._client._api_endpoint()
        headers = self._client._get_headers()
        payload = {"query": self.query, "variables": self.vars}
        limiter = self._client._get_limiter(self._limiter_key)

        while retries <= self._client._max_retries:
            try:
                limiter.try_acquire(self._limiter_key)
                response = self._client._post(url=url, headers=headers, json=payload)
                self._status_code = response.status_code
                
                if response.status_code == 200:
                    if self._process_successful_response(response, url, headers):
                        return
                else:
                    self._handle_failed_response(response)
                    self._logger.debug(f"Payload: {payload}")
                    
            except requests.RequestException as e:
                self._handle_network_error(e)
            except Exception as e:
                self._handle_unexpected_error(e)

            retries += 1
            self._wait_before_retry(retries)

        self._handle_final_failure()
    
    def _process_successful_response(self, response, url: str, headers: Dict[str, str]) -> bool:
        """Process a successful HTTP response. Returns True if processing is complete."""
        self._response = response.json()
        self.errors.extend(self._response.get("errors", []))
        page_data = self._response.get("data", {})
        self._merge_page(page_data)

        if self.errors:
            self._set_done_event()
            return True

        if self._generate_report():
            self._logger.debug("Trigger report workflow")
            self._report_workflow(response)
            return True
            
        if Config.serverless():
            return self._handle_serverless_pagination(url, headers)
        else:
            return self._handle_standard_pagination(page_data)
    
    def _handle_serverless_pagination(self, url: str, headers: Dict[str, str]) -> bool:
        """Handle pagination in serverless mode."""
        if self._paginate and not self.errors:
            page_data = self._response.get("data", {})
            info = self._page_info(page_data)
            while info and info.get("hasNextPage") and not self.errors:
                self.vars["after"] = info.get("endCursor")
                # Execute next page immediately
                response = self._client._post(url=url, headers=headers, json={"query": self.query, "variables": self.vars})
                if response.status_code == 200:
                    next_response = response.json()
                    self.errors.extend(next_response.get("errors", []))
                    next_page_data = next_response.get("data", {})
                    self._merge_page(next_page_data)
                    info = self._page_info(next_page_data)
                else:
                    break
            
            self.data = self._aggregated_data
            self._clean_page_info()
        else:
            self.data = self._response.get("data", {})
        return True
    
    def _handle_standard_pagination(self, page_data: Dict[str, Any]) -> bool:
        """Handle pagination in standard (non-serverless) mode."""
        if not self._paginate:
            self.data = page_data
            self._set_done_event()
            return True
        else:
            self._trigger_page_event(page_data)
            info = self._page_info(page_data)
            if info and info.get("hasNextPage"):
                self.vars["after"] = info.get("endCursor")
                self._client._enqueue_request(self)
            else:
                self._finalize_pagination()
            return True
    
    def _trigger_page_event(self, page_data: Dict[str, Any]) -> None:
        """Trigger the page event callback if configured."""
        if self._page_event:
            self._logger.debug(f"on_page_event [Page {self._page}]")
            self._page += 1
            page_info = { 
                "per_page": self.vars.get("first", 0),
                "page": self._page 
            }
            self._page_event({
                "page_data": page_data, 
                "page_info": page_info,
                "errors": self.errors
            })
    
    def _finalize_pagination(self) -> None:
        """Finalize pagination by setting final data and marking as done."""
        self.data = self._aggregated_data
        self._clean_page_info()
        self._set_done_event()
    
    def _clean_page_info(self) -> None:
        """Remove pageInfo from final data."""
        if self.data and "pageInfo" in self.data[next(iter(self.data))]:
            del self.data[next(iter(self.data))]["pageInfo"]
    
    def _handle_failed_response(self, response) -> None:
        """Handle failed HTTP response."""
        error_msg = f"Query failed with status {response.status_code}: {response.text}"
        self.errors.append({"message": response.text})
        self._logger.warning(error_msg)
    
    def _handle_network_error(self, e: requests.RequestException) -> None:
        """Handle network-related errors."""
        error_msg = f"Network error executing query: {e}"
        self.errors.append({"message": str(e), "trace": traceback.format_exc()})
        self._logger.error(error_msg, exc_info=True)
    
    def _handle_unexpected_error(self, e: Exception) -> None:
        """Handle unexpected errors."""
        error_msg = f"Unexpected error executing query: {e}"
        self.errors.append({"message": str(e), "trace": traceback.format_exc()})
        self._logger.error(error_msg, exc_info=True)
    
    def _wait_before_retry(self, retries: int) -> None:
        """Wait before retrying with jitter."""
        sleep_time = self._client._query_retry_time * (1 + random.uniform(0, 0.5))
        self._logger.debug(f"Retrying in {sleep_time:.2f}s (Attempt {retries})")
        time.sleep(sleep_time)
    
    def _handle_final_failure(self) -> None:
        """Handle final failure after all retries exhausted."""
        self._logger.error("Query ultimately failed after retries.")
        self._set_done_event()
    
    def _set_done_event(self) -> None:
        """Set the done event if it exists."""
        if hasattr(self, "_done_event"):
            self._done_event.set()
    
    def _generate_report(self):
        if self._report_request:
            self.report_name = self._report_request.get("name")
            self.stream_report = self._report_request.get("stream", Config.report_stream_by_default())
        return self._current_query_info.get("source", "") == "createReport"

    def _report_workflow(self, response):
        self._report_id = self._response['data']['createReport']['report']['id']
        self.query = """query ReportDownloadUrl($reportId: ID!) {report(id: $reportId) { name lastRun {url status progress runAt}}}"""
        self.vars = {'reportId': self._report_id}

        while True:
            polling_response = self.submit()
            if not polling_response.success():
                self._logger.warning("Failed to get report download URL.")
                time.sleep(self._client._query_retry_time)
                continue

            last_run = polling_response.data["report"]["lastRun"]
            status = last_run["status"]
            progress = last_run["progress"]

            self._logger.info(f"Report status: {status}, Progress: {progress}%")

            if status == "COMPLETED":
                download_url = last_run["url"]
                if not download_url:
                    self.errors.append({"message": "No download URL found in final report status."})
                    return self
                if self.stream_report:
                    self._logger.info("Streaming report")
                    self.data["report_data"] = list(self._stream_report(download_url, self.report_name, as_generator=False, on_page_event=self._page_event))
                else:
                    self._logger.info("Downloading full report")
                    self.data["report_data"] = self._download_report(download_url)
                return self
            time.sleep(self._client._query_retry_time)

    def _stream_report(self, download_url, report_name, as_generator=False, on_page_event=None, chunk_size=8192):
        if as_generator:
            return self._stream_report_generator(download_url, report_name, on_page_event, chunk_size)
        else:
            return list(self._stream_report_generator(download_url, report_name, on_page_event, chunk_size))

    def _stream_report_generator(self, download_url, report_name, on_page_event=None, chunk_size=8192):
        self._logger.info(f"Streaming report: {report_name}")
        response = requests.get(download_url, stream=True)
        if response.status_code == 200:
            content_type = response.headers.get("Content-Type")
            total_size = int(response.headers.get("content-length", 0))
            downloaded = 0

            if "application/json" in content_type:
                decoded_lines = codecs.iterdecode(response.iter_lines(chunk_size), "utf-8")
                for line in decoded_lines:
                    if line:
                        data = json.loads(line)
                        downloaded += len(line)
                        yield data
                        if on_page_event:
                            on_page_event({"name": report_name, "total_size": total_size, "downloaded": downloaded, "status": "In Progress"})
            elif "text/csv" in content_type or "application/csv" in content_type:
                decoded_lines = codecs.iterdecode(response.iter_lines(chunk_size), "utf-8")
                reader = csv.reader(decoded_lines)
                for row in reader:
                    downloaded += len(",".join(row).encode("utf-8"))
                    yield row
            else:
                self._logger.error(f"Unsupported content type: {content_type}")
        else:
            self._logger.error(f"Failed to retrieve report: HTTP {response.status_code}")

    def _download_report(self, download_url):
        try:
            response = requests.get(download_url)
            response.raise_for_status()
            return response.content
        except Exception as e:
            self._logger.error(f"Error downloading report: {e}", exc_info=True)
            self.errors.append({"message": str(e)})
            return None       

    def _merge_page(self, page_data):
        """Merge a page of data into the aggregated results."""
        if not hasattr(self, "_aggregated_data") or self._aggregated_data is None:
            self._aggregated_data = page_data or {}
            return
        
        for key, value in (page_data or {}).items():
            if (isinstance(value, dict)
                and "nodes" in value
                and key in self._aggregated_data
                and isinstance(self._aggregated_data[key], dict)):

                self._aggregated_data[key]["nodes"].extend(value.get("nodes", []))
                if "pageInfo" in value:
                    self._aggregated_data[key]["pageInfo"] = value.get("pageInfo")
            else:
                self._aggregated_data[key] = value

    def _page_info(self, data):
        for _, value in (data or {}).items():
            if isinstance(value, dict) and "pageInfo" in value:
                page_info = value.get("pageInfo")
                return {
                    "hasNextPage": page_info.get("hasNextPage", False),
                    "endCursor": page_info.get("endCursor")
                }
        return None

    def success(self) -> bool:
        return self.data is not None and not self.errors

    def __repr__(self) -> str:
        return f"<WizRequest status={self._status_code} success={self.success()}>"



class WizResponse:
    """
    Acts like a protective class for WizRequests.  Only exposes requried attributes to user
    """
    _EXPOSED_FIELDS = ["query", "vars", "paginate", "report_request", "on_page_event"]

    def __init__(self, request):
        self._request = request

        for attr in self._EXPOSED_FIELDS:
            if hasattr(request, attr):
                if getattr(request, attr):
                    setattr(self, attr, getattr(request, attr))
    @property
    def data(self):
        return self._request.data

    @property
    def errors(self):
        return self._request.errors
    
    @property
    def success(self):
        return self._request.success()
    
    @property
    def node_type(self):
        return self._request._current_query_info.get("source", None)

    def submit(self):
        return self._request.submit()

    def __dir__(self):
        return [attr for attr in super().__dir__() if not attr.startswith('_')]


class WizBatchRequest:
    """
    Manages multiple requests for batch execution.
    Provides efficient concurrent execution within rate limits.
    """
    
    def __init__(self, client: "WizClient") -> None:
        self._client = client
        self._logger = Config.get_logger()
        self._requests: List[WizRequest] = []
        self._responses: List["WizResponse"] = []  # Keep track of WizResponse objects
        self._results: Dict[int, "WizResponse"] = {}
        self._completed_count = 0
        self._total_count = 0
        self._batch_event = threading.Event()
        self._lock = threading.Lock()
        self._progress_callback: Optional[Callable] = None
    
    def add_request(
        self, 
        query: str, 
        vars: Optional[Dict[str, Any]] = None,
        queryCollection: Optional[Union[str, Any]] = None,
        paginate: bool = True,
        **kwargs
    ) -> int:
        """
        Add a request to the batch. Returns the request ID for later reference.
        """
        # Create the response object that users will interact with
        response = self._client.create_request(
            queryCollection=queryCollection,
            query=query,
            vars=vars,
            paginate=paginate,
            **kwargs
        )
        
        request_id = len(self._requests)
        self._requests.append(response._request)  # Store internal WizRequest
        self._responses.append(response)  # Store WizResponse for user access
        self._logger.debug(f"Added request {request_id} to batch")
        return request_id
    
    def add_response(self, response: "WizResponse") -> int:
        """Add an existing WizResponse to the batch."""
        request_id = len(self._requests)
        self._requests.append(response._request)
        self._responses.append(response)
        self._logger.debug(f"Added WizResponse {request_id} to batch")
        return request_id
    
    def set_progress_callback(self, callback: Callable[[int, int], None]) -> None:
        """Set a callback function to track batch progress: callback(completed, total)"""
        self._progress_callback = callback
    
    def submit(self, max_concurrent: int = 5) -> "WizBatchResponse":
        """
        Submit all requests in the batch with controlled concurrency.
        
        Args:
            max_concurrent: Maximum number of requests to process concurrently
        
        Returns:
            WizBatchResponse containing all results
        """
        if not self._requests:
            self._logger.warning("No requests in batch to submit")
            return WizBatchResponse({})
        
        self._total_count = len(self._requests)
        self._completed_count = 0
        self._results.clear()
        self._batch_event.clear()
        
        self._logger.info(f"Submitting batch of {self._total_count} requests")
        
        if Config.serverless():
            return self._submit_sequential()
        else:
            return self._submit_concurrent(max_concurrent)
    
    def _submit_sequential(self) -> "WizBatchResponse":
        """Submit requests sequentially in serverless mode."""
        for i, request in enumerate(self._requests):
            try:
                request.submit()  # Submit the internal WizRequest
                self._results[i] = self._responses[i]  # Store the WizResponse
                self._completed_count += 1
                self._trigger_progress_callback()
            except Exception as e:
                self._logger.error(f"Request {i} failed: {e}")
                # Add error to the request and store the response
                request.errors.append({"message": f"Batch execution failed: {e}"})
                self._results[i] = self._responses[i]
                self._completed_count += 1
                self._trigger_progress_callback()
        
        return WizBatchResponse(self._results)
    
    def _submit_concurrent(self, max_concurrent: int) -> "WizBatchResponse":
        """Submit requests concurrently with controlled parallelism."""
        # Set up completion tracking for each request
        for i, request in enumerate(self._requests):
            # Wrap the original done event to track batch completion
            original_done_event = getattr(request, '_done_event', None)
            request._batch_id = i
            request._batch_parent = self
            
            # Replace or set the done event handler
            if original_done_event:
                request._original_done_event = original_done_event
            request._done_event = threading.Event()
        
        # Submit requests to the queue (rate limiting happens in the client)
        for i, request in enumerate(self._requests):
            self._client._enqueue_request(request)
        
        # Wait for all requests to complete
        self._wait_for_completion()
        
        return WizBatchResponse(self._results)
    
    def _wait_for_completion(self) -> None:
        """Wait for all batch requests to complete."""
        timeout = Config.api_timeout() * len(self._requests)  # Scale timeout with batch size
        
        for i, request in enumerate(self._requests):
            if request._done_event.wait(timeout=timeout):
                self._results[i] = self._responses[i]  # Store WizResponse
                with self._lock:
                    self._completed_count += 1
                    self._trigger_progress_callback()
            else:
                self._logger.error(f"Request {i} timed out")
                # Create a timeout error result
                request.errors.append({"message": "Request timed out in batch"})
                self._results[i] = self._responses[i]  # Store WizResponse even on timeout
                with self._lock:
                    self._completed_count += 1
                    self._trigger_progress_callback()
    
    def _trigger_progress_callback(self) -> None:
        """Trigger the progress callback if set."""
        if self._progress_callback:
            try:
                self._progress_callback(self._completed_count, self._total_count)
            except Exception as e:
                self._logger.error(f"Progress callback error: {e}")
    
    def clear(self) -> None:
        """Clear all requests from the batch."""
        self._requests.clear()
        self._responses.clear()
        self._results.clear()
        self._completed_count = 0
        self._total_count = 0
        self._logger.debug("Batch cleared")
    
    def size(self) -> int:
        """Return the number of requests in the batch."""
        return len(self._requests)
    
    def __len__(self) -> int:
        return len(self._requests)


class WizBatchResponse:
    """
    Contains results from a batch request submission.
    Provides convenient access to individual results and batch statistics.
    """
    
    def __init__(self, results: Dict[int, "WizResponse"]) -> None:
        self._results = results
        self._logger = Config.get_logger()
    
    def get_result(self, request_id: int) -> Optional["WizResponse"]:
        """Get result for a specific request ID."""
        return self._results.get(request_id)
    
    def get_successful_results(self) -> Dict[int, "WizResponse"]:
        """Get all successful results."""
        return {id: resp for id, resp in self._results.items() if resp.success}
    
    def get_failed_results(self) -> Dict[int, "WizResponse"]:
        """Get all failed results."""
        return {id: resp for id, resp in self._results.items() if not resp.success}
    
    def all_successful(self) -> bool:
        """Check if all requests in the batch were successful."""
        return all(resp.success for resp in self._results.values())
    
    def success_count(self) -> int:
        """Return the number of successful requests."""
        return sum(1 for resp in self._results.values() if resp.success)
    
    def failure_count(self) -> int:
        """Return the number of failed requests."""
        return sum(1 for resp in self._results.values() if not resp.success)
    
    def total_count(self) -> int:
        """Return the total number of requests."""
        return len(self._results)
    
    def success_rate(self) -> float:
        """Return the success rate as a percentage."""
        if not self._results:
            return 0.0
        return (self.success_count() / self.total_count()) * 100
    
    def get_all_data(self) -> List[Any]:
        """Get data from all successful requests."""
        return [resp.data for resp in self._results.values() if resp.success and resp.data]
    
    def get_all_errors(self) -> List[Dict[str, Any]]:
        """Get all errors from failed requests."""
        errors = []
        for id, resp in self._results.items():
            if resp.errors:
                errors.extend([{**error, "request_id": id} for error in resp.errors])
        return errors
    
    def iterate_results(self) -> Iterator[tuple[int, "WizResponse"]]:
        """Iterate over all results."""
        for id, resp in self._results.items():
            yield id, resp
    
    def __getitem__(self, request_id: int) -> Optional["WizResponse"]:
        return self.get_result(request_id)
    
    def __len__(self) -> int:
        return len(self._results)
    
    def __iter__(self):
        return iter(self._results.items())
    
    def __repr__(self) -> str:
        return f"<WizBatchResponse: {self.success_count()}/{self.total_count()} successful>"


# ========== ASYNC CLASSES ==========
# These classes provide async functionality using the same business logic

class AsyncWizRequest:
    """Async version of WizRequest - shares most logic with sync version"""
    
    def __init__(
        self,
        client: "WizClient",
        queryCollection: Optional[Union[str, Any]] = None,
        query: Optional[str] = None,
        vars: Optional[Dict[str, Any]] = None,
        paginate: bool = True,
        report_request: Optional[Dict[str, Any]] = None,
        on_page_event: Optional[Callable] = None
    ) -> None:
        # Reuse initialization logic from WizRequest
        sync_request = WizRequest(client, queryCollection, query, vars, paginate, report_request, on_page_event)
        
        # Copy all the setup
        self._client = sync_request._client
        self._logger = sync_request._logger
        self.vars = sync_request.vars
        self.query = sync_request.query
        self._paginate = sync_request._paginate
        self._page_event = sync_request._page_event
        self._report_request = sync_request._report_request
        self.errors: List[Dict[str, Any]] = []
        self.data: Optional[Dict[str, Any]] = None
        self._aggregated_data: Optional[Dict[str, Any]] = None
        self._page = 1
    
    async def submit(self) -> "AsyncWizRequest":
        """Submit request asynchronously"""
        await self._execute_page()
        return self
    
    async def _execute_page(self) -> None:
        """Async version of page execution"""
        if not hasattr(self._client, '_async_session'):
            raise RuntimeError("Async session not initialized. Use client.async_session() context manager.")
        
        session = self._client._async_session
        retries = 0
        url = self._client._api_endpoint()
        headers = self._client._get_headers()
        payload = {"query": self.query, "variables": self.vars}
        
        while retries <= self._client._max_retries:
            try:
                # Rate limiting with semaphore
                semaphore = getattr(self._client, '_async_semaphore', asyncio.Semaphore(10))
                
                async with semaphore:
                    async with session.post(url=url, json=payload, headers=headers) as response:
                        if response.status == 200:
                            response_data = await response.json()
                            self.errors.extend(response_data.get("errors", []))
                            page_data = response_data.get("data", {})
                            self._merge_page(page_data)
                            
                            if not self.errors:
                                await self._handle_pagination(page_data, url, headers, session)
                            return
                        else:
                            error_msg = f"Query failed with status {response.status}"
                            self.errors.append({"message": await response.text()})
                            self._logger.warning(error_msg)
                            
            except asyncio.TimeoutError:
                self._logger.error("Request timed out")
                self.errors.append({"message": "Request timed out"})
            except Exception as e:
                error_msg = f"Async request error: {e}"
                self.errors.append({"message": str(e)})
                self._logger.error(error_msg)
            
            retries += 1
            if retries <= self._client._max_retries:
                await asyncio.sleep(self._client._query_retry_time * retries)
    
    async def _handle_pagination(self, page_data: Dict[str, Any], url: str, headers: Dict[str, str], session: aiohttp.ClientSession) -> None:
        """Handle async pagination"""
        if not self._paginate:
            self.data = page_data
            return
            
        # Trigger page event
        if self._page_event:
            self._page_event({
                "page_data": page_data,
                "page_info": {"per_page": self.vars.get("first", 0), "page": self._page},
                "errors": self.errors
            })
        
        # Check for more pages
        page_info = self._page_info(page_data)
        if page_info and page_info.get("hasNextPage"):
            self.vars["after"] = page_info.get("endCursor")
            self._page += 1
            await self._execute_page()  # Recursive call for next page
        else:
            # Finalize pagination
            self.data = self._aggregated_data
            if self.data and "pageInfo" in self.data.get(next(iter(self.data)), {}):
                del self.data[next(iter(self.data))]["pageInfo"]
    
    def _merge_page(self, page_data: Dict[str, Any]) -> None:
        """Reuse merge logic from sync version"""
        if not hasattr(self, "_aggregated_data") or self._aggregated_data is None:
            self._aggregated_data = page_data or {}
            return

        if not page_data:
            return

        for key, value in page_data.items():
            if key not in self._aggregated_data:
                self._aggregated_data[key] = value
            elif isinstance(value, dict) and "nodes" in value:
                # Merge paginated results
                if key not in self._aggregated_data:
                    self._aggregated_data[key] = {"nodes": []}
                if "nodes" not in self._aggregated_data[key]:
                    self._aggregated_data[key]["nodes"] = []
                
                self._aggregated_data[key]["nodes"].extend(value.get("nodes", []))
                
                # Update pageInfo
                if "pageInfo" in value:
                    self._aggregated_data[key]["pageInfo"] = value["pageInfo"]
    
    def _page_info(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Reuse page info extraction from sync version"""
        if not data:
            return None
            
        for key, value in data.items():
            if isinstance(value, dict) and "pageInfo" in value:
                return {
                    "hasNextPage": value["pageInfo"].get("hasNextPage", False),
                    "endCursor": value["pageInfo"].get("endCursor")
                }
        return None
    
    def success(self) -> bool:
        """Check if request was successful"""
        return self.data is not None and not self.errors


class AsyncWizResponse:
    """Async wrapper for AsyncWizRequest - same interface as WizResponse"""
    
    def __init__(self, request: AsyncWizRequest) -> None:
        self._request = request
    
    async def submit(self) -> "AsyncWizResponse":
        """Submit the async request"""
        await self._request.submit()
        return self
    
    @property
    def success(self) -> bool:
        return self._request.success()
    
    @property
    def data(self) -> Optional[Dict[str, Any]]:
        return self._request.data
    
    @property  
    def errors(self) -> List[Dict[str, Any]]:
        return self._request.errors
    
    def __repr__(self) -> str:
        return f"<AsyncWizResponse success={self.success}>"


class AsyncWizBatchRequest:
    """Async batch request with much higher concurrency than sync version"""
    
    def __init__(self, client: "WizClient", max_concurrent: int = 50) -> None:
        self._client = client
        self._logger = Config.get_logger()
        self._requests: List[AsyncWizRequest] = []
        self._max_concurrent = max_concurrent
        self._progress_callback: Optional[Callable] = None
        
        # Ensure we have an async semaphore on the client
        if not hasattr(client, '_async_semaphore'):
            client._async_semaphore = asyncio.Semaphore(max_concurrent)
    
    def add_request(
        self,
        query: str,
        vars: Optional[Dict[str, Any]] = None,
        queryCollection: Optional[Union[str, Any]] = None,
        paginate: bool = True,
        **kwargs
    ) -> int:
        """Add request to batch"""
        request = AsyncWizRequest(
            client=self._client,
            queryCollection=queryCollection,
            query=query,
            vars=vars,
            paginate=paginate,
            **kwargs
        )
        
        request_id = len(self._requests)
        self._requests.append(request)
        self._logger.debug(f"Added async request {request_id} to batch")
        return request_id
    
    def set_progress_callback(self, callback: Callable[[int, int], None]) -> None:
        """Set progress callback"""
        self._progress_callback = callback
    
    async def submit(self, max_concurrent: Optional[int] = None) -> WizBatchResponse:
        """Submit all requests with high concurrency"""
        if not self._requests:
            return WizBatchResponse({})
        
        concurrent_limit = max_concurrent or self._max_concurrent
        semaphore = asyncio.Semaphore(concurrent_limit)
        
        async def execute_request(index: int, request: AsyncWizRequest) -> tuple[int, AsyncWizResponse]:
            async with semaphore:
                await request.submit()
                response = AsyncWizResponse(request)
                
                if self._progress_callback:
                    self._progress_callback(index + 1, len(self._requests))
                
                return index, response
        
        # Execute all requests concurrently
        tasks = [execute_request(i, req) for i, req in enumerate(self._requests)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        response_map = {}
        for result in results:
            if isinstance(result, Exception):
                self._logger.error(f"Batch request failed: {result}")
                # Create failed response
                # This would need error handling logic
            else:
                index, response = result
                response_map[index] = response
        
        return WizBatchResponse(response_map)
    
    def clear(self) -> None:
        """Clear all requests"""
        self._requests.clear()
    
    def __len__(self) -> int:
        return len(self._requests)


