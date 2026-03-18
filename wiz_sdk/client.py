##
## 
##   CCCCC   L        IIIIIII   EEEEE   N     N   TTTTTTT
##  C        L           I      E       NN    N      T   
##  C        L           I      EEEEE   N N   N      T   
##  C        L           I      E       N  N  N      T   
##   CCCCC   LLLLLL   IIIIIII   EEEEE   N     N      T   
##
##
## Author: James Husted             Email: james@husted.dev
## Repo: https://github.com/HusteDev/wiz-sdk
##

# client.py
import requests
import threading
import time
import os
import base64
import json
import queue
from typing import TYPE_CHECKING, Optional, Dict, Any, Union
from pyrate_limiter import Duration, Limiter, Rate
import asyncio
import aiohttp
from contextlib import contextmanager
from wiz_sdk.config import Config
import wiz_sdk.utils as utils
import webbrowser
from wiz_sdk.exceptions import (
    WizAuthenticationError, WizAPIError, WizCredentialsError, 
    WizConfigurationError, WizTimeoutError, WizError
)

if TYPE_CHECKING:
    from wiz_sdk._request import WizRequest
    from wiz_sdk._request import WizResponse

class WizClient:
    _tokens = {}
    _locks = {}
    _global_lock = threading.Lock()
    _clients = {}  # (env, profile) -> WizClient instance
    _clients_lock = threading.Lock()
    _client_ids = {}
    _client_secrets = {}
    _dcs = {}
    _headers_auth = {"Content-Type": "application/x-www-form-urlencoded"}
    _content_type = "application/json"
    _headers = {}
    _query_queue = queue.Queue()
    _queue_lock = threading.Lock()
    _worker_thread = None
    _stop_worker = threading.Event()

    def __new__(cls, environment: str = "", profile: str = "", *args, **kwargs) -> "WizClient":
        environment = environment or Config.default_domain()
        profile = profile or "default"
        key = (environment, profile)

        if Config.serverless():
            # Create new instance for each Lambda invocation
            instance = super().__new__(cls)
            return instance

        with cls._clients_lock:
            if key in cls._clients:
                instance = cls._clients[key]
                return instance
            
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
        
        if log_lvl:
            Config.set_log_level(log_lvl)

        self._logger = Config.get_logger()
        self._logger.debug(f"Client Request: environment={environment}, profile={profile}, grant_type={grant_type}")
        if getattr(self, '_initialized', False):
            return
        self.profile = profile or "default"
        self.environment = environment or Config.default_domain()
        self._domain = Config.domain_root(self.environment)
        self._client_id = client_id
        self._client_secret = client_secret
        self._grant_type = grant_type or Config.grant_type()
        self.is_service_account = False if self._grant_type.startswith("device") else True
        self._query_retry_time = Config.api_retry()
        self._max_retries = Config.api_max_retries()
        self._proxies = Config.get_proxies()  # Will use env var if not set in Config file
        self._verbose = Config.verbose_mode()
        self.credential_storage = "prompt" if interactive else Config.storage_method()
        self.serverless = serverless or Config.serverless()
        
        self._logger.info(f"WizClient Initialized:  environment={self.environment}, profile={self.profile}, grant_type={self._grant_type}")

        self._initialize_headers()
        self._preload_credentials()
        self._initialized = True

    @property
    def access_token(self) -> Optional[str]:
        return WizClient._tokens.get(self.profile, {}).get("access_token")

    @access_token.setter
    def access_token(self, value: str) -> None:
        if self.profile not in WizClient._tokens:
            WizClient._tokens[self.profile] = {}
        WizClient._tokens[self.profile]["access_token"] = value

    @property
    def is_service_account(self):
        return self._is_service_account

    @is_service_account.setter
    def is_service_account(self, value):
        self._is_service_account = value

    @property
    def client_id(self):
        return WizClient._client_ids.get(self.profile)

    @client_id.setter
    def client_id(self, value):
        WizClient._client_ids[self.profile] = value

    @property
    def client_secret(self):
        return WizClient._client_secrets.get(self.profile)

    @client_secret.setter
    def client_secret(self, value):
        WizClient._client_secrets[self.profile] = value

    @property
    def dc(self):
        return WizClient._dcs.get(self.environment)

    @dc.setter
    def dc(self, value):
        WizClient._dcs[self.environment] = value

    def _initialize_headers(self):
        self._logger.debug("Initializing headers")
        user_agent = f'{Config.app_name()}/{Config.release_version()}'
        if self.environment not in WizClient._headers:
            WizClient._headers[self.environment] = {}
        WizClient._headers[self.environment] = {
            "Content-Type": self._content_type,
            "User-Agent": user_agent
        }
        self._logger.debug(f"Headers initialized: {WizClient._headers[self.environment]}")

    def _enqueue_request(self, request: "WizRequest"):
        """Add a request to the global query queue and start the worker."""
        self._logger.debug("Enqueuing request")

        if Config.serverless():
            # Execute immediately in serverless mode
            self._logger.debug("Serverless mode: executing request immediately")
            try:
                request._execute_page()
            except Exception as e:
                self._logger.error(f"Serverless execution error: {e}")
                request.errors.append({"message": str(e)})
                if hasattr(request, "_done_event"):
                    request._done_event.set()
            return

        WizClient._query_queue.put(request)
        self._start_worker()

    def _start_worker(self):
        with WizClient._queue_lock:
            if WizClient._worker_thread and WizClient._worker_thread.is_alive():
                return
            WizClient._worker_thread = threading.Thread(target=self._process_queue, daemon=True)
            WizClient._worker_thread.start()

    def _process_queue(self):
        while not WizClient._stop_worker.is_set():
            try:
                request = WizClient._query_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self._logger.debug("Processing request from queue")
            try:
                request._execute_page()
            except Exception as e:
                self._logger.error(f"Queue processing error: {e}")
            finally:
                WizClient._query_queue.task_done()

    def stop_worker(self):
        """Stop the background queue worker."""
        WizClient._stop_worker.set()
        if WizClient._worker_thread:
            WizClient._worker_thread.join(timeout=1)
        WizClient._stop_worker.clear()
        WizClient._worker_thread = None

    def create_request(
        self, 
        queryCollection: Optional[str] = None, 
        query: Optional[str] = None, 
        vars: Optional[Dict[str, Any]] = None, 
        **kwargs
    ) -> "WizResponse":
        self._logger.debug(f"Creating request with query: {query[:50] if query else 'None'}...")
        from wiz_sdk._request import WizRequest, WizResponse
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
        from wiz_sdk._request import WizBatchRequest
        return WizBatchRequest(client=self)
    
    # ========== ASYNC METHODS ==========
    # These methods provide async functionality alongside existing sync methods
    
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
            async with client.async_session():
                response = await client.create_async_request(query="...", vars={})
                result = await response.submit()
        """
        self._logger.debug(f"Creating async request with query: {query[:50] if query else 'None'}...")
        from wiz_sdk._request import AsyncWizRequest, AsyncWizResponse
        request = AsyncWizRequest(client=self, queryCollection=queryCollection, query=query, vars=vars, **kwargs)
        return AsyncWizResponse(request)
    
    async def create_async_batch_request(self) -> "AsyncWizBatchRequest":
        """
        Async version of create_batch_request with higher concurrency support.
        
        Example:
            async with client.async_session():
                batch = await client.create_async_batch_request()
                batch.add_request("query1", vars1)
                batch.add_request("query2", vars2)
                results = await batch.submit(max_concurrent=50)
        """
        self._logger.debug("Creating async batch request")
        from wiz_sdk._request import AsyncWizBatchRequest
        return AsyncWizBatchRequest(client=self)
    
    @contextmanager
    def async_session(self, session: Optional[aiohttp.ClientSession] = None):
        """
        Context manager for async operations. Manages aiohttp session lifecycle.
        
        Example:
            async with client.async_session() as async_client:
                response = await async_client.create_async_request(query="...")
                result = await response.submit()
        """
        class AsyncEnabledClient:
            def __init__(self, sync_client, session):
                self._sync_client = sync_client
                self._session = session
                self._owns_session = session is None
                
            async def __aenter__(self):
                if self._owns_session:
                    self._session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=Config.api_timeout()),
                        connector=aiohttp.TCPConnector(limit=100)
                    )
                # Inject session into sync client for async methods
                self._sync_client._async_session = self._session
                return self._sync_client
                
            async def __aexit__(self, exc_type, exc_val, exc_tb):
                if self._owns_session and self._session:
                    await self._session.close()
                # Clean up
                if hasattr(self._sync_client, '_async_session'):
                    delattr(self._sync_client, '_async_session')
        
        return AsyncEnabledClient(self, session)

    def _api_endpoint(self) -> str:
        dc = self.dc
        endpoint = f"https://api.{dc}.{self._domain}/graphql"
        self._logger.debug(f"API endpoint resolved: {endpoint}")
        return endpoint

    def _get_headers(self) -> Dict[str, str]:
        headers = WizClient._headers.get(self.environment, {})
        safe_keys = {k: ("***" if k == "Authorization" else v) for k, v in headers.items()}
        self._logger.debug(f"Headers fetched: {safe_keys}")
        return headers

    def _post(self, **kwargs):
        self._logger.debug(f"POST to {self._api_endpoint()}")
        self._logger.debug(f"POST data: {kwargs.get('json', {})}")
        try:
            response = requests.post(
                proxies=self._proxies,
                verify=os.environ.get('REQUESTS_CA_BUNDLE', None),
                timeout=Config.api_timeout(),
                **kwargs
            )
            self._logger.verbose(f"Response status code: {response.status_code}")
            return response
        except Exception as e:
            self._logger.error(f"POST request failed: {e}", exc_info=True)
            raise

    def _limiter_key(self, request: "WizRequest" = None):
        info = getattr(request, "_current_query_info", {}) or {}
        qtype = str(info.get("request_type", "query")).lower()
        source = "service" if self._is_service_account else "user"
        self._logger.debug("Limiter key: %s_%s", qtype, source)
        return f"{qtype}_{source}"

    def _get_limiter(self, key):
        rate_configs = {
            "query_user": Rate(100, Duration.SECOND),
            "query_service": Rate(10, Duration.SECOND),
            "mutation_user": Rate(10, Duration.SECOND),
            "mutation_service": Rate(3, Duration.SECOND)
        }
        if not hasattr(self, '_limiters'):
            self._limiters = {k: Limiter(v, max_delay=1000) for k, v in rate_configs.items()}
        limiter = self._limiters.get(key)
        if limiter is None:
            raise ValueError(f"No limiter for key: {key}")
        return limiter

    def _check_token(self):
        self._logger.debug("Checking token for profile: %s", self.profile)
        with WizClient._get_or_create_lock(self.profile):
            # In serverless mode, always authenticate fresh to avoid token issues
            if Config.serverless():
                self._logger.verbose("Serverless mode: forcing fresh authentication")
                self._authenticate()
            if self.profile not in WizClient._tokens:
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
        
        # If credentials are provided during client initialization, use them directly
        if self._client_id and self._client_secret:
            self._logger.verbose("Using direct credentials")
            return
            
        if self.credential_storage == "env":
            pass  # Will fallback to env vars later
        elif self.credential_storage == "file":
            self._load_credentials_from_file()
        elif self.credential_storage == "prompt":
            self._load_credentials_from_prompt()
        else:
            self._logger.warning(f"Unknown storage method [{self.credential_storage}]. Falling back to environment variables.")
        
        # Fallback to environment variables if credentials not loaded yet
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
        # Looks for WIZ_CLIENT_ID first, then <profile>_WIZ_CLIENT_ID
        self._client_id = os.environ.get("WIZ_CLIENT_ID") or os.getenv(f"{self.profile}_WIZ_CLIENT_ID")
        self._client_secret = os.environ.get("WIZ_CLIENT_SECRET") or os.getenv(f"{self.profile}_WIZ_CLIENT_SECRET")
    
    def _validate_and_store_credentials(self) -> None:
        """Validate that credentials were loaded and store them."""
        if not self._client_id or not self._client_secret:
            self._logger.error("Credentials not found.")
            raise WizCredentialsError("Failed to load credentials. Please check your credential configuration.")
        
        # Store the credentials in class variables
        WizClient._client_ids[self.profile] = self._client_id
        WizClient._client_secrets[self.profile] = self._client_secret
        self._logger.verbose(f"Credentials set for profile: {self.profile}")
        
        # Try to save credentials to file in non-serverless mode
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
            self._logger.warning(f"Failed to save credentials to file. Continuing without saving.")

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
            response = requests.post(url=auth_url, timeout=Config.api_timeout())
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
                token_response = requests.post(
                    url=auth_url,
                    headers=self._headers_auth,
                    data={"device_code": device_code},
                    timeout=Config.api_timeout()
                )
                if token_response.status_code == 200:
                    token_data = token_response.json()
                    token_data["time_received"] = time.time()
                    WizClient._tokens[self.profile] = token_data
                    token_type = token_data.get("token_type", "Bearer")
                    access_token = token_data.get("access_token")
                    WizClient._headers[self.environment]["Authorization"] = f"{token_type} {access_token}"
                    if access_token:
                        if self._decode_access_token(access_token):
                            self._logger.verbose("Device code authentication successful")
                            return True
                time.sleep(interval)
                elapsed += interval
            raise WizTimeoutError("Timeout waiting for device code authentication")
        except requests.RequestException as e:
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
            'client_id': WizClient._client_ids[self.profile],
            'client_secret': WizClient._client_secrets[self.profile]
        }
        try:
            response = requests.post(auth_url, data=auth_payload, headers=self._headers_auth, proxies=self._proxies)
            if response.status_code != 200:
                self._logger.error(f"Failed to authenticate: {response.status_code} - {response.text}", exc_info=True)
                raise WizAuthenticationError(
                    f"Authentication failed with status {response.status_code}: {response.text}"
                )
            token_data = response.json()
            token_data["time_received"] = time.time()
            token_data["expiry_time"] = token_data["time_received"] + token_data["expires_in"]
            WizClient._tokens[self.profile] = token_data
            access_token = token_data.get("access_token")
            token_type = token_data.get("token_type", "Bearer")
            WizClient._headers[self.environment]["Authorization"] = f"{token_type} {access_token}"
            self._decode_access_token(access_token)
            self._logger.verbose("Client credentials authentication successful")
            return True
        except requests.RequestException as e:
            self._logger.error(f"Network error during authentication: {e}", exc_info=True)
            raise WizAPIError("Network error during authentication", original_error=e)
        except WizAuthenticationError:
            raise  # Re-raise our custom exception
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
        else:
            return False

    def _token_expired(self):
        token_info = WizClient._tokens.get(self.profile, {})
        access_token = token_info.get("access_token")
        received = token_info.get("time_received", 0)
        if not access_token:
            return True
        self._decode_access_token(access_token)
        expired = (time.time() - received) > 3600
        self._logger.debug(f"Token expired: {expired}")
        return expired

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

    @classmethod
    def _get_or_create_lock(cls, profile):
        with cls._global_lock:
            if profile not in cls._locks:
                cls._locks[profile] = threading.Lock()
            return cls._locks[profile]

    def cleanup_for_lambda(self):
        """Clean up resources after Lambda execution"""
        if self.serverless:
            # Clear class-level caches
            WizClient._tokens.clear()
            WizClient._client_ids.clear()
            WizClient._client_secrets.clear()
            WizClient._dcs.clear()
            WizClient._headers.clear()
            
            # Stop any remaining workers
            if hasattr(self, '_worker_thread') and self._worker_thread:
                WizClient._stop_worker.set()