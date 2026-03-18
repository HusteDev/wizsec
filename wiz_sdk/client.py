##
##
##   CCCCC   L        IIIIIII   EEEEE   N     N   TTTTTTT
##  C        L           I      E       NN    N      T
##  C        L           I      EEEEE   N N   N      T
##  C        L           I      E       N  N  N      T
##   CCCCC   LLLLLL   IIIIIII   EEEEE   N     N      T
##
##
##

# client.py
import threading
import time
import os
import base64
import json
import queue
from typing import TYPE_CHECKING, Optional, Dict, Any, Union
import asyncio
from contextlib import contextmanager
from .config import Config
from . import utils
import webbrowser
from .exceptions import (
    WizAuthenticationError, WizAPIError, WizCredentialsError,
    WizConfigurationError, WizTimeoutError, WizError
)
from ._registry import EnvironmentRegistry, ProfileRegistry
from ._transport import post as transport_post, create_async_client, TransportError

if TYPE_CHECKING:
    from ._request import WizRequest
    from ._request import WizResponse


class WizClient:
    _headers_auth = {"Content-Type": "application/x-www-form-urlencoded"}
    _clients: Dict[tuple, "WizClient"] = {}
    _clients_lock = threading.Lock()

    def __new__(cls, environment: str = "", profile: str = "", *args, **kwargs) -> "WizClient":
        environment = environment or Config.default_domain()
        profile = profile or "default"
        key = (environment, profile)

        if Config.serverless():
            return super().__new__(cls)

        with cls._clients_lock:
            if key in cls._clients:
                return cls._clients[key]
            instance = super().__new__(cls)
            cls._clients[key] = instance
            return instance

    def __init__(
        self,
        environment: str = "",
        profile: str = "",
        client_id: str = "",
        client_secret: str = "",
        grant_type: str = "",
        log_lvl: Optional[Union[str, int]] = None,
        serverless: bool = False,
        interactive: bool = False
    ) -> None:
        if getattr(self, '_initialized', False):
            return

        if log_lvl:
            Config.set_log_level(log_lvl)

        self._logger = Config.get_logger()
        self.profile = profile or "default"
        self.environment = environment or Config.default_domain()
        self._domain = Config.domain_root(self.environment)
        self._client_id = client_id
        self._client_secret = client_secret
        self._grant_type = grant_type or Config.grant_type()
        self._is_service_account = not self._grant_type.startswith("device")
        self._query_retry_time = Config.api_retry()
        self._max_retries = Config.api_max_retries()
        self._proxies = Config.get_proxies()
        self._verbose = Config.verbose_mode()
        self.credential_storage = "prompt" if interactive else Config.storage_method()
        self.serverless = serverless or Config.serverless()

        # Obtain shared per-environment and per-profile state
        self._env_state = EnvironmentRegistry.get_or_create(self.environment)
        self._profile_state = ProfileRegistry.get_or_create(self.profile)

        self._logger.debug(f"Client Request: environment={self.environment}, profile={self.profile}, grant_type={self._grant_type}")
        self._logger.info(f"WizClient Initialized:  environment={self.environment}, profile={self.profile}, grant_type={self._grant_type}")

        self._initialize_headers()
        self._preload_credentials()
        self._initialized = True

    # ========== PROPERTIES ==========

    @property
    def access_token(self) -> Optional[str]:
        return self._profile_state.token_data.get("access_token")

    @access_token.setter
    def access_token(self, value: str) -> None:
        self._profile_state.token_data["access_token"] = value

    @property
    def is_service_account(self):
        return self._is_service_account

    @is_service_account.setter
    def is_service_account(self, value):
        self._is_service_account = value

    @property
    def client_id(self):
        return self._profile_state.client_id

    @client_id.setter
    def client_id(self, value):
        self._profile_state.client_id = value

    @property
    def client_secret(self):
        return self._profile_state.client_secret

    @client_secret.setter
    def client_secret(self, value):
        self._profile_state.client_secret = value

    @property
    def dc(self):
        return self._env_state.dc

    @dc.setter
    def dc(self, value):
        self._env_state.dc = value

    # ========== HEADERS ==========

    def _initialize_headers(self):
        self._logger.debug("Initializing headers")
        user_agent = f'{Config.app_name()}/{Config.release_version()}'
        self._env_state.headers = {
            "Content-Type": "application/json",
            "User-Agent": user_agent
        }

    # ========== QUEUE & WORKER ==========
    # Queue and worker are per-environment so all clients on the same
    # environment share a single request pipeline and rate limiter.

    def _enqueue_request(self, request: "WizRequest"):
        """Add a request to the environment's query queue and start the worker."""
        self._logger.debug("Enqueuing request")

        if Config.serverless():
            self._logger.debug("Serverless mode: executing request immediately")
            try:
                request._execute_page()
            except Exception as e:
                self._logger.error(f"Serverless execution error: {e}")
                request.errors.append({"message": str(e)})
                if hasattr(request, "_done_event"):
                    request._done_event.set()
            return

        self._env_state.queue.put(request)
        self._start_worker()

    def _start_worker(self):
        env = self._env_state
        with env.queue_lock:
            if env.worker_thread and env.worker_thread.is_alive():
                return
            env.stop_event.clear()
            env.worker_thread = threading.Thread(
                target=self._process_queue, daemon=True
            )
            env.worker_thread.start()

    def _process_queue(self):
        env = self._env_state
        while not env.stop_event.is_set():
            try:
                request = env.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self._logger.debug("Processing request from queue")
            try:
                request._execute_page()
            except Exception as e:
                self._logger.error(f"Queue processing error: {e}")
            finally:
                env.queue.task_done()

    def stop_worker(self):
        """Stop the background queue worker for this environment."""
        env = self._env_state
        env.stop_event.set()
        if env.worker_thread:
            env.worker_thread.join(timeout=1)
        env.worker_thread = None

    # ========== REQUEST CREATION ==========

    def create_request(
        self,
        queryCollection: Optional[str] = None,
        query: Optional[str] = None,
        vars: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> "WizResponse":
        self._logger.debug(f"Creating request with query: {query[:50] if query else 'None'}...")
        from ._request import WizRequest, WizResponse
        request = WizRequest(client=self, queryCollection=queryCollection, query=query, vars=vars, **kwargs)
        return WizResponse(request)

    def create_batch_request(self) -> "WizBatchRequest":
        """
        Create a new batch request for submitting multiple queries concurrently.

        Returns:
            WizBatchRequest: A batch request manager

        Example:
            batch = client.create_batch_request()
            batch.add_request("query1", {"var1": "value1"})
            batch.add_request("query2", {"var2": "value2"})
            results = batch.submit()
        """
        self._logger.debug("Creating batch request")
        from ._request import WizBatchRequest
        return WizBatchRequest(client=self)

    # ========== ASYNC METHODS ==========

    async def create_async_request(
        self,
        queryCollection: Optional[str] = None,
        query: Optional[str] = None,
        vars: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> "AsyncWizResponse":
        """
        Async version of create_request. Returns an async-enabled response.

        Example:
            async with client.async_session() as session_client:
                response = await session_client.create_async_request(query="...", vars={})
                result = await response.submit()
        """
        self._logger.debug(f"Creating async request with query: {query[:50] if query else 'None'}...")
        from ._request import AsyncWizRequest, AsyncWizResponse
        request = AsyncWizRequest(client=self, queryCollection=queryCollection, query=query, vars=vars, **kwargs)
        return AsyncWizResponse(request)

    async def create_async_batch_request(self) -> "AsyncWizBatchRequest":
        """
        Async version of create_batch_request with higher concurrency support.

        Example:
            async with client.async_session() as session_client:
                batch = await session_client.create_async_batch_request()
                batch.add_request("query1", vars1)
                batch.add_request("query2", vars2)
                results = await batch.submit(max_concurrent=50)
        """
        self._logger.debug("Creating async batch request")
        from ._request import AsyncWizBatchRequest
        return AsyncWizBatchRequest(client=self)

    def async_session(self, client: Optional[Any] = None):
        """
        Context manager for async operations. Manages async HTTP client lifecycle.

        Example:
            async with client.async_session() as async_client:
                response = await async_client.create_async_request(query="...")
                result = await response.submit()
        """
        class AsyncEnabledClient:
            def __init__(self, sync_client, async_client):
                self._sync_client = sync_client
                self._async_client = async_client
                self._owns_client = async_client is None

            async def __aenter__(self):
                if self._owns_client:
                    ca_bundle = os.environ.get('REQUESTS_CA_BUNDLE')
                    verify = ca_bundle if ca_bundle else True
                    self._async_client = create_async_client(
                        timeout=Config.api_timeout(),
                        verify=verify,
                    )
                self._sync_client._async_session = self._async_client
                return self._sync_client

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                if self._owns_client and self._async_client:
                    await self._async_client.aclose()
                if hasattr(self._sync_client, '_async_session'):
                    delattr(self._sync_client, '_async_session')

        return AsyncEnabledClient(self, client)

    # ========== HTTP & API ==========

    def _api_endpoint(self) -> str:
        dc = self.dc
        endpoint = f"https://api.{dc}.{self._domain}/graphql"
        self._logger.debug(f"API endpoint resolved: {endpoint}")
        return endpoint

    def _get_headers(self) -> Dict[str, str]:
        headers = dict(self._env_state.headers)
        safe_keys = {k: ("***" if k == "Authorization" else v) for k, v in headers.items()}
        self._logger.debug(f"Headers fetched: {safe_keys}")
        return headers

    def _post(self, **kwargs):
        self._logger.debug(f"POST to {self._api_endpoint()}")
        self._logger.debug(f"POST data: {kwargs.get('json', {})}")
        try:
            ca_bundle = os.environ.get('REQUESTS_CA_BUNDLE')
            verify = ca_bundle if ca_bundle else True
            response = transport_post(
                proxy=self._proxies.get("https") or self._proxies.get("http"),
                verify=verify,
                timeout=Config.api_timeout(),
                **kwargs
            )
            self._logger.verbose(f"Response status code: {response.status_code}")
            return response
        except TransportError as e:
            self._logger.error(f"POST request failed: {e}", exc_info=True)
            raise

    # ========== RATE LIMITING ==========

    def _limiter_key(self, request: "WizRequest" = None):
        info = getattr(request, "_current_query_info", {}) or {}
        qtype = str(info.get("request_type", "query")).lower()
        source = "service" if self._is_service_account else "user"
        self._logger.debug("Limiter key: %s_%s", qtype, source)
        return f"{qtype}_{source}"

    def _get_limiter(self, key):
        return self._env_state.get_limiter(key)

    # ========== AUTHENTICATION ==========

    def _check_token(self):
        self._logger.debug("Checking token for profile: %s", self.profile)
        with self._profile_state.auth_lock:
            if Config.serverless():
                self._logger.verbose("Serverless mode: forcing fresh authentication")
                self._authenticate()
            if not self._profile_state.token_data:
                self._logger.verbose("Existing token not found; attempting to authenticate.")
                self._authenticate()
            elif self._token_expired():
                self._logger.verbose("Token expired; refreshing.")
                self._authenticate(refresh=True)
            else:
                self._logger.debug("Valid token found; no action needed.")

    def _preload_credentials(self) -> None:
        """Load credentials based on configuration."""
        self._logger.debug("Attempting to preload credentials")
        if self._grant_type != "client_credentials":
            self._logger.debug("Grant type is not client_credentials; skipping credential retrieval")
            return

        try:
            self._load_credentials_by_storage_method()
            self._validate_and_store_credentials()
        except Exception as e:
            self._logger.error(f"Error in credential preload: {e}", exc_info=True)
            raise

    def _load_credentials_by_storage_method(self) -> None:
        """Load credentials based on the configured storage method."""
        self._logger.verbose(f"Credential storage method: {self.credential_storage}")

        if self._client_id and self._client_secret:
            self._logger.verbose("Using direct credentials")
            return

        if self.credential_storage == "env":
            pass  # Will fallback to env vars below
        elif self.credential_storage == "file":
            self._load_credentials_from_file()
        elif self.credential_storage == "prompt":
            self._load_credentials_from_prompt()
        else:
            self._logger.warning(f"Unknown storage method [{self.credential_storage}]. Falling back to environment variables.")

        if not self._client_id or not self._client_secret:
            self._load_credentials_from_env()

    def _load_credentials_from_file(self) -> None:
        """Load credentials from the credentials file."""
        creds_file_path, creds_file = utils.parse_filepath(Config.credential_file_path())
        path = creds_file_path / (creds_file or "wiz.credentials")

        self._logger.verbose(f"Looking for credentials file: {path}")
        if path.exists():
            creds = utils.load_credentials_from_file(self.profile, str(path))
            if creds:
                self._client_id = creds.get("client_id")
                self._client_secret = creds.get("client_secret")
                self._validate_environment_match(creds)
                self._logger.verbose(f"Credentials loaded from file for profile: {self.profile}")
            else:
                self._logger.warning(f"No credentials for profile {self.profile} found in file: {path}.")

    def _validate_environment_match(self, creds: Dict[str, Optional[str]]) -> None:
        """Validate that the loaded credentials match the expected environment."""
        if creds.get("environment") and creds.get("environment") != self.environment:
            self._logger.warning(
                f"Credentials for {self.profile} are for a different environment: {creds.get('environment')}"
            )

    def _load_credentials_from_prompt(self) -> None:
        """Load credentials from user prompt."""
        import getpass
        self._client_id = input(f"{self.profile} Client ID: ").strip()
        self._client_secret = getpass.getpass(f"{self.profile} Client Secret: ").strip()
        self._logger.verbose("Credentials input from prompt.")

    def _load_credentials_from_env(self) -> None:
        """Load credentials from environment variables."""
        self._logger.debug("Checking environment variables for credentials")
        self._client_id = os.environ.get("WIZ_CLIENT_ID") or os.getenv(f"{self.profile}_WIZ_CLIENT_ID")
        self._client_secret = os.environ.get("WIZ_CLIENT_SECRET") or os.getenv(f"{self.profile}_WIZ_CLIENT_SECRET")

    def _validate_and_store_credentials(self) -> None:
        """Validate that credentials were loaded and store them in profile state."""
        if not self._client_id or not self._client_secret:
            self._logger.error("Credentials not found.")
            raise WizCredentialsError("Failed to load credentials. Please check your credential configuration.")

        self._profile_state.client_id = self._client_id
        self._profile_state.client_secret = self._client_secret
        self._logger.verbose(f"Credentials set for profile: {self.profile}")

        if not self.serverless:
            self._try_save_credentials_to_file()

    def _try_save_credentials_to_file(self) -> None:
        """Attempt to save credentials to file, logging warnings on failure."""
        try:
            creds_file_path, creds_file = utils.parse_filepath(Config.credential_file_path())
            path = creds_file_path / (creds_file or "wiz.credentials")
            utils.write_credentials_to_file(
                profile=self.profile,
                client_id=self._client_id,
                client_secret=self._client_secret,
                environment=self.environment,
                credentials_file=str(path)
            )
        except Exception:
            self._logger.warning("Failed to save credentials to file. Continuing without saving.")

    def _authenticate(self, refresh=False):
        self._logger.debug(f"Authenticating (refresh={refresh})")
        if self._grant_type == "device_code":
            self._authenticate_device_code()
        else:
            self._authenticate_client_credentials()

    def _authenticate_device_code(self):
        self._logger.verbose("Starting device code authentication")
        auth_url = f"https://auth.{self._domain}/api/token/device"
        try:
            response = transport_post(url=auth_url, timeout=Config.api_timeout())
            response.raise_for_status()
            device_code_data = response.json()
            uri = device_code_data.get("verification_uri_complete")
            if uri:
                webbrowser.open(uri + f"&quiet={Config.quiet_auth()}", new=0, autoraise=True)
            else:
                self._logger.warning("No verification_uri_complete in response.")
            device_code = device_code_data.get("device_code")
            interval = device_code_data.get("interval", Config.auth_poll_time())
            elapsed = 0
            while elapsed < Config.api_timeout():
                token_response = transport_post(
                    url=auth_url,
                    headers=self._headers_auth,
                    data={"device_code": device_code},
                    timeout=Config.api_timeout()
                )
                if token_response.status_code == 200:
                    token_data = token_response.json()
                    token_data["time_received"] = time.time()
                    self._profile_state.token_data = token_data
                    token_type = token_data.get("token_type", "Bearer")
                    access_token = token_data.get("access_token")
                    self._env_state.headers["Authorization"] = f"{token_type} {access_token}"
                    if access_token:
                        if self._decode_access_token(access_token):
                            self._logger.verbose("Device code authentication successful")
                            return True
                time.sleep(interval)
                elapsed += interval
            raise WizTimeoutError("Timeout waiting for device code authentication")
        except TransportError as e:
            self._logger.error(f"Network error during device code authentication: {e}", exc_info=True)
            raise WizAPIError("Network error during device code authentication", original_error=e)
        except Exception as e:
            self._logger.error(f"Device code authentication failed: {e}", exc_info=True)
            raise WizAuthenticationError("Device code authentication failed", e)

    def _authenticate_client_credentials(self):
        self._logger.verbose("Authenticating via client credentials")
        auth_url = f"https://auth.{self._domain}/oauth/token"
        auth_payload = {
            'grant_type': 'client_credentials',
            'audience': 'wiz-api',
            'client_id': self._profile_state.client_id,
            'client_secret': self._profile_state.client_secret
        }
        try:
            proxy = self._proxies.get("https") or self._proxies.get("http")
            response = transport_post(auth_url, data=auth_payload, headers=self._headers_auth, proxy=proxy)
            if response.status_code != 200:
                self._logger.error(f"Failed to authenticate: {response.status_code} - {response.text}", exc_info=True)
                raise WizAuthenticationError(
                    f"Authentication failed with status {response.status_code}: {response.text}"
                )
            token_data = response.json()
            token_data["time_received"] = time.time()
            token_data["expiry_time"] = token_data["time_received"] + token_data["expires_in"]
            self._profile_state.token_data = token_data
            access_token = token_data.get("access_token")
            token_type = token_data.get("token_type", "Bearer")
            self._env_state.headers["Authorization"] = f"{token_type} {access_token}"
            self._decode_access_token(access_token)
            self._logger.verbose("Client credentials authentication successful")
            return True
        except TransportError as e:
            self._logger.error(f"Network error during authentication: {e}", exc_info=True)
            raise WizAPIError("Network error during authentication", original_error=e)
        except WizAuthenticationError:
            raise
        except Exception as e:
            self._logger.error(f"Client credentials authentication error: {e}", exc_info=True)
            raise WizAuthenticationError("Client credentials authentication failed", e)

    def _decode_access_token(self, token):
        if token:
            self._logger.debug("Decoding access token")
            try:
                parts = token.split('.')
                if len(parts) < 2:
                    raise ValueError("Invalid JWT")
                padded = parts[1] + '=' * (-len(parts[1]) % 4)
                payload = base64.urlsafe_b64decode(padded.encode())
                data = json.loads(payload)
                self.dc = data.get("dc")
                self.is_service_account = data.get("isServiceAccount", False)
                self._logger.verbose("Access token decoded successfully")
                return True
            except Exception as e:
                self._logger.error(f"Token decode failed: {e}", exc_info=True)
                return False
        return False

    def _token_expired(self):
        token_data = self._profile_state.token_data
        access_token = token_data.get("access_token")
        received = token_data.get("time_received", 0)
        if not access_token:
            return True
        expired = (time.time() - received) > 3600
        self._logger.debug(f"Token expired: {expired}")
        return expired

    # ========== LOGGING UTILITIES ==========

    @contextmanager
    def loud_logging(self):
        if self._verbose:
            try:
                self._logger.debug("Loud Logging on")
                if self._logger.console_handler.level != 15:
                    self._logger.initial_console_level = self._logger.console_handler.level
                    self._logger.console_handler.setLevel(15)
                yield
                self._logger.console_handler.setLevel(self._logger.initial_console_level)
                self._logger.debug("Loud Logging off")
            except Exception as e:
                self._logger.error(f"Loud logging setup error: {e}", exc_info=True)
                yield
        else:
            yield

    def set_log_level(self, level, handler_level=None, include_children=True):
        Config.set_log_level(level, handler_level=handler_level, include_children=include_children)
        self._logger = Config.get_logger()

    # ========== CLEANUP ==========

    def cleanup_for_lambda(self):
        """Clean up resources after Lambda execution — scoped to this client only."""
        if self.serverless:
            ProfileRegistry.cleanup(self.profile)
            EnvironmentRegistry.cleanup(self.environment)
