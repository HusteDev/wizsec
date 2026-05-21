##
##
##   RRRRR   EEEEE    QQQQQ   U     U   EEEEE   SSSSS   TTTTTTT
##   R   R   E       Q     Q  U     U   E       S          T
##   RRRRR   EEEEE   Q  Q  Q  U     U   EEEEE   SSSSS      T
##   R  R    E       Q   QQQ  U     U   E           S      T
##   R   R   EEEEE    QQQQQ    UUUUU    EEEEE   SSSSS      T
##
##
##

# _request.py
import time
import traceback
import random
import codecs
import csv
import re
import json
import threading
from typing import Optional, Dict, Any, List, Tuple, Union, Callable, Iterator

try:
    from pyrate_limiter import BucketFullException
except ImportError:  # older 3.x releases don't re-export via __init__
    from pyrate_limiter.exceptions import BucketFullException  # type: ignore[no-redef]
from graphql import parse, GraphQLError
import asyncio
from .utils import (
    parse_query_metadata,
    ensure_pagination_variables,
    has_totalcount_field,
    build_totalcount_probe_query,
    inject_subscription_filter,
)
from ._schema import SchemaValidator
from .config import Config
from .client import WizClient
from .exceptions import WizQueryError, WizAPIError, WizReportError, WizTimeoutError
from ._transport import stream_get, get as transport_get, TransportError

# ──────────────────────────────────────────────────────────────────────────────
# Built-in queries used by the query-splitting feature.
# Prefixed "WizsecInternal" to avoid collisions with user query collections.
# ──────────────────────────────────────────────────────────────────────────────
_SPLIT_QUERY_CLOUD_ACCOUNTS = """
query WizsecInternalCloudAccounts($first: Int, $after: String) {
  cloudAccounts(first: $first, after: $after) {
    nodes { id externalId name cloudProvider }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_SPLIT_QUERY_PROJECTS = """
query WizsecInternalProjects($first: Int, $after: String) {
  projects(first: $first, after: $after) {
    nodes { id name slug }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def _extract_totalcount(data: Optional[Dict[str, Any]], source: str) -> int:
    """Extract the totalCount integer from a probe response."""
    if not data:
        return 0
    return int((data.get(source) or {}).get("totalCount", 0))


def _schema_supports_totalcount(source: str, client: Any) -> bool:
    """Return True if the schema reports the root field's connection type has totalCount."""
    try:
        schema = SchemaValidator.get_schema(client.environment, client)
        if schema is None:
            return False
        query_type = schema.query_type
        if query_type is None:
            return False
        root_field = query_type.fields.get(source)
        if root_field is None:
            return False
        # Unwrap NonNull / List wrappers to get the named type
        named_type = root_field.type
        while hasattr(named_type, "of_type"):
            named_type = named_type.of_type  # type: ignore[union-attr]
        connection_fields = getattr(named_type, "fields", {})
        return "totalCount" in connection_fields
    except Exception:
        return False


def _get_cached_or_fetch_entities(client: Any) -> List[Dict[str, Any]]:
    """Return the session-cached split entities, fetching and caching them if needed."""
    env_state = client._env_state
    split_by = Config.query_splitting_split_by()
    cache_enabled = Config.query_splitting_cache_subscriptions()

    with env_state._split_entities_lock:
        if cache_enabled and env_state._cached_split_entities is not None:
            return env_state._cached_split_entities

        query = (
            _SPLIT_QUERY_CLOUD_ACCOUNTS
            if split_by == "cloudAccounts"
            else _SPLIT_QUERY_PROJECTS
        )
        node_key = "cloudAccounts" if split_by == "cloudAccounts" else "projects"

        fetch_req = WizRequest(
            client=client,
            query=query,
            vars={"first": 500},
            paginate=True,
        )
        fetch_req._is_sub_request = True  # type: ignore[attr-defined]
        fetch_req.submit()

        if not fetch_req.success() or fetch_req.data is None:
            entities: List[Dict[str, Any]] = []
        else:
            entities = (fetch_req.data.get(node_key) or {}).get("nodes", [])

        if cache_enabled:
            env_state._cached_split_entities = entities

    return entities


def _merge_split_results(
    responses: List[Any],
    source: str,
    logger: Any,
) -> Dict[str, Any]:
    """Merge nodes from multiple per-entity responses into a single data dict."""
    all_nodes: List[Any] = []
    total_count = 0
    sample_structure: Optional[Dict[str, Any]] = None

    for resp in responses:
        if not resp.success or resp.data is None:
            if resp.errors:
                logger.warning("Splitting sub-query failed: %s", resp.errors)
            continue
        connection = resp.data.get(source) or {}
        nodes = connection.get("nodes", [])
        all_nodes.extend(nodes)
        total_count += connection.get("totalCount", len(nodes))
        if sample_structure is None:
            sample_structure = {k: v for k, v in connection.items() if k != "nodes"}

    merged_connection: Dict[str, Any] = dict(sample_structure or {})
    merged_connection["nodes"] = all_nodes
    merged_connection["totalCount"] = total_count
    merged_connection.pop("pageInfo", None)
    return {source: merged_connection}


# Types rejected as queryCollection — anything else (including SimpleNamespace,
# dataclasses, class instances) is accepted and resolved via getattr at use time.
_REJECTED_QC_TYPES = (
    int,
    float,
    bool,
    bytes,
    bytearray,
    list,
    tuple,
    set,
    frozenset,
    dict,
    type(None),
)


class _RequestBase:
    """Shared logic for sync and async request classes."""

    def _init_common(
        self,
        client: "WizClient",
        queryCollection: Optional[Union[str, Any]] = None,
        query: Optional[str] = None,
        vars: Optional[Dict[str, Any]] = None,
        paginate: bool = True,
        report_request: Optional[Dict[str, Any]] = None,
        on_page_event: Optional[Callable] = None,
    ) -> None:
        """Initialize common state shared by sync and async request classes."""
        self._logger = Config.get_logger()
        self._client = client
        self.vars = vars or {}
        self._response: Optional[Dict[str, Any]] = None
        self.errors: List[Dict[str, Any]] = []
        self.data: Optional[Dict[str, Any]] = None
        self._status_code = None
        self._paginate = paginate and not Config.serverless()
        self._report_request = report_request or {}
        self._page_event = on_page_event
        self._page = 0
        self._aggregated_data: Optional[Dict[str, Any]] = None

        self._queryCollection: Optional[Any] = None
        if queryCollection:
            self.queryCollection = queryCollection

        if query is not None:
            self.query = query

        self._limiter_key = self._client._limiter_key(self)

    @property
    def queryCollection(self) -> Optional[Any]:
        """Return the loaded query collection module, or None if unset."""
        if hasattr(self, "_queryCollection") and self._queryCollection:
            return self._queryCollection
        else:
            self._logger.debug("No queryCollection set")
            return None

    @queryCollection.setter
    def queryCollection(self, module: Union[str, Any]) -> None:
        """Set the query collection.

        Accepts:
          - a module name string (auto-imported)
          - a module object (registered in sys.modules for downstream resolvers)
          - any object whose attributes are the GraphQL query strings
            (e.g. types.SimpleNamespace, a dataclass instance, a class instance,
            or sys.modules[__name__] — the calling module itself)

        Primitive and container types (int, float, bool, bytes, list, tuple,
        set, dict, None) are rejected with WizQueryError.
        """
        import importlib, sys, types

        if isinstance(module, str):
            if module not in sys.modules:
                module = importlib.import_module(module)
        elif isinstance(module, types.ModuleType):
            module_name = module.__name__
            if module_name not in sys.modules:
                sys.modules[module_name] = module
                self._logger.debug("Manually added [%s] to sys.modules", module_name)
        elif isinstance(module, _REJECTED_QC_TYPES):
            self._logger.warning(
                "Unexpected type for queryCollection: %s", type(module)
            )
            raise WizQueryError(
                "QueryCollection must be a module, a module name string, "
                "or an object with attributes for each query (e.g. SimpleNamespace)."
            )
        # else: namespace-like object (SimpleNamespace, dataclass, class
        # instance, etc.) — accepted as-is. Missing attributes fall back
        # gracefully via the existing AttributeError handling in `query.setter`.

        self._queryCollection = module

    @property
    def query(self) -> str:
        """Return the resolved GraphQL query string."""
        return self._query

    @query.setter
    def query(self, QUERY: str) -> None:
        """Resolve, validate, and set the GraphQL query string."""
        if not isinstance(QUERY, str):
            self._logger.warning("Query must be a string, got: %s", type(QUERY))
            raise WizQueryError(f"Query must be a string, got: {type(QUERY)}")

        resolved = None
        try:
            parse(QUERY)
            resolved = QUERY
        except GraphQLError:
            if self.queryCollection:
                try:
                    resolved = getattr(self.queryCollection, QUERY)
                    self._logger.debug(f"Resolved query '{QUERY}' from queryCollection")
                except AttributeError:
                    self._logger.debug(
                        f"Query '{QUERY}' not found in queryCollection. Using raw query."
                    )
                    resolved = QUERY
            else:
                resolved = QUERY

        if self._paginate:
            resolved = ensure_pagination_variables(resolved)
        self._query = resolved
        self._current_query_info = parse_query_metadata(self._query)

        if Config.validate_queries():
            SchemaValidator.validate_query(
                self._query,
                self._client.environment,
                client=self._client,
            )

        if self._current_query_info[
            "request_type"
        ].lower() == "mutation" and self._current_query_info["source"].lower() in [
            "createreport",
            "rerunreport",
        ]:
            self._logger.debug("__current_query = %s", self._current_query_info)
            self._report = True

    def _merge_page(self, page_data: Optional[Dict[str, Any]]) -> None:
        """Merge a page of data into the aggregated results."""
        if self._aggregated_data is None:
            self._aggregated_data = page_data or {}
            return

        for key, value in (page_data or {}).items():
            if (
                isinstance(value, dict)
                and "nodes" in value
                and key in self._aggregated_data
                and isinstance(self._aggregated_data[key], dict)
            ):

                if "nodes" not in self._aggregated_data[key]:
                    self._aggregated_data[key]["nodes"] = []
                self._aggregated_data[key]["nodes"].extend(value.get("nodes", []))
                if "pageInfo" in value:
                    self._aggregated_data[key]["pageInfo"] = value["pageInfo"]
            else:
                self._aggregated_data[key] = value

    def _page_info(self, data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Extract pageInfo from the first field that has it."""
        for _, value in (data or {}).items():
            if isinstance(value, dict) and "pageInfo" in value:
                page_info = value["pageInfo"]
                return {
                    "hasNextPage": page_info.get("hasNextPage", False),
                    "endCursor": page_info.get("endCursor"),
                }
        return None

    def _clean_page_info(self) -> None:
        """Remove pageInfo from final data."""
        if self.data:
            first_key = next(iter(self.data), None)
            if (
                first_key
                and isinstance(self.data[first_key], dict)
                and "pageInfo" in self.data[first_key]
            ):
                del self.data[first_key]["pageInfo"]

    def success(self) -> bool:
        """Return True if the request completed with data and no errors."""
        return self.data is not None and not self.errors


class WizRequest(_RequestBase):
    """Synchronous GraphQL request with pagination, retry, and report support."""

    def __init__(
        self,
        client: "WizClient",
        queryCollection: Optional[Union[str, Any]] = None,
        query: Optional[str] = None,
        vars: Optional[Dict[str, Any]] = None,
        paginate: bool = True,
        report_request: Optional[Dict[str, Any]] = None,
        on_page_event: Optional[Callable] = None,
    ) -> None:
        """Initialize a synchronous Wiz GraphQL request."""
        self._init_common(
            client,
            queryCollection,
            query,
            vars,
            paginate,
            report_request,
            on_page_event,
        )
        self._done_event = threading.Event()

    def submit(self) -> "WizRequest":
        """Enqueue this request and wait for completion."""
        if self._maybe_split():
            return self

        if Config.serverless():
            # Skip threading in serverless mode
            self._client._enqueue_request(self)
            return self

        self._done_event.clear()
        self._client._enqueue_request(self)
        self._done_event.wait()
        return self

    def _maybe_split(self) -> bool:
        """Run a pre-emptive totalCount probe and split across entities if over threshold.

        Returns True if splitting took over (self.data is populated); False to proceed normally.
        Splitting is permanently disabled in serverless mode — that check is first and
        unconditional regardless of any config setting.
        """
        # Hard guard: splitting relies on asyncio and threading patterns that are
        # unsafe in serverless (Lambda) environments.
        if Config.serverless():
            return False

        if not Config.query_splitting_enabled():
            return False

        # Prevent recursive probing on sub-requests fired by the splitter itself.
        if getattr(self, "_is_sub_request", False):
            return False

        if self._current_query_info.get("request_type", "").lower() != "query":
            return False

        source = self._current_query_info.get("source", "")
        if not source:
            return False

        # Detection: check whether this query supports totalCount
        detection_mode = Config.query_splitting_detection_mode()
        if detection_mode == "schema":
            if not _schema_supports_totalcount(source, self._client):
                return False
        else:  # static
            if not has_totalcount_field(self._current_query_info.get("fields", {})):
                return False

        # Build the probe query (totalCount-only version of the original)
        probe_query = build_totalcount_probe_query(self._query)
        if probe_query is None:
            return False

        # Run the probe
        probe = WizRequest(
            client=self._client,
            query=probe_query,
            vars=self.vars,
            paginate=False,
        )
        probe._is_sub_request = True  # type: ignore[attr-defined]
        probe._done_event.clear()
        self._client._enqueue_request(probe)
        probe._done_event.wait()

        if not probe.success():
            self._logger.debug("query_splitting: probe failed, skipping split")
            return False

        total = _extract_totalcount(probe.data, source)
        threshold = Config.query_splitting_threshold()
        self._logger.info(
            "query_splitting: probe totalCount=%d (threshold=%d)", total, threshold
        )

        if total <= threshold:
            return False

        # Over threshold — attempt to split
        filter_path = Config.query_splitting_filter_path()
        if not filter_path:
            self._logger.warning(
                "query_splitting: totalCount=%d exceeds threshold but query_splitting.filter_path "
                "is not configured; running original query without splitting",
                total,
            )
            return False

        entities = _get_cached_or_fetch_entities(self._client)
        if not entities:
            self._logger.warning(
                "query_splitting: no entities returned for split_by=%s; running original query",
                Config.query_splitting_split_by(),
            )
            return False

        self._logger.info(
            "query_splitting: splitting query across %d entities (max_concurrent=%d)",
            len(entities),
            Config.query_splitting_max_concurrent(),
        )

        # Fire async sub-queries, one per entity
        results = asyncio.run(self._run_split_async(entities, filter_path))
        self.data = _merge_split_results(results, source, self._logger)
        self._set_done_event()
        return True

    async def _run_split_async(
        self, entities: List[Dict[str, Any]], filter_path: str
    ) -> List[Any]:
        """Run one async sub-query per entity and return all responses."""
        async with self._client.async_session() as async_client:
            batch = AsyncWizBatchRequest(
                client=async_client,
                max_concurrent=Config.query_splitting_max_concurrent(),
            )
            for entity in entities:
                entity_id = entity.get("id")
                if not entity_id:
                    continue
                scoped_vars = inject_subscription_filter(
                    self.vars, filter_path, [entity_id]
                )
                req_id = batch.add_request(
                    query=self._query,
                    vars=scoped_vars,
                    paginate=self._paginate,
                )
                # Mark the internal request as a sub-request to skip recursive probing
                batch._requests[req_id]._is_sub_request = True  # type: ignore[attr-defined]

            batch_response = await batch.submit()

        return [resp for _, resp in batch_response]

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
                    non_retryable = self._handle_failed_response(response)
                    self._logger.debug(f"Payload: {payload}")
                    if non_retryable:
                        self._handle_final_failure()
                        return

            except TransportError as e:
                self._handle_network_error(
                    e, attempt=retries, max_retries=self._client._max_retries
                )
            except Exception as e:
                self._handle_unexpected_error(e)

            retries += 1
            self._wait_before_retry(retries)

        self._handle_final_failure()

    def _process_successful_response(
        self, response: Any, url: str, headers: Dict[str, str]
    ) -> bool:
        """Process a successful HTTP response. Returns True if processing is complete."""
        self._response = response.json()
        assert self._response is not None
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
        assert self._response is not None
        if self._paginate and not self.errors:
            page_data = self._response.get("data", {})
            info = self._page_info(page_data)
            while info and info.get("hasNextPage") and not self.errors:
                self.vars["after"] = info.get("endCursor")
                # Execute next page immediately
                response = self._client._post(
                    url=url,
                    headers=headers,
                    json={"query": self.query, "variables": self.vars},
                )
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
            page_info = {"per_page": self.vars.get("first", 0), "page": self._page}
            self._page_event(
                {"page_data": page_data, "page_info": page_info, "errors": self.errors}
            )

    def _finalize_pagination(self) -> None:
        """Finalize pagination by setting final data and marking as done."""
        self.data = self._aggregated_data
        self._clean_page_info()
        self._set_done_event()

    def _handle_failed_response(self, response: Any) -> bool:
        """Handle failed HTTP response. Returns True for non-retryable errors (4xx)."""
        error_msg = f"Query failed with status {response.status_code}: {response.text}"
        self.errors.append({"message": response.text})
        self._logger.warning(error_msg)
        # 4xx errors are client-side — retrying will never help
        return 400 <= response.status_code < 500

    def _handle_network_error(
        self, e: TransportError, attempt: int = 0, max_retries: int = 0
    ) -> None:
        """Handle network-related errors. Logs a clean warning per attempt; final result logged by _handle_final_failure."""
        self.errors.append({"message": str(e), "trace": traceback.format_exc()})
        self._logger.warning(
            "Network error (attempt %d/%d): %s", attempt + 1, max_retries + 1, e
        )

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
        last_error = self.errors[-1]["message"] if self.errors else "unknown error"
        self._logger.error(
            "Query failed after %d retries: %s", self._client._max_retries, last_error
        )
        self._set_done_event()

    def _set_done_event(self) -> None:
        """Set the done event if it exists."""
        if hasattr(self, "_done_event"):
            self._done_event.set()

    def _generate_report(self) -> bool:
        """Check if the current query is a report creation and configure report settings."""
        if self._report_request:
            self.report_name = self._report_request.get("name")
            self.stream_report = self._report_request.get(
                "stream", Config.report_stream_by_default()
            )
        return self._current_query_info.get("source", "") == "createReport"

    def _report_workflow(self, response: Any) -> Optional["WizRequest"]:
        """Poll for report completion and download/stream the result."""
        assert self._response is not None
        self._report_id = self._response["data"]["createReport"]["report"]["id"]
        self.query = """query ReportDownloadUrl($reportId: ID!) {report(id: $reportId) { name lastRun {url status progress runAt}}}"""
        self.vars = {"reportId": self._report_id}

        while True:
            polling_response = self.submit()
            if not polling_response.success():
                self._logger.warning("Failed to get report download URL.")
                time.sleep(self._client._query_retry_time)
                continue

            assert polling_response.data is not None
            last_run = polling_response.data["report"]["lastRun"]
            status = last_run["status"]
            progress = last_run["progress"]

            self._logger.info(f"Report status: {status}, Progress: {progress}%")

            if status == "COMPLETED":
                download_url = last_run["url"]
                if not download_url:
                    self.errors.append(
                        {"message": "No download URL found in final report status."}
                    )
                    return self
                assert self.data is not None
                if self.stream_report:
                    self._logger.info("Streaming report")
                    self.data["report_data"] = list(
                        self._stream_report(
                            download_url,
                            self.report_name,
                            as_generator=False,
                            on_page_event=self._page_event,
                        )
                    )
                else:
                    self._logger.info("Downloading full report")
                    self.data["report_data"] = self._download_report(download_url)
                return self
            time.sleep(self._client._query_retry_time)

    def _stream_report(
        self,
        download_url: str,
        report_name: Optional[str],
        as_generator: bool = False,
        on_page_event: Optional[Callable] = None,
        chunk_size: int = 8192,
    ) -> Union[Iterator[Any], List[Any]]:
        """Stream or fully download a report from the given URL."""
        if as_generator:
            return self._stream_report_generator(
                download_url, report_name, on_page_event, chunk_size
            )
        else:
            return list(
                self._stream_report_generator(
                    download_url, report_name, on_page_event, chunk_size
                )
            )

    def _stream_report_generator(
        self,
        download_url: str,
        report_name: Optional[str],
        on_page_event: Optional[Callable] = None,
        chunk_size: int = 8192,
    ) -> Iterator[Any]:
        """Yield report rows/records line-by-line from a streaming download."""
        self._logger.info(f"Streaming report: {report_name}")
        with stream_get(download_url) as response:
            if response.status_code == 200:
                content_type = response.headers.get("Content-Type", "")
                total_size = int(response.headers.get("content-length", 0))
                downloaded = 0

                if "application/json" in content_type:
                    for line in response.iter_lines():
                        if line:
                            data = json.loads(line)
                            downloaded += len(line.encode("utf-8"))
                            yield data
                            if on_page_event:
                                on_page_event(
                                    {
                                        "name": report_name,
                                        "total_size": total_size,
                                        "downloaded": downloaded,
                                        "status": "In Progress",
                                    }
                                )
                elif "text/csv" in content_type or "application/csv" in content_type:
                    for line in response.iter_lines():
                        if line:
                            row = next(csv.reader([line]))
                            downloaded += len(line.encode("utf-8"))
                            yield row
                else:
                    self._logger.error(f"Unsupported content type: {content_type}")
            else:
                self._logger.error(
                    f"Failed to retrieve report: HTTP {response.status_code}"
                )

    def _download_report(self, download_url: str) -> Optional[bytes]:
        """Download a report file from the given URL."""
        try:
            response = transport_get(download_url)
            response.raise_for_status()
            return response.content
        except Exception as e:
            self._logger.error(f"Error downloading report: {e}", exc_info=True)
            self.errors.append({"message": str(e)})
            return None

    def __repr__(self) -> str:
        """Return string representation with status code and success state."""
        return f"<WizRequest status={self._status_code} success={self.success()}>"


class WizResponse:
    """Protective wrapper for WizRequest that exposes only public attributes."""

    _EXPOSED_FIELDS: List[str] = [
        "query",
        "vars",
        "paginate",
        "report_request",
        "on_page_event",
    ]

    def __init__(self, request: WizRequest) -> None:
        """Initialize response wrapper around a WizRequest."""
        self._request = request

        for attr in self._EXPOSED_FIELDS:
            if hasattr(request, attr):
                if getattr(request, attr):
                    setattr(self, attr, getattr(request, attr))

    @property
    def data(self) -> Optional[Dict[str, Any]]:
        """Return the response data from the underlying request."""
        return self._request.data

    @property
    def errors(self) -> List[Dict[str, Any]]:
        """Return the list of errors from the underlying request."""
        return self._request.errors

    @property
    def success(self) -> bool:
        """Return True if the underlying request succeeded."""
        return self._request.success()

    @property
    def node_type(self) -> Optional[str]:
        """Return the GraphQL source/node type of the query."""
        return self._request._current_query_info.get("source", None)

    def submit(self) -> WizRequest:
        """Submit the underlying request and return it."""
        return self._request.submit()

    def __dir__(self) -> List[str]:
        """Return only public attribute names."""
        return [attr for attr in super().__dir__() if not attr.startswith("_")]


class WizBatchRequest:
    """
    Manages multiple requests for batch execution.
    Provides efficient concurrent execution within rate limits.
    """

    def __init__(self, client: "WizClient") -> None:
        """Initialize a batch request manager for the given client."""
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
        **kwargs,
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
            **kwargs,
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
            original_done_event = getattr(request, "_done_event", None)
            request._batch_id = i  # type: ignore[attr-defined]
            request._batch_parent = self  # type: ignore[attr-defined]

            # Replace or set the done event handler
            if original_done_event:
                request._original_done_event = original_done_event  # type: ignore[attr-defined]
            request._done_event = threading.Event()

        # Submit requests to the queue (rate limiting happens in the client)
        for i, request in enumerate(self._requests):
            self._client._enqueue_request(request)

        # Wait for all requests to complete
        self._wait_for_completion()

        return WizBatchResponse(self._results)

    def _wait_for_completion(self) -> None:
        """Wait for all batch requests to complete."""
        timeout = Config.api_timeout() * len(
            self._requests
        )  # Scale timeout with batch size

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
                self._results[i] = self._responses[
                    i
                ]  # Store WizResponse even on timeout
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
        """Return the number of requests in the batch."""
        return len(self._requests)


class WizBatchResponse:
    """
    Contains results from a batch request submission.
    Provides convenient access to individual results and batch statistics.
    """

    def __init__(self, results: Dict[int, "WizResponse"]) -> None:
        """Initialize batch response with a mapping of request IDs to responses."""
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
        return [
            resp.data for resp in self._results.values() if resp.success and resp.data
        ]

    def get_all_errors(self) -> List[Dict[str, Any]]:
        """Get all errors from failed requests."""
        errors = []
        for id, resp in self._results.items():
            if resp.errors:
                errors.extend([{**error, "request_id": id} for error in resp.errors])
        return errors

    def iterate_results(self) -> Iterator[Tuple[int, "WizResponse"]]:
        """Iterate over all results as (request_id, response) pairs."""
        for id, resp in self._results.items():
            yield id, resp

    def __getitem__(self, request_id: int) -> Optional["WizResponse"]:
        """Get a result by request ID using bracket notation."""
        return self.get_result(request_id)

    def __len__(self) -> int:
        """Return the total number of results."""
        return len(self._results)

    def __iter__(self) -> Iterator[Tuple[int, "WizResponse"]]:
        """Iterate over all results as (request_id, response) pairs."""
        return iter(self._results.items())

    def __repr__(self) -> str:
        """Return string representation with success/total counts."""
        return f"<WizBatchResponse: {self.success_count()}/{self.total_count()} successful>"


# ========== ASYNC CLASSES ==========
# These classes provide async functionality using the same business logic


class AsyncWizRequest(_RequestBase):
    """Async version of WizRequest — shares query/pagination logic via _RequestBase."""

    def __init__(
        self,
        client: "WizClient",
        queryCollection: Optional[Union[str, Any]] = None,
        query: Optional[str] = None,
        vars: Optional[Dict[str, Any]] = None,
        paginate: bool = True,
        report_request: Optional[Dict[str, Any]] = None,
        on_page_event: Optional[Callable] = None,
    ) -> None:
        """Initialize an asynchronous Wiz GraphQL request."""
        self._init_common(
            client,
            queryCollection,
            query,
            vars,
            paginate,
            report_request,
            on_page_event,
        )

    async def submit(self) -> "AsyncWizRequest":
        """Submit request asynchronously and return self on completion."""
        self._client._check_token()
        if await self._maybe_split_async():
            return self
        await self._execute_page()
        return self

    async def _maybe_split_async(self) -> bool:
        """Async counterpart of WizRequest._maybe_split().

        Identical logic but uses await throughout — no asyncio.run() needed
        since we are already in an async context.
        Splitting is permanently disabled in serverless mode.
        """
        if Config.serverless():
            return False

        if not Config.query_splitting_enabled():
            return False

        if getattr(self, "_is_sub_request", False):
            return False

        if self._current_query_info.get("request_type", "").lower() != "query":
            return False

        source = self._current_query_info.get("source", "")
        if not source:
            return False

        detection_mode = Config.query_splitting_detection_mode()
        if detection_mode == "schema":
            if not _schema_supports_totalcount(source, self._client):
                return False
        else:
            if not has_totalcount_field(self._current_query_info.get("fields", {})):
                return False

        probe_query = build_totalcount_probe_query(self._query)
        if probe_query is None:
            return False

        probe = AsyncWizRequest(
            client=self._client,
            query=probe_query,
            vars=self.vars,
            paginate=False,
        )
        probe._is_sub_request = True  # type: ignore[attr-defined]
        await probe.submit()

        if not probe.success():
            self._logger.debug("query_splitting: async probe failed, skipping split")
            return False

        total = _extract_totalcount(probe.data, source)
        threshold = Config.query_splitting_threshold()
        self._logger.info(
            "query_splitting: async probe totalCount=%d (threshold=%d)",
            total,
            threshold,
        )

        if total <= threshold:
            return False

        filter_path = Config.query_splitting_filter_path()
        if not filter_path:
            self._logger.warning(
                "query_splitting: totalCount=%d exceeds threshold but query_splitting.filter_path "
                "is not configured; running original query without splitting",
                total,
            )
            return False

        # Run the blocking sync entity fetch in a thread executor to avoid
        # stalling the event loop while waiting on _done_event.wait().
        loop = asyncio.get_event_loop()
        entities = await loop.run_in_executor(
            None, _get_cached_or_fetch_entities, self._client
        )
        if not entities:
            self._logger.warning(
                "query_splitting: no entities for split_by=%s; running original query",
                Config.query_splitting_split_by(),
            )
            return False

        self._logger.info(
            "query_splitting: async splitting across %d entities", len(entities)
        )

        batch = AsyncWizBatchRequest(
            client=self._client,
            max_concurrent=Config.query_splitting_max_concurrent(),
        )
        for entity in entities:
            entity_id = entity.get("id")
            if not entity_id:
                continue
            scoped_vars = inject_subscription_filter(
                self.vars, filter_path, [entity_id]
            )
            req_id = batch.add_request(
                query=self._query,
                vars=scoped_vars,
                paginate=self._paginate,
            )
            batch._requests[req_id]._is_sub_request = True  # type: ignore[attr-defined]

        batch_response = await batch.submit()
        responses = [resp for _, resp in batch_response]
        self.data = _merge_split_results(responses, source, self._logger)
        return True

    async def _execute_page(self) -> None:
        """Async version of page execution — iterative pagination (no recursion)."""
        if not hasattr(self._client, "_async_session"):
            raise RuntimeError(
                "Async session not initialized. Use client.async_session() context manager."
            )

        client = self._client._async_session  # type: ignore[attr-defined]
        semaphore = getattr(self._client, "_async_semaphore", asyncio.Semaphore(10))
        limiter = self._client._get_limiter(self._limiter_key)

        # Lazily attach a shared rate-ok event to the client so every
        # coroutine in the same batch coordinates through one object.
        # asyncio.Event must be created inside the running loop, so we
        # can't do this at client construction time.
        # Use isinstance rather than hasattr: MagicMock (used in tests)
        # always returns True for hasattr, causing rate_ok.wait() to
        # await a MagicMock instead of a real coroutine.
        if not isinstance(getattr(self._client, "_rate_ok", None), asyncio.Event):
            self._client._rate_ok = asyncio.Event()  # type: ignore[attr-defined]
            self._client._rate_ok.set()  # type: ignore[attr-defined]
        rate_ok: asyncio.Event = self._client._rate_ok  # type: ignore[attr-defined]

        while True:
            url = self._client._api_endpoint()
            headers = self._client._get_headers()
            payload = {"query": self.query, "variables": self.vars}
            retries = 0
            page_data = None

            while retries <= self._client._max_retries:
                try:
                    # Wait for any server-side 429 backoff to clear, then
                    # acquire a local rate-limit slot — neither counts as a
                    # retry so failures here never burn retry attempts.
                    while True:
                        await rate_ok.wait()  # blocks while event is clear
                        try:
                            await asyncio.to_thread(
                                limiter.try_acquire, self._limiter_key
                            )
                            break
                        except BucketFullException:
                            await asyncio.sleep(0.1)

                    async with semaphore:
                        response = await client.post(
                            url=url, json=payload, headers=headers
                        )
                        if response.status_code == 200:
                            response_data = response.json()
                            self.errors.extend(response_data.get("errors", []))
                            page_data = response_data.get("data", {})
                            self._merge_page(page_data)
                            break
                        elif response.status_code == 429:
                            # Pause every coroutine sharing this client by
                            # clearing the event.  Only the first 429 winner
                            # clears it; subsequent ones see it already clear
                            # and just sleep for their own retry_after window.
                            retry_after = int(response.headers.get("Retry-After", "10"))
                            rate_ok.clear()
                            self._logger.warning(
                                f"Rate limited by server (429) — "
                                f"backing off {retry_after}s"
                            )
                            retries += 1
                            await asyncio.sleep(retry_after)
                            rate_ok.set()  # unblocks all waiting coroutines
                            continue
                        else:
                            error_msg = (
                                f"Query failed with status {response.status_code}"
                            )
                            self.errors.append({"message": response.text})
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
            else:
                # All retries exhausted for this page
                return

            # If errors occurred, stop paginating
            if self.errors:
                return

            # Handle pagination decision
            if not self._paginate:
                self.data = page_data
                return

            if self._page_event:
                self._page_event(
                    {
                        "page_data": page_data,
                        "page_info": {
                            "per_page": self.vars.get("first", 0),
                            "page": self._page,
                        },
                        "errors": self.errors,
                    }
                )

            page_info = self._page_info(page_data)
            if page_info and page_info.get("hasNextPage"):
                self.vars["after"] = page_info.get("endCursor")
                self._page += 1
            else:
                self.data = self._aggregated_data
                self._clean_page_info()
                return


class AsyncWizResponse:
    """Async wrapper for AsyncWizRequest with the same interface as WizResponse."""

    def __init__(self, request: AsyncWizRequest) -> None:
        """Initialize async response wrapper around an AsyncWizRequest."""
        self._request = request

    async def submit(self) -> "AsyncWizResponse":
        """Submit the async request and return self on completion."""
        await self._request.submit()
        return self

    @property
    def success(self) -> bool:
        """Return True if the underlying request succeeded."""
        return self._request.success()

    @property
    def data(self) -> Optional[Dict[str, Any]]:
        """Return the response data from the underlying request."""
        return self._request.data

    @property
    def errors(self) -> List[Dict[str, Any]]:
        """Return the list of errors from the underlying request."""
        return self._request.errors

    def __repr__(self) -> str:
        """Return string representation with success status."""
        return f"<AsyncWizResponse success={self.success}>"


class AsyncWizBatchRequest:
    """Async batch request with much higher concurrency than sync version."""

    def __init__(self, client: "WizClient", max_concurrent: int = 50) -> None:
        """Initialize an async batch request manager."""
        self._client = client
        self._logger = Config.get_logger()
        self._requests: List[AsyncWizRequest] = []
        self._max_concurrent = max_concurrent
        self._progress_callback: Optional[Callable] = None

        # Ensure we have an async semaphore on the client
        if not hasattr(client, "_async_semaphore"):
            client._async_semaphore = asyncio.Semaphore(max_concurrent)  # type: ignore[attr-defined]

    def add_request(
        self,
        query: str,
        vars: Optional[Dict[str, Any]] = None,
        queryCollection: Optional[Union[str, Any]] = None,
        paginate: bool = True,
        **kwargs,
    ) -> int:
        """Add request to batch"""
        request = AsyncWizRequest(
            client=self._client,
            queryCollection=queryCollection,
            query=query,
            vars=vars,
            paginate=paginate,
            **kwargs,
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

        async def execute_request(
            index: int, request: AsyncWizRequest
        ) -> Tuple[int, AsyncWizResponse]:
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
        response_map: Dict[int, "AsyncWizResponse"] = {}
        for result in results:
            if isinstance(result, BaseException):
                self._logger.error(f"Batch request failed: {result}")
                # Create failed response
                # This would need error handling logic
            else:
                index, response = result
                response_map[index] = response

        return WizBatchResponse(response_map)  # type: ignore[arg-type]

    def clear(self) -> None:
        """Clear all requests from the batch."""
        self._requests.clear()

    def __len__(self) -> int:
        """Return the number of requests in the batch."""
        return len(self._requests)
